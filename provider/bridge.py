"""In-process bridge between OSWorld's DesktopEnv and one E2B guest sandbox.

DesktopEnv dials http://127.0.0.1:<port> for the guest server, Chrome CDP and
VLC. E2B exposes those as https://<port>-<sandbox>.e2b.app behind a per-sandbox
traffic token. The Bridge binds loopback listeners on OS-assigned ports, proxies
each request (HTTP and WebSocket) to the right ingress host, rewrites CDP's
advertised host, and owns the sandbox lifecycle: create, strict replace on
revert, snapshot save/revert, activity heartbeat, kill at stop.

One Bridge per DesktopEnv. It runs its asyncio loop on a daemon thread and
exposes synchronous methods for the provider. Nothing here listens beyond
127.0.0.1 and no method returns the traffic token.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import aiohttp
import httpx
from aiohttp import web
from e2b import Sandbox, TimeoutException

from e2b_policy import (
    require_campaign_id,
    require_immutable_template_ref,
    sandbox_network_policy,
)

logger = logging.getLogger("desktopenv.providers.e2b.bridge")

GUEST_SERVER_PORT = 5000
GUEST_CDP_PORT = 9222
GUEST_VLC_PORT = 8080

HEARTBEAT_RETRY_ATTEMPTS = 3
HEARTBEAT_RETRY_DELAY_S = 2.0
SETUP_UPLOAD_RETRY_ATTEMPTS = 3
SETUP_UPLOAD_RETRY_DELAY_S = 1.0
SETUP_UPLOAD_TRANSIENT_ERRORS = (
    TimeoutError,
    TimeoutException,
    aiohttp.ClientConnectionError,
    httpx.TransportError,
)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class BridgeConfig:
    template: str
    campaign_id: str
    sandbox_timeout_s: int = 3600
    ready_timeout_s: int = 180
    http_timeout_s: int = 240
    heartbeat_interval_s: int = 300
    ws_connect_retry_s: int = 20
    task_service_ports: str = ""
    guest_proxy_script: str | None = None
    fleet_rules: str | None = None
    website_host_suffix: str = "127.0.0.1.nip.io"
    guest_proxy_ports: str = "80,443,8090"
    # Subset of guest_proxy_ports that terminates TLS once a campaign CA/leaf
    # pair is present (see _install_guest_proxy). Not env-configurable: the
    # port split is a property of the guest proxy's own listener setup, not a
    # per-run knob.
    guest_proxy_tls_ports: str = "443,8090"
    retain_snapshots: bool = False

    @classmethod
    def from_env(cls, template: str | None = None) -> "BridgeConfig":
        env = os.environ
        timeout = _env_int("SANDBOX_TIMEOUT_S", 3600)
        return cls(
            template=require_immutable_template_ref(
                template or env.get("GUEST_TEMPLATE"), "GUEST_TEMPLATE"
            ),
            campaign_id=require_campaign_id(env.get("OSWORLD_CAMPAIGN_ID")),
            sandbox_timeout_s=timeout,
            ready_timeout_s=_env_int("GUEST_READY_TIMEOUT_S", 180),
            http_timeout_s=_env_int("RELAY_HTTP_TIMEOUT_S", 240),
            heartbeat_interval_s=max(
                30, min(_env_int("SANDBOX_HEARTBEAT_INTERVAL_S", 300), timeout // 4)
            ),
            ws_connect_retry_s=_env_int("RELAY_WS_CONNECT_RETRY_S", 20),
            task_service_ports=env.get("OSWORLD_TASK_SERVICE_PORTS", ""),
            guest_proxy_script=env.get("HOSTMAP_PROXY_SCRIPT") or None,
            fleet_rules=env.get("OSWORLD_FLEET_RULES") or None,
            website_host_suffix=env.get("WEBSITE_HOST_SUFFIX", "127.0.0.1.nip.io"),
            guest_proxy_ports=env.get("GUEST_HOSTMAP_PORTS", "80,443,8090"),
            retain_snapshots=env.get("OSWORLD_RETAIN_SNAPSHOTS") == "1",
        )


def parse_task_service_ports(raw: str) -> dict[int, int]:
    """`port` or `local:guest` entries, comma separated -> {local: guest}."""
    mappings: dict[int, int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 1:
            local = guest = int(parts[0])
        elif len(parts) == 2:
            local, guest = (int(part) for part in parts)
        else:
            raise ValueError(f"invalid task-service port mapping: {entry!r}")
        if not (1 <= local <= 65535 and 1 <= guest <= 65535):
            raise ValueError(
                f"task-service ports must be between 1 and 65535: {entry!r}"
            )
        if local in mappings:
            raise ValueError(f"duplicate local task-service port: {local}")
        mappings[local] = guest
    return mappings


class GuestUnavailable(RuntimeError):
    """No live guest: the bridge is starting, replacing, or stopped."""


@dataclass(frozen=True)
class Guest:
    sandbox: Sandbox
    sandbox_id: str
    traffic_token: str
    hosts: dict[int, str]
    generation: int
    source: str = "template"


def _install_guest_proxy(sandbox: Sandbox, config: BridgeConfig) -> None:
    """Upload the stdlib Host-mapping proxy + fleet routing file into the guest
    and start it as root on :80 (and :443/:8090 once a campaign CA is present).
    No-op unless both env vars are configured, so the pure boundary-layer path
    is untouched. Runs in a worker thread.

    When the runtime file carries a `tls` section (see campaign_tls.py), the
    campaign's leaf cert/key and CA cert are uploaded and the CA is installed
    into both the guest's system trust store and Chrome's NSS database *before*
    the proxy starts, so Chrome finds it already trusted at launch. Only
    ca.crt/leaf.crt/leaf.key ever leave the host -- the CA private key
    (`ca_key`, deliberately absent from the `tls` runtime section) never
    reaches the guest.
    """
    if not (config.guest_proxy_script and config.fleet_rules):
        return
    script = Path(config.guest_proxy_script).read_text()
    raw_runtime_json = Path(config.fleet_rules).read_text()
    rules = _guest_proxy_runtime_json(raw_runtime_json)
    sandbox.files.write("/opt/hostmap_proxy.py", script)
    sandbox.files.write("/opt/fleet_runtime.json", rules)
    sandbox.commands.run(
        "chmod 0700 /opt/hostmap_proxy.py && chmod 0600 /opt/fleet_runtime.json",
        user="root",
        timeout=15,
    )

    runtime = json.loads(raw_runtime_json)
    tls = runtime.get("tls") if isinstance(runtime.get("tls"), dict) else None
    tls_env = ""
    if tls and tls.get("leaf_cert"):
        for guest_name, runtime_key in (
            ("leaf.crt", "leaf_cert"),
            ("leaf.key", "leaf_key"),
            ("ca.crt", "ca_cert"),
        ):
            sandbox.files.write(
                f"/opt/hostmap-tls/{guest_name}", Path(tls[runtime_key]).read_text()
            )
        # Trust install runs before the proxy starts below, so Chrome (started
        # later, after the guest server responds ready) always sees the CA
        # already installed. A missing certutil/nssdb or a failed
        # update-ca-certificates must fail loudly here rather than silently
        # leave Chrome untrusting: neither command backgrounds or swallows its
        # exit code, so sandbox.commands.run's default foreground wait() raises
        # CommandExitException on any non-zero exit and this function propagates it.
        sandbox.commands.run(
            "chmod 0700 /opt/hostmap-tls && chmod 0600 /opt/hostmap-tls/leaf.key && "
            "install -m 0644 /opt/hostmap-tls/ca.crt "
            "/usr/local/share/ca-certificates/osworld-campaign.crt && "
            "update-ca-certificates >/dev/null",
            user="root",
            timeout=60,
        )
        sandbox.commands.run(
            'certutil -d sql:/home/user/.pki/nssdb -A -t "C,," '
            "-n osworld-campaign -i /opt/hostmap-tls/ca.crt",
            user="user",
            timeout=30,
        )
        tls_env = (
            f"HOSTMAP_TLS_PORTS={config.guest_proxy_tls_ports} "
            "HOSTMAP_TLS_CERT=/opt/hostmap-tls/leaf.crt "
            "HOSTMAP_TLS_KEY=/opt/hostmap-tls/leaf.key "
        )
    # Without TLS material there is nothing listening on 443 to answer, so
    # fall back to the pre-TLS port list rather than binding a port config
    # advertises but the proxy can't yet serve securely.
    ports = config.guest_proxy_ports if tls_env else "80,8090"

    # nip.io resolves site hosts to 127.0.0.1 from inside E2B sandboxes; if a
    # guest's DNS blocks it, fall back to enumerated /etc/hosts entries (the site
    # list is enumerable from the uploaded runtime file). Strip any :port from the
    # suffix before a DNS lookup.
    portless_suffix = config.website_host_suffix.split(":")[0]
    probe = sandbox.commands.run(
        f"getent hosts mailhub.{portless_suffix} >/dev/null && echo ok || echo no",
        user="root",
        timeout=20,
    )
    if "ok" not in (probe.stdout or ""):
        hosts = _fleet_hostnames(rules, config.website_host_suffix)
        if hosts:
            entry = "127.0.0.1 " + " ".join(hosts)
            sandbox.commands.run(
                f"grep -q '{hosts[0]}' /etc/hosts || echo '{entry}' >> /etc/hosts",
                user="root",
                timeout=15,
            )
    # Task 041 hardcodes a public GitLab host as a sslip.io address that
    # resolves publicly (not to loopback), so the nip.io probe above can never
    # detect it -- this alias must be routed to the guest proxy unconditionally,
    # not gated on that probe's outcome. grep-gated on its own first alias so
    # repeated installs of the same guest stay idempotent.
    aliases = [str(a) for a in (runtime.get("gitlab") or {}).get("aliases") or []]
    if aliases:
        entry = "127.0.0.1 " + " ".join(aliases)
        sandbox.commands.run(
            f"grep -q '{aliases[0]}' /etc/hosts || echo '{entry}' >> /etc/hosts",
            user="root",
            timeout=15,
        )

    sandbox.commands.run(
        f"HOSTMAP_PORT={ports} {tls_env}FLEET_RUNTIME_FILE=/opt/fleet_runtime.json "
        "python3 /opt/hostmap_proxy.py > /var/log/hostmap_proxy.log 2>&1",
        user="root",
        background=True,
        timeout=0,
    )
    logger.info("guest Host-mapping proxy installed on :%s", ports)


def _guest_proxy_runtime_json(rules_json: str) -> str:
    """Return the minimum routing data and fleet bearers the guest proxy needs.

    The guest is trusted with the fleet traffic tokens in this compatibility
    path, but it must never receive the GitLab PAT or host-local token paths.
    """
    runtime = json.loads(rules_json)
    websites = runtime.get("websites") or {}
    gitlab = runtime.get("gitlab") or {}
    safe = {
        "websites": {
            key: websites[key]
            for key in (
                "traffic_token",
                "host_suffix",
                "public_host_suffix",
                "scheme",
                "asset_url_map",
            )
            if key in websites
        }
        | {
            "sites": {
                str(name): {
                    key: info[key] for key in ("ingress_host", "port") if key in info
                }
                for name, info in (websites.get("sites") or {}).items()
                if isinstance(info, dict)
            }
        },
        "gitlab": {
            key: gitlab[key]
            for key in (
                "traffic_token",
                "host",
                "url",
                "ingress_host",
                "port",
                "scheme",
                "aliases",
            )
            if key in gitlab
        },
    }
    return json.dumps(safe, separators=(",", ":"))


def _fleet_hostnames(rules_json: str, default_suffix: str) -> list[str]:
    try:
        runtime = json.loads(rules_json)
    except json.JSONDecodeError:
        return []
    hosts: list[str] = []
    web_section = runtime.get("websites") or {}
    suffix = web_section.get("host_suffix", default_suffix)
    for site in web_section.get("sites") or {}:
        hosts.append(f"{site}.{suffix}")
    gl = runtime.get("gitlab") or {}
    if gl.get("host"):
        hosts.append(gl["host"])
    hosts.extend(str(alias) for alias in gl.get("aliases") or [])
    return hosts


async def _direct_setup_upload(
    request: web.Request, guest: Guest, config: BridgeConfig
) -> web.Response:
    """Stream the OSWorld setup upload through E2B's native file API."""
    destination: str | None = None
    staged_path: Path | None = None
    seen_fields: set[str] = set()
    try:
        try:
            reader = await request.multipart()
            while part := await reader.next():
                if part.name not in {"file_path", "file_data"}:
                    continue
                if part.name in seen_fields:
                    raise ValueError(f"duplicate multipart field: {part.name}")
                seen_fields.add(part.name)
                if part.name == "file_path":
                    destination = await part.text()
                    continue
                with tempfile.NamedTemporaryFile(
                    prefix="osworld-upload-", delete=False
                ) as staged:
                    staged_path = Path(staged.name)
                    while chunk := await part.read_chunk(size=1024 * 1024):
                        staged.write(chunk)
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid multipart upload") from exc

        if (
            destination is None
            or not PurePosixPath(destination).is_absolute()
            or staged_path is None
        ):
            raise web.HTTPBadRequest(
                text="absolute file_path and file_data are required"
            )

        destination_path = PurePosixPath(destination)
        staging_name = (
            f".{destination_path.name or 'upload'}.osworld-upload-"
            f"{secrets.token_hex(12)}"
        )
        guest_staging_path = str(destination_path.parent / staging_name)
        committed = False
        try:
            for attempt in range(1, SETUP_UPLOAD_RETRY_ATTEMPTS + 1):
                try:
                    with staged_path.open("rb") as payload:
                        await asyncio.to_thread(
                            guest.sandbox.files.write,
                            guest_staging_path,
                            payload,
                            user="user",
                            request_timeout=config.http_timeout_s,
                            use_octet_stream=True,
                        )
                    await asyncio.to_thread(
                        guest.sandbox.files.rename,
                        guest_staging_path,
                        destination,
                        user="user",
                        request_timeout=config.http_timeout_s,
                    )
                    committed = True
                    size = staged_path.stat().st_size
                    return web.Response(text=f"File Uploaded: {size} bytes")
                except SETUP_UPLOAD_TRANSIENT_ERRORS as exc:
                    if attempt == SETUP_UPLOAD_RETRY_ATTEMPTS:
                        raise web.HTTPBadGateway(
                            text="native E2B setup upload failed"
                        ) from exc
                    await asyncio.sleep(SETUP_UPLOAD_RETRY_DELAY_S * attempt)
                except Exception as exc:
                    raise web.HTTPBadGateway(
                        text="native E2B setup upload failed"
                    ) from exc
            raise AssertionError("unreachable")
        finally:
            if not committed:
                try:
                    await asyncio.to_thread(
                        guest.sandbox.files.remove,
                        guest_staging_path,
                        user="user",
                        request_timeout=config.http_timeout_s,
                    )
                except Exception as exc:
                    logger.warning(
                        "could not remove failed upload staging %s: %s",
                        guest_staging_path,
                        exc,
                    )
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


class GuestManager:
    def __init__(self, config: BridgeConfig, guest_ports: frozenset[int]) -> None:
        self._config = config
        self._guest_ports = guest_ports
        self._guest: Guest | None = None
        # Survives stop() so shutdown reporting can name the last guest.
        self._last_sandbox_id: str | None = None
        self._lock = asyncio.Lock()
        self._replace_lock = asyncio.Lock()
        # Set once by stop() and never cleared: replace() re-checks it around its
        # slow create so a /stop or SIGTERM that lands mid-/reset cannot race a
        # freshly created sandbox into _guest after shutdown already killed
        # everything (the fresh sandbox would otherwise run out its full
        # sandbox_timeout_s with no owner).
        self._stopped = False
        # OSWorld snapshot name -> E2B snapshot id (memory + filesystem state).
        # Run-owned snapshots are deleted at shutdown by default. Explicit
        # retain_snapshots keeps them for a debugging session.
        self._snapshots: dict[str, str] = {}
        self._retain_snapshots = config.retain_snapshots
        # Activity-based timeout heartbeat state: touch() advances _last_activity
        # on guest-directed traffic; _heartbeat_once() refreshes the sandbox
        # timeout only when activity happened since the last refresh.
        self._last_activity = 0.0
        self._last_refresh = 0.0

    def touch(self) -> None:
        """Record guest-directed activity for the timeout heartbeat."""
        self._last_activity = asyncio.get_running_loop().time()

    def last_sandbox_id(self) -> str | None:
        """The most recent guest's sandbox id, kept past stop() for reporting."""
        return self._last_sandbox_id

    def snapshot_ids(self) -> dict[str, str]:
        """This run's saved snapshots (OSWorld name -> persistent E2B id)."""
        return dict(self._snapshots)

    async def _heartbeat_once(self) -> bool:
        """Refresh the current guest's timeout iff traffic flowed since the last
        refresh. Returns True when a refresh was issued."""
        async with self._lock:
            guest = self._guest
            if (
                self._stopped
                or guest is None
                or self._last_activity <= self._last_refresh
            ):
                return False
        for attempt in range(1, HEARTBEAT_RETRY_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(
                    guest.sandbox.set_timeout, self._config.sandbox_timeout_s
                )
                break
            except Exception as exc:
                logger.warning(
                    "timeout refresh attempt %s/%s failed for %s: %s",
                    attempt,
                    HEARTBEAT_RETRY_ATTEMPTS,
                    guest.sandbox_id,
                    exc,
                )
                if attempt == HEARTBEAT_RETRY_ATTEMPTS:
                    return False
                await asyncio.sleep(HEARTBEAT_RETRY_DELAY_S * attempt)
        async with self._lock:
            if self._stopped or self._guest is not guest:
                return False
            self._last_refresh = asyncio.get_running_loop().time()
        return True

    async def heartbeat(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self._config.heartbeat_interval_s)
            await self._heartbeat_once()

    async def current(self) -> Guest:
        async with self._lock:
            if self._guest is None:
                raise GuestUnavailable("guest is not ready")
            return self._guest

    async def _kill(self, guest: Guest) -> None:
        try:
            await asyncio.to_thread(guest.sandbox.kill)
        except Exception as exc:
            logger.warning("could not kill %s: %s", guest.sandbox_id, exc)

    async def _create(
        self, generation: int, source: str | None, abandoned: threading.Event
    ) -> Guest:
        def create_sync() -> Sandbox:
            template_or_snapshot = source or self._config.template
            sandbox = Sandbox.create(
                template_or_snapshot,
                timeout=self._config.sandbox_timeout_s,
                secure=True,
                network=sandbox_network_policy(),
                metadata={
                    "workload": "osworld",
                    "generation": str(generation),
                    "campaign_id": self._config.campaign_id,
                },
            )
            # The awaiting task can be cancelled while this thread runs (client
            # disconnect, shutdown, a timed-out Bridge._call); the thread still
            # completes and would leak the sandbox. asyncio.run() waits for
            # executor threads on exit, so reaping here is guaranteed to run
            # before the process dies.
            # The RuntimeErrors below land in an awaiter that is usually already
            # cancelled, so log the reap here or it leaves no trace at all.
            if self._stopped:
                sandbox.kill()
                logger.warning(
                    "reaped just-created sandbox %s: %s",
                    sandbox.sandbox_id,
                    "bridge stopping",
                )
                raise RuntimeError(
                    f"bridge stopping; reaped just-created sandbox {sandbox.sandbox_id}"
                )
            if abandoned.is_set():
                sandbox.kill()
                logger.warning(
                    "reaped just-created sandbox %s: %s",
                    sandbox.sandbox_id,
                    "replace was cancelled",
                )
                raise RuntimeError(
                    f"replace was cancelled; reaped just-created sandbox "
                    f"{sandbox.sandbox_id}"
                )
            return sandbox

        sandbox = await asyncio.to_thread(create_sync)
        token = getattr(sandbox, "traffic_access_token", None)
        if not token:
            await asyncio.to_thread(sandbox.kill)
            raise RuntimeError("E2B did not return a traffic access token")
        guest = Guest(
            sandbox=sandbox,
            sandbox_id=sandbox.sandbox_id,
            traffic_token=token,
            hosts={port: sandbox.get_host(port) for port in self._guest_ports},
            generation=generation,
            source=f"snapshot:{source}" if source else "template",
        )
        try:
            await self._wait_ready(guest)
            await asyncio.to_thread(_install_guest_proxy, sandbox, self._config)
        except BaseException:
            await asyncio.to_thread(sandbox.kill)
            raise
        return guest

    async def _wait_ready(self, guest: Guest) -> None:
        deadline = asyncio.get_running_loop().time() + self._config.ready_timeout_s
        headers = {"e2b-traffic-access-token": guest.traffic_token}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    url = f"https://{guest.hosts[GUEST_SERVER_PORT]}/screen_size"
                    async with session.post(url, headers=headers) as response:
                        if response.status == 200:
                            return
                except (TimeoutError, aiohttp.ClientError):
                    pass
                await asyncio.sleep(2)
        raise TimeoutError(f"guest {guest.sandbox_id} did not become ready")

    async def replace(self, source: str | None = None) -> Guest:
        async with self._replace_lock:
            async with self._lock:
                if self._stopped:
                    raise GuestUnavailable("bridge is stopping")
                generation = 1 if self._guest is None else self._guest.generation + 1
            # A cancelled caller (a timed-out Bridge._call, a client disconnect)
            # must not strand a sandbox: create_sync reaps one it is still
            # building, and a cancel landing after _create returned is reaped
            # here, so the guest this manager tracks is always one that lives.
            abandoned = threading.Event()
            new_guest = None
            installed = False
            try:
                new_guest = await self._create(generation, source, abandoned)
                async with self._lock:
                    # stop() may have run while _create was in flight (it only
                    # takes _lock, which we release across the slow create);
                    # installing the new guest now would orphan it, so reap it.
                    if self._stopped:
                        await self._kill(new_guest)
                        raise GuestUnavailable("bridge is stopping")
                    old_guest = self._guest
                    self._guest = new_guest
                    self._last_sandbox_id = new_guest.sandbox_id
                    installed = True
                    # The fresh sandbox starts with a full sandbox_timeout_s, so
                    # the heartbeat owes it nothing until new traffic arrives.
                    now = asyncio.get_running_loop().time()
                    self._last_activity = now
                    self._last_refresh = now
            except asyncio.CancelledError:
                abandoned.set()
                if new_guest is not None and not installed:
                    await self._kill(new_guest)
                raise
            if old_guest is not None:
                await self._kill(old_guest)
        logger.info(
            "guest ready id=%s generation=%s template=%s",
            new_guest.sandbox_id,
            generation,
            self._config.template,
        )
        return new_guest

    async def save_snapshot(self, name: str) -> str:
        """Capture the current guest's memory + filesystem as an E2B snapshot.

        The sandbox pauses briefly during capture and resumes automatically.
        Snapshots persist independently of the sandbox and can seed any number
        of new sandboxes, so a saved name stays revert-able for the whole run.
        """
        guest = await self.current()
        self.touch()
        info = await asyncio.to_thread(guest.sandbox.create_snapshot)
        self._snapshots[name] = info.snapshot_id
        # The capture pauses the guest and drops any live CDP websockets. Gate
        # the /save response on the resumed server answering again, so OSWorld
        # only proceeds (and clients only re-dial CDP) against a live guest —
        # the websocket handler's connect retry covers stragglers that re-dial
        # while the resume is still settling.
        await self._wait_ready(guest)
        logger.info(
            "snapshot saved name=%s id=%s sandbox=%s",
            name,
            info.snapshot_id,
            guest.sandbox_id,
        )
        return info.snapshot_id

    async def check_volume(self, requested_gb: int) -> dict[str, int]:
        """Verify the running guest's root filesystem satisfies OSWorld's
        provider-level ``volume_size`` request. E2B root capacity is fixed when
        the template is built, so this checks the restored filesystem instead of
        attaching a volume or resizing a partition."""
        if requested_gb <= 0:
            raise ValueError("requested_gb must be a positive integer")
        guest = await self.current()
        self.touch()
        result = await asyncio.to_thread(
            guest.sandbox.commands.run,
            "df -B1 --output=size / | tail -n 1",
            user="root",
            timeout=30,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"could not measure sandbox root capacity: {result.stderr or result.stdout}"
            )
        root_capacity_bytes = int((result.stdout or "").strip())
        # OSWorld's volume_size and E2B's disk sizes are both decimal GB, so
        # compare decimal bytes: an E2B 100 GB root satisfies a 100 GB request
        # even though filesystem overhead makes df's binary display smaller.
        required_bytes = requested_gb * 1_000_000_000
        if root_capacity_bytes < required_bytes:
            raise RuntimeError(
                f"OSWorld task requires {requested_gb} GB root capacity, but E2B "
                f"sandbox {guest.sandbox_id} has "
                f"{root_capacity_bytes / 1_000_000_000:.2f} GB"
            )
        return {
            "requested_gb": requested_gb,
            "required_bytes": required_bytes,
            "root_capacity_bytes": root_capacity_bytes,
        }

    async def revert(self, snapshot_name: str | None = None) -> Guest:
        """Replace the guest: from a saved snapshot if the name is known,
        otherwise from the immutable template (OSWorld's default revert names,
        e.g. "init_state", are never explicitly saved and mean base state)."""
        source = self._snapshots.get(snapshot_name) if snapshot_name else None
        return await self.replace(source)

    def request_stop(self) -> None:
        """Mark the manager stopped from any thread, without the event loop.

        Bridge.stop() calls this before it signals the loop, so a create still
        in flight reaps its own sandbox as soon as it returns instead of
        finishing readiness and proxy install first."""
        self._stopped = True

    async def stop(self) -> None:
        async with self._lock:
            self._stopped = True
            guest, self._guest = self._guest, None
            snapshots = dict(self._snapshots)
            if not self._retain_snapshots:
                self._snapshots.clear()
        if guest is not None:
            await self._kill(guest)
        if not self._retain_snapshots:
            for snapshot_id in sorted(set(snapshots.values())):
                try:
                    await asyncio.to_thread(Sandbox.delete_snapshot, snapshot_id)
                except Exception as exc:
                    logger.warning("could not delete snapshot %s: %s", snapshot_id, exc)

    def public_state(self, guest: Guest) -> dict[str, object]:
        return {
            "ready": True,
            "sandbox_id": guest.sandbox_id,
            "generation": guest.generation,
            "template": self._config.template,
            "campaign_id": self._config.campaign_id,
            "source": guest.source,
            "sandbox_timeout_seconds": self._config.sandbox_timeout_s,
            "heartbeat_interval_seconds": self._config.heartbeat_interval_s,
            "restricted_ingress": True,
            "snapshots": self.snapshot_ids(),
        }


def _upstream_headers(request: web.Request, guest: Guest) -> dict[str, str]:
    excluded = {
        "host",
        "connection",
        "content-length",
        "accept-encoding",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
    }
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in excluded
    }
    headers["e2b-traffic-access-token"] = guest.traffic_token
    return headers


def _rewrite_cdp_host(payload: bytes, local_port: int) -> bytes:
    text = payload.decode("utf-8", "replace")
    text = re.sub(
        r"ws://[^/\"]+/devtools", f"ws://127.0.0.1:{local_port}/devtools", text
    )
    text = re.sub(r"\"host\":\s*\"[^\"]*\"", f'"host": "127.0.0.1:{local_port}"', text)
    return text.encode()


async def _ws_connect_with_retry(session, url, headers, retry_s: int):
    """ws_connect, retrying for up to retry_s seconds before raising.

    A guest resuming from a create_snapshot pause (or mid-replace) refuses
    connections for a few seconds; a client re-dialing CDP in that window should
    connect as soon as the guest is back, not fail instantly."""
    deadline = asyncio.get_running_loop().time() + retry_s
    while True:
        try:
            return await session.ws_connect(url, headers=headers, max_msg_size=0)
        except Exception:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(1)


class Bridge:
    """Synchronous facade over GuestManager + loopback proxy listeners.

    start() binds listeners on 127.0.0.1:0 (plus any literal task-service
    ports), starts the loop thread, and creates the first guest. Every other
    method schedules a coroutine on that loop and waits for it.
    """

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self._task_ports = parse_task_service_ports(config.task_service_ports)
        guest_ports = {GUEST_SERVER_PORT, GUEST_CDP_PORT, GUEST_VLC_PORT}
        guest_ports.update(self._task_ports.values())
        self._manager = GuestManager(config, frozenset(guest_ports))
        # local listener port -> guest port, filled in after bind
        self.local_ports: dict[int, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._startup_error: BaseException | None = None
        self._stopped = False
        # True once the loop thread has confirmed it finished cleaning up.
        self.clean_stop = False

    # ---- public sync API ------------------------------------------------
    @property
    def started(self) -> bool:
        return (
            self._thread is not None
            and self._startup_error is None
            and not self._stopped
        )

    @property
    def server_port(self) -> int:
        return self._local_port_for(GUEST_SERVER_PORT)

    @property
    def cdp_port(self) -> int:
        return self._local_port_for(GUEST_CDP_PORT)

    @property
    def vlc_port(self) -> int:
        return self._local_port_for(GUEST_VLC_PORT)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("bridge already started")
        if not self._task_ports:
            logger.info(
                "no task-service listeners configured (OSWORLD_TASK_SERVICE_PORTS "
                "empty); task 082 needs 3000, see README"
            )
        # Registered before the thread exists: an interruption below (SIGTERM,
        # the deadline alarm, Ctrl-C) leaves the caller with no bridge object to
        # close, so stop() must already be wired to the interpreter's exit.
        atexit.register(self.stop)
        self._thread = threading.Thread(
            target=self._run, name="e2b-bridge", daemon=True
        )
        self._thread.start()
        try:
            # bind + create + readiness gate, with headroom for the create call
            if not self._ready.wait(timeout=self.config.ready_timeout_s + 600):
                raise TimeoutError("bridge did not become ready")
            if self._startup_error is not None:
                error, self._startup_error = self._startup_error, None
                raise error
        except BaseException:
            # Including a signal-derived exception: the loop thread is still
            # creating the first guest and nobody else will reap it.
            self.stop()
            raise

    def reset(self, snapshot_name: str | None = None) -> dict:
        guest = self._call(
            "reset",
            self._manager.revert(snapshot_name),
            timeout=self.config.ready_timeout_s + 600,
        )
        return self._manager.public_state(guest)

    def save(self, name: str) -> str:
        return self._call(
            "save",
            self._manager.save_snapshot(name),
            timeout=self.config.ready_timeout_s + 600,
        )

    def check_volume(self, requested_gb: int) -> dict:
        return self._call(
            "check_volume", self._manager.check_volume(requested_gb), timeout=120
        )

    def state(self) -> dict:
        guest = self._call("state", self._manager.current(), timeout=15)
        return self._manager.public_state(guest)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Before the signal and the join: an in-flight create must reap the
        # sandbox it is building rather than hand it over to a stopping bridge.
        self._manager.request_stop()
        loop, thread, stop_event = self._loop, self._thread, self._stop_event
        if thread is None:
            self.clean_stop = True  # nothing was ever running
        else:
            join_timeout = self.config.ready_timeout_s + 600
            # The loop thread may be alive but not yet have assigned the loop
            # and the stop event, and the loop may already be closed (a bridge
            # that failed to start); either way the join below is what decides
            # whether cleanup finished.
            if loop is not None and stop_event is not None:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(stop_event.set)
            thread.join(timeout=join_timeout)
            self.clean_stop = not thread.is_alive()
            if not self.clean_stop:
                logger.error(
                    "bridge did not stop within %ss; campaign %s guest %s may "
                    "still be running",
                    join_timeout,
                    self.config.campaign_id,
                    self._manager.last_sandbox_id(),
                )
        with contextlib.suppress(Exception):
            atexit.unregister(self.stop)

    # ---- internals ------------------------------------------------------
    def _local_port_for(self, guest_port: int) -> int:
        for local, guest in self.local_ports.items():
            if guest == guest_port and local not in self._task_ports:
                return local
        raise RuntimeError("bridge is not started")

    def _call(self, name: str, coro, timeout: float):
        if self._stopped or self._loop is None:
            coro.close()
            raise GuestUnavailable("bridge is stopped")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            # concurrent.futures.TimeoutError is the builtin TimeoutError, so an
            # operation that timed out on its own lands here too; only an
            # unfinished future means our own wait ran out.
            if future.done():
                raise
            # Cancel it: a caller that gave up must not have the operation
            # complete unnoticed behind its back.
            future.cancel()
            raise TimeoutError(f"bridge {name} exceeded {timeout}s") from exc

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # noqa: BLE001 -- surfaced to start()
            if not self._ready.is_set():
                self._startup_error = exc
                self._ready.set()
            else:
                logger.warning("bridge loop ended with %s", exc)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stopped:
            # stop() ran before this thread published its loop and stop event,
            # so it had nothing to signal and is now waiting on the join; there
            # is no listener to bind and no guest to create.
            return
        runners: list[web.AppRunner] = []
        heartbeat: asyncio.Task | None = None
        try:
            for guest_port in (GUEST_SERVER_PORT, GUEST_CDP_PORT, GUEST_VLC_PORT):
                runners.append(await self._listen(0, guest_port))
            for local_port, guest_port in self._task_ports.items():
                runners.append(await self._listen(local_port, guest_port))
            await self._manager.replace()
            heartbeat = asyncio.create_task(self._manager.heartbeat())
            self._ready.set()
            await self._stop_event.wait()
        finally:
            # Each step is independent: a heartbeat that died of its own
            # exception, or a manager stop that raises, must not skip the guest
            # kill or the listener cleanup that follow it.
            if heartbeat is not None:
                heartbeat.cancel()
                # cancel() is a no-op on a task that already ended with an
                # exception, and awaiting such a task re-raises it; drain it
                # instead so the exception is retrieved and reported here.
                outcome = (await asyncio.gather(heartbeat, return_exceptions=True))[0]
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    logger.warning("heartbeat task ended with %s", outcome)
            try:
                await self._manager.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("guest manager did not stop cleanly: %s", exc)
            for runner in reversed(runners):
                try:
                    await runner.cleanup()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("proxy listener did not clean up: %s", exc)

    async def _listen(self, local_port: int, guest_port: int) -> web.AppRunner:
        app = web.Application(client_max_size=1024**3)
        app.router.add_route("*", "/{tail:.*}", self._proxy_handler(guest_port))
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", local_port).start()
        except OSError as exc:
            await runner.cleanup()
            if local_port != 0:
                raise RuntimeError(
                    f"task-service listener port {local_port} is already owned by "
                    f"another process ({exc}); under parallel envs give this env a "
                    f"unique local:guest OSWORLD_TASK_SERVICE_PORTS mapping"
                ) from exc
            raise
        bound = runner.addresses[0][1]
        self.local_ports[bound] = guest_port
        return runner

    def _proxy_handler(self, guest_port: int):
        manager = self._manager
        config = self.config

        async def handler(request: web.Request) -> web.StreamResponse:
            try:
                guest = await manager.current()
            except GuestUnavailable as exc:
                raise web.HTTPServiceUnavailable(text=str(exc)) from exc
            manager.touch()
            local_port = request.transport.get_extra_info("sockname")[1]
            target = f"https://{guest.hosts[guest_port]}{request.rel_url}"
            headers = _upstream_headers(request, guest)

            if (
                guest_port == GUEST_SERVER_PORT
                and request.method == "POST"
                and request.path == "/setup/upload"
            ):
                return await _direct_setup_upload(request, guest, config)

            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self._proxy_websocket(request, target, headers, guest_port)

            body = await request.read()
            timeout = aiohttp.ClientTimeout(total=config.http_timeout_s)
            try:
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.request(
                        request.method,
                        target,
                        headers=headers,
                        data=body or None,
                        allow_redirects=False,
                    ) as response,
                ):
                    payload = await response.read()
                    if guest_port == GUEST_CDP_PORT and "json" in response.headers.get(
                        "Content-Type", ""
                    ):
                        payload = _rewrite_cdp_host(payload, local_port)
                    excluded = {
                        "content-length",
                        "transfer-encoding",
                        "content-encoding",
                        "connection",
                        "e2b-traffic-access-token",
                    }
                    out_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower() not in excluded
                    }
                    return web.Response(
                        status=response.status, body=payload, headers=out_headers
                    )
            except TimeoutError as exc:
                raise web.HTTPGatewayTimeout(
                    text=f"upstream exceeded {config.http_timeout_s}s bridge timeout"
                ) from exc
            except aiohttp.ClientError as exc:
                raise web.HTTPBadGateway(
                    text=f"upstream request failed: {exc}"
                ) from exc

        return handler

    async def _proxy_websocket(
        self,
        request: web.Request,
        target: str,
        headers: dict[str, str],
        guest_port: int,
    ) -> web.StreamResponse:
        manager = self._manager
        downstream = web.WebSocketResponse(max_msg_size=0)
        await downstream.prepare(request)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                upstream = await _ws_connect_with_retry(
                    session,
                    target.replace("https://", "wss://", 1),
                    headers,
                    self.config.ws_connect_retry_s,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[bridge:%s] websocket connect failed: %s", guest_port, exc
                )
                await downstream.close(code=1011, message=b"upstream connect failed")
                return downstream

            async def upstream_to_downstream() -> None:
                async for message in upstream:
                    manager.touch()
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await downstream.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)

            async def downstream_to_upstream() -> None:
                async for message in downstream:
                    manager.touch()
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await upstream.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await upstream.send_bytes(message.data)

            tasks = [
                asyncio.create_task(upstream_to_downstream()),
                asyncio.create_task(downstream_to_upstream()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    logger.warning(
                        "[bridge:%s] websocket error: %s", guest_port, task.exception()
                    )
            await upstream.close()
            await downstream.close()
            return downstream
