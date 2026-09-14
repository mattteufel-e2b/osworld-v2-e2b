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
import json
import logging
import os
import re
import secrets
import tempfile
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
    guest_proxy_ports: str = "80,8090"
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
            guest_proxy_ports=env.get("GUEST_HOSTMAP_PORTS", "80,8090"),
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
    and start it as root on :80 and :8090. No-op unless both env vars are
    configured, so the pure boundary-layer path is untouched. Runs in a worker
    thread."""
    if not (config.guest_proxy_script and config.fleet_rules):
        return
    script = Path(config.guest_proxy_script).read_text()
    rules = _guest_proxy_runtime_json(Path(config.fleet_rules).read_text())
    sandbox.files.write("/opt/hostmap_proxy.py", script)
    sandbox.files.write("/opt/fleet_runtime.json", rules)
    sandbox.commands.run(
        "chmod 0700 /opt/hostmap_proxy.py && chmod 0600 /opt/fleet_runtime.json",
        user="root",
        timeout=15,
    )
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
    sandbox.commands.run(
        f"HOSTMAP_PORT={config.guest_proxy_ports} FLEET_RUNTIME_FILE=/opt/fleet_runtime.json "
        "python3 /opt/hostmap_proxy.py > /var/log/hostmap_proxy.log 2>&1",
        user="root",
        background=True,
        timeout=0,
    )
    logger.info("guest Host-mapping proxy installed on :%s", config.guest_proxy_ports)


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
            for key in ("traffic_token", "host_suffix", "public_host_suffix")
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
            for key in ("traffic_token", "host", "url", "ingress_host", "port")
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

    async def _create(self, generation: int, source: str | None = None) -> Guest:
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
            # disconnect, shutdown); the thread still completes and would leak
            # the sandbox. asyncio.run() waits for executor threads on exit, so
            # reaping here is guaranteed to run before the process dies.
            if self._stopped:
                sandbox.kill()
                raise RuntimeError(
                    f"relay stopping; reaped just-created sandbox {sandbox.sandbox_id}"
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
            new_guest = await self._create(generation, source)
            async with self._lock:
                # stop() may have run while _create was in flight (it only takes
                # _lock, which we release across the slow create); installing the
                # new guest now would orphan it, so reap it instead.
                if self._stopped:
                    try:
                        await asyncio.to_thread(new_guest.sandbox.kill)
                    except Exception as exc:
                        logger.warning(
                            "could not kill %s: %s", new_guest.sandbox_id, exc
                        )
                    raise GuestUnavailable("bridge is stopping")
                old_guest = self._guest
                self._guest = new_guest
                # The fresh sandbox starts with a full sandbox_timeout_s, so the
                # heartbeat owes it nothing until new traffic arrives.
                now = asyncio.get_running_loop().time()
                self._last_activity = now
                self._last_refresh = now
            if old_guest is not None:
                try:
                    await asyncio.to_thread(old_guest.sandbox.kill)
                except Exception as exc:
                    logger.warning("could not kill %s: %s", old_guest.sandbox_id, exc)
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
        """Verify that the running sandbox root filesystem satisfies OSWorld's
        provider-level ``volume_size`` request.

        E2B root capacity is fixed when the template is built, so this hook
        validates the actual restored filesystem rather than attaching a
        persistent Volume or attempting a provider-specific partition resize.
        """
        if (
            isinstance(requested_gb, bool)
            or not isinstance(requested_gb, int)
            or requested_gb <= 0
        ):
            raise ValueError("requested_gb must be a positive integer")
        guest = await self.current()
        self.touch()

        def check_sync() -> dict[str, int]:
            result = guest.sandbox.commands.run(
                "df -B1 --output=size / | tail -n 1",
                user="root",
                timeout=30,
            )
            if result.exit_code != 0:
                raise RuntimeError(
                    f"could not measure sandbox root capacity: {result.stderr or result.stdout}"
                )
            try:
                root_capacity_bytes = int((result.stdout or "").strip())
            except ValueError as exc:
                raise RuntimeError(
                    f"invalid root-capacity output from sandbox: {result.stdout!r}"
                ) from exc
            # OSWorld defines volume_size as GB, and E2B product disk sizes use
            # GB. Compare decimal bytes so an E2B 100 GB root satisfies an
            # OSWorld 100 GB request despite filesystem overhead making df's
            # binary-unit display smaller than 100 GiB.
            required_bytes = requested_gb * 1_000_000_000
            if root_capacity_bytes < required_bytes:
                actual_gb = root_capacity_bytes / 1_000_000_000
                raise RuntimeError(
                    f"OSWorld task requires {requested_gb} GB root capacity, "
                    f"but E2B sandbox {guest.sandbox_id} has {actual_gb:.2f} GB"
                )
            marker = guest.sandbox.commands.run(
                f"printf '%s\\n' {requested_gb} > /run/osworld-required-volume-gb",
                user="root",
                timeout=15,
            )
            if marker.exit_code != 0:
                raise RuntimeError(
                    f"could not record OSWorld volume requirement: {marker.stderr or marker.stdout}"
                )
            return {
                "requested_gb": requested_gb,
                "required_bytes": required_bytes,
                "root_capacity_bytes": root_capacity_bytes,
            }

        return await asyncio.to_thread(check_sync)

    async def revert(self, snapshot_name: str | None = None) -> Guest:
        """Replace the guest: from a saved snapshot if the name is known,
        otherwise from the immutable template (OSWorld's default revert names,
        e.g. "init_state", are never explicitly saved and mean base state)."""
        source = self._snapshots.get(snapshot_name) if snapshot_name else None
        return await self.replace(source)

    async def stop(self) -> None:
        async with self._lock:
            self._stopped = True
            guest, self._guest = self._guest, None
            snapshots = dict(self._snapshots)
            if not self._retain_snapshots:
                self._snapshots.clear()
        if guest is not None:
            try:
                await asyncio.to_thread(guest.sandbox.kill)
            except Exception as exc:
                logger.warning("could not kill %s: %s", guest.sandbox_id, exc)
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
