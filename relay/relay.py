#!/usr/bin/env python3
"""Secure, lifecycle-aware bridge between OSWorld and an E2B sandbox.

The bridge binds only to localhost. It creates the guest with public traffic
disabled, authenticates every E2B ingress request with the per-sandbox traffic
token, and replaces the sandbox on OSWorld snapshot reverts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web
from e2b import Sandbox
from e2b_policy import require_immutable_template_ref, sandbox_network_policy

TEMPLATE = os.environ.get("GUEST_TEMPLATE", "osworld-v2-gnome")

# Guest-side Host-mapping proxy (V2 addressing): the website/GitLab fleets live
# in separate sandboxes that E2B ingress only exposes as `<port>-<sbx>.e2b.app`,
# and ingress rejects an overridden Host header. So site URLs like
# http://mailhub.127.0.0.1.nip.io/ resolve (via nip.io) to loopback inside the
# guest and are served by a small proxy the relay installs at session start,
# which maps each Host to the right fleet ingress port + token. This is wired
# only when both env vars point at the proxy script and the fleet runtime file;
# with them unset the relay behaves exactly like the base boundary layer.
GUEST_PROXY_SCRIPT = os.environ.get("HOSTMAP_PROXY_SCRIPT")
GUEST_FLEET_RULES = os.environ.get("OSWORLD_FLEET_RULES")
WEBSITE_HOST_SUFFIX = os.environ.get("WEBSITE_HOST_SUFFIX", "127.0.0.1.nip.io")
SANDBOX_TIMEOUT_S = int(os.environ.get("SANDBOX_TIMEOUT_S", "3600"))
RELAY_HTTP_TIMEOUT_S = int(os.environ.get("RELAY_HTTP_TIMEOUT_S", "240"))
READY_TIMEOUT_S = int(os.environ.get("GUEST_READY_TIMEOUT_S", "180"))
# SANDBOX_TIMEOUT_S is an *idle* ceiling, not a task ceiling: the heartbeat below
# calls set_timeout(SANDBOX_TIMEOUT_S) on the current guest whenever guest-directed
# traffic flowed since the last refresh, so an active task (OSWorld-V2 medians run
# ~1.6 h, tails ~3 h — far past the 1 h default) stays alive indefinitely while an
# abandoned guest still expires within SANDBOX_TIMEOUT_S of its last activity.
# Clamped so the refresh cadence always fits several times inside the ceiling.
HEARTBEAT_INTERVAL_S = max(
    30,
    min(int(os.environ.get("SANDBOX_HEARTBEAT_INTERVAL_S", "300")), SANDBOX_TIMEOUT_S // 4),
)
# Control-plane retries are deliberately fixed and short. A later scheduled
# heartbeat remains available if all attempts fail.
HEARTBEAT_RETRY_ATTEMPTS = 3
HEARTBEAT_RETRY_DELAY_S = 2.0
SETUP_UPLOAD_RETRY_ATTEMPTS = 3
SETUP_UPLOAD_RETRY_DELAY_S = 1.0
# How long a websocket upgrade retries the upstream connect before giving up.
# Covers the resume window after create_snapshot (the capture pauses the guest,
# dropping live CDP sockets): a client that re-dials immediately connects as soon
# as the guest is back instead of getting an instant 1011.
WS_CONNECT_RETRY_S = int(os.environ.get("RELAY_WS_CONNECT_RETRY_S", "20"))
# OSWORLD_RELAY_PORT_BASE namespaces a relay instance so many can run at once
# (one per parallel rollout worker). It is an integer offset added to every
# *remapped* local listener -- the control channel and the 15000/19222/18080
# control/CDP/VLC ports -- so worker N binds a disjoint block. The default of 0
# reproduces today's single-instance port numbers byte-for-byte, so the base
# boundary-layer path (and every existing caller) is untouched. The E2B provider
# reads the same env var to build its matching localhost tuple + control URL.
PORT_BASE = int(os.environ.get("OSWORLD_RELAY_PORT_BASE", "0"))
CONTROL_PORT = int(os.environ.get("E2B_RELAY_CONTROL_PORT", "14999")) + PORT_BASE
# 5000/9222/8080 are OSWorld's control/CDP/VLC channels: DesktopEnv learns them
# from the provider tuple's remapped ports (15000/19222/18080, each shifted by
# PORT_BASE) and dials the relay locally. Some V2 task setup/evaluator code also
# dials guest services through the relay. OSWORLD_TASK_SERVICE_PORTS entries are
# either a backward-compatible literal `port` or an explicit `local:guest`
# mapping. The latter gives each worker a collision-free host listener without
# changing the service port visible inside its guest.
SERVER_LOCAL = 15000 + PORT_BASE
CDP_LOCAL = 19222 + PORT_BASE
VLC_LOCAL = 18080 + PORT_BASE
def _task_service_port_map(raw: str) -> dict[int, int]:
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
            raise ValueError(f"task-service ports must be between 1 and 65535: {entry!r}")
        if local in mappings:
            raise ValueError(f"duplicate local task-service port: {local}")
        mappings[local] = guest
    return mappings


TASK_SERVICE_PORT_MAP = _task_service_port_map(
    os.environ.get("OSWORLD_TASK_SERVICE_PORTS", "3000,8000")
)
# Compatibility alias used by diagnostics and bind-collision reporting.
LITERAL_PORTS = set(TASK_SERVICE_PORT_MAP)
PORT_MAP = {
    SERVER_LOCAL: 5000,
    CDP_LOCAL: 9222,
    VLC_LOCAL: 8080,
    **TASK_SERVICE_PORT_MAP,
}


@dataclass(frozen=True)
class Guest:
    sandbox: Sandbox
    sandbox_id: str
    traffic_token: str
    hosts: dict[int, str]
    generation: int
    source: str = "template"


# The guest proxy binds both :80 and :8090. The harness builds site URLs as
# `<site>.<WEBSITE_HOST_SUFFIX>` where the suffix carries :8090 (so the same URL
# works from the OSWorld host, where :80 needs elevation); serving :80 as well
# covers any port-less caller inside the guest. :8080 is reserved by VLC's own
# baked Lua HTTP interface (relay VLC channel 18080->8080 below), so the guest
# hostmap proxy must not double-book it -- see FIDELITY.md.
GUEST_PROXY_PORTS = os.environ.get("GUEST_HOSTMAP_PORTS", "80,8090")


def _install_guest_proxy(sandbox: Sandbox) -> None:
    """Upload the stdlib Host-mapping proxy + fleet routing file into the guest
    and start it as root on :80 and :8090. No-op unless both env vars are
    configured, so the pure boundary-layer path is untouched. Runs in a worker
    thread."""
    if not (GUEST_PROXY_SCRIPT and GUEST_FLEET_RULES):
        return
    script = Path(GUEST_PROXY_SCRIPT).read_text()
    rules = _guest_proxy_runtime_json(Path(GUEST_FLEET_RULES).read_text())
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
    portless_suffix = WEBSITE_HOST_SUFFIX.split(":")[0]
    probe = sandbox.commands.run(
        f"getent hosts mailhub.{portless_suffix} >/dev/null && echo ok || echo no",
        user="root",
        timeout=20,
    )
    if "ok" not in (probe.stdout or ""):
        hosts = _fleet_hostnames(rules)
        if hosts:
            entry = "127.0.0.1 " + " ".join(hosts)
            sandbox.commands.run(
                f"grep -q '{hosts[0]}' /etc/hosts || echo '{entry}' >> /etc/hosts",
                user="root",
                timeout=15,
            )
    sandbox.commands.run(
        f"HOSTMAP_PORT={GUEST_PROXY_PORTS} FLEET_RUNTIME_FILE=/opt/fleet_runtime.json "
        "python3 /opt/hostmap_proxy.py > /var/log/hostmap_proxy.log 2>&1",
        user="root",
        background=True,
        timeout=0,
    )
    print(f"[relay] guest Host-mapping proxy installed on :{GUEST_PROXY_PORTS}", file=sys.stderr)


def _guest_proxy_runtime_json(rules_json: str) -> str:
    """Return the minimum fleet routing document safe to place in a guest."""
    runtime = json.loads(rules_json)
    websites = runtime.get("websites") or {}
    gitlab = runtime.get("gitlab") or {}
    safe = {
        "websites": {
            key: websites[key]
            for key in ("host_suffix", "public_host_suffix")
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
            for key in ("host", "url", "ingress_host", "port")
            if key in gitlab
        },
    }
    return json.dumps(safe, separators=(",", ":"))


def _service_network_rules(rules_json: str) -> dict[str, list[dict[str, object]]]:
    """Build exact-ingress-host E2B rules for fleet traffic credentials."""
    runtime = json.loads(rules_json)
    rules: dict[str, list[dict[str, object]]] = {}

    def add_rule(ingress_host: object, token: object, label: str) -> None:
        if not isinstance(ingress_host, str) or not ingress_host:
            raise ValueError(f"{label} ingress_host is required")
        if not isinstance(token, str) or not token:
            raise ValueError(f"{label} traffic_token is required")
        rule = {
            "transform": {
                "headers": {"e2b-traffic-access-token": token},
            }
        }
        existing = rules.get(ingress_host)
        if existing is not None and existing != [rule]:
            raise ValueError(
                f"conflicting traffic tokens for ingress host {ingress_host!r}"
            )
        rules[ingress_host] = [rule]

    websites = runtime.get("websites") or {}
    website_token = websites.get("traffic_token")
    for site, info in (websites.get("sites") or {}).items():
        if not isinstance(info, dict):
            raise TypeError(f"website {site!r} route must be an object")
        add_rule(info.get("ingress_host"), website_token, f"website {site!r}")

    gitlab = runtime.get("gitlab") or {}
    if gitlab:
        add_rule(
            gitlab.get("ingress_host"), gitlab.get("traffic_token"), "gitlab route"
        )
    return rules


def _guest_network_policy() -> dict[str, object]:
    policy = sandbox_network_policy()
    if GUEST_FLEET_RULES:
        policy["rules"] = _service_network_rules(Path(GUEST_FLEET_RULES).read_text())
    return policy


def _fleet_hostnames(rules_json: str) -> list[str]:
    try:
        runtime = json.loads(rules_json)
    except json.JSONDecodeError:
        return []
    hosts: list[str] = []
    web_section = runtime.get("websites") or {}
    suffix = web_section.get("host_suffix", WEBSITE_HOST_SUFFIX)
    for site in web_section.get("sites") or {}:
        hosts.append(f"{site}.{suffix}")
    gl = runtime.get("gitlab") or {}
    if gl.get("host"):
        hosts.append(gl["host"])
    return hosts


async def _direct_setup_upload(request: web.Request, guest: Guest) -> web.Response:
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
            or not Path(destination).is_absolute()
            or staged_path is None
        ):
            raise web.HTTPBadRequest(
                text="absolute file_path and file_data are required"
            )

        for attempt in range(1, SETUP_UPLOAD_RETRY_ATTEMPTS + 1):
            try:
                with staged_path.open("rb") as payload:
                    await asyncio.to_thread(
                        guest.sandbox.files.write,
                        destination,
                        payload,
                        user="user",
                        request_timeout=RELAY_HTTP_TIMEOUT_S,
                        use_octet_stream=True,
                    )
                size = staged_path.stat().st_size
                return web.Response(text=f"File Uploaded: {size} bytes")
            except Exception as exc:
                if attempt == SETUP_UPLOAD_RETRY_ATTEMPTS:
                    raise web.HTTPBadGateway(
                        text="native E2B setup upload failed"
                    ) from exc
                await asyncio.sleep(SETUP_UPLOAD_RETRY_DELAY_S * attempt)
        raise AssertionError("unreachable")
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


class GuestManager:
    def __init__(self) -> None:
        self._guest: Guest | None = None
        self._lock = asyncio.Lock()
        self._replace_lock = asyncio.Lock()
        # Set once by stop() and never cleared: replace() re-checks it around its
        # slow create so a /stop or SIGTERM that lands mid-/reset cannot race a
        # freshly created sandbox into _guest after shutdown already killed
        # everything (the fresh sandbox would otherwise run out its full
        # SANDBOX_TIMEOUT_S with no owner).
        self._stopped = False
        # OSWorld snapshot name -> E2B snapshot id (memory + filesystem state).
        # Deliberately never deleted: snapshots persist in the E2B account past
        # the run so consumers can inspect a run's saved states afterwards; the
        # ids are surfaced via /save responses and the /state "snapshots" map.
        self._snapshots: dict[str, str] = {}
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
            if self._stopped or guest is None or self._last_activity <= self._last_refresh:
                return False
        for attempt in range(1, HEARTBEAT_RETRY_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(guest.sandbox.set_timeout, SANDBOX_TIMEOUT_S)
                break
            except Exception as exc:
                print(
                    f"[relay] warning: timeout refresh attempt {attempt}/"
                    f"{HEARTBEAT_RETRY_ATTEMPTS} failed for {guest.sandbox_id}: {exc}",
                    file=sys.stderr,
                )
                if attempt == HEARTBEAT_RETRY_ATTEMPTS:
                    return False
                await asyncio.sleep(HEARTBEAT_RETRY_DELAY_S * attempt)
        self._last_refresh = asyncio.get_running_loop().time()
        return True

    async def heartbeat(self) -> None:
        while not self._stopped:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            await self._heartbeat_once()

    async def current(self) -> Guest:
        async with self._lock:
            if self._guest is None:
                raise web.HTTPServiceUnavailable(text="guest is not ready")
            return self._guest

    async def _create(self, generation: int, source: str | None = None) -> Guest:
        def create_sync() -> Sandbox:
            template_or_snapshot = source or require_immutable_template_ref(
                TEMPLATE,
                "GUEST_TEMPLATE",
            )
            sandbox = Sandbox.create(
                template_or_snapshot,
                timeout=SANDBOX_TIMEOUT_S,
                secure=True,
                network=_guest_network_policy(),
                metadata={"workload": "osworld", "generation": str(generation)},
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
            hosts={port: sandbox.get_host(port) for port in set(PORT_MAP.values())},
            generation=generation,
            source=f"snapshot:{source}" if source else "template",
        )
        try:
            await self._wait_ready(guest)
            await asyncio.to_thread(_install_guest_proxy, sandbox)
        except BaseException:
            await asyncio.to_thread(sandbox.kill)
            raise
        return guest

    async def _wait_ready(self, guest: Guest) -> None:
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT_S
        headers = {"e2b-traffic-access-token": guest.traffic_token}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    url = f"https://{guest.hosts[5000]}/screen_size"
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
                    raise web.HTTPServiceUnavailable(text="relay is stopping")
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
                        print(
                            f"[relay] warning: could not kill {new_guest.sandbox_id}: {exc}",
                            file=sys.stderr,
                        )
                    raise web.HTTPServiceUnavailable(text="relay is stopping")
                old_guest = self._guest
                self._guest = new_guest
                # The fresh sandbox starts with a full SANDBOX_TIMEOUT_S, so the
                # heartbeat owes it nothing until new traffic arrives.
                now = asyncio.get_running_loop().time()
                self._last_activity = now
                self._last_refresh = now
            if old_guest is not None:
                try:
                    await asyncio.to_thread(old_guest.sandbox.kill)
                except Exception as exc:
                    print(
                        f"[relay] warning: could not kill {old_guest.sandbox_id}: {exc}",
                        file=sys.stderr,
                    )
        print(
            f"[relay] guest ready id={new_guest.sandbox_id} "
            f"generation={generation} template={TEMPLATE}",
            file=sys.stderr,
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
        print(
            f"[relay] snapshot saved name={name} id={info.snapshot_id} sandbox={guest.sandbox_id}",
            file=sys.stderr,
        )
        return info.snapshot_id

    async def check_volume(self, requested_gb: int) -> dict[str, int]:
        """Verify that the running sandbox root filesystem satisfies OSWorld's
        provider-level ``volume_size`` request.

        E2B root capacity is fixed when the template is built, so this hook
        validates the actual restored filesystem rather than attaching a
        persistent Volume or attempting a provider-specific partition resize.
        """
        if isinstance(requested_gb, bool) or not isinstance(requested_gb, int) or requested_gb <= 0:
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
        if guest is not None:
            try:
                await asyncio.to_thread(guest.sandbox.kill)
            except Exception as exc:
                print(f"[relay] warning: could not kill {guest.sandbox_id}: {exc}", file=sys.stderr)


manager = GuestManager()
stop_event = asyncio.Event()


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
    headers = {key: value for key, value in request.headers.items() if key.lower() not in excluded}
    headers["e2b-traffic-access-token"] = guest.traffic_token
    return headers


def _rewrite_cdp_host(payload: bytes, local_port: int) -> bytes:
    text = payload.decode("utf-8", "replace")
    text = re.sub(r"ws://[^/\"]+/devtools", f"ws://127.0.0.1:{local_port}/devtools", text)
    text = re.sub(r"\"host\":\s*\"[^\"]*\"", f'"host": "127.0.0.1:{local_port}"', text)
    return text.encode()


async def _ws_connect_with_retry(session: aiohttp.ClientSession, url: str, headers: dict[str, str]):
    """ws_connect, retrying for up to WS_CONNECT_RETRY_S before raising.

    A guest resuming from a create_snapshot pause (or mid-replace) refuses
    connections for a few seconds; a client re-dialing CDP in that window should
    connect as soon as the guest is back, not fail instantly."""
    deadline = asyncio.get_running_loop().time() + WS_CONNECT_RETRY_S
    while True:
        try:
            return await session.ws_connect(url, headers=headers, max_msg_size=0)
        except Exception:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(1)


def make_proxy_handler(local_port: int):
    remote_port = PORT_MAP[local_port]

    async def handler(request: web.Request) -> web.StreamResponse:
        guest = await manager.current()
        # Guest-directed traffic is the heartbeat's activity signal.
        manager.touch()
        target = f"https://{guest.hosts[remote_port]}{request.rel_url}"
        headers = _upstream_headers(request, guest)

        if (
            local_port == SERVER_LOCAL
            and request.method == "POST"
            and request.path == "/setup/upload"
        ):
            return await _direct_setup_upload(request, guest)

        if request.headers.get("Upgrade", "").lower() == "websocket":
            downstream = web.WebSocketResponse(max_msg_size=0)
            await downstream.prepare(request)
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                try:
                    upstream = await _ws_connect_with_retry(
                        session,
                        target.replace("https://", "wss://", 1),
                        headers,
                    )
                except Exception as exc:
                    print(f"[relay:{local_port}] websocket connect failed: {exc}", file=sys.stderr)
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
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        print(
                            f"[relay:{local_port}] websocket error: {task.exception()}",
                            file=sys.stderr,
                        )
                await upstream.close()
                await downstream.close()
                return downstream

        body = await request.read()
        timeout = aiohttp.ClientTimeout(total=RELAY_HTTP_TIMEOUT_S)
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
                if local_port == CDP_LOCAL and "json" in response.headers.get("Content-Type", ""):
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
                return web.Response(status=response.status, body=payload, headers=out_headers)
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(
                text=f"upstream exceeded {RELAY_HTTP_TIMEOUT_S}s relay timeout"
            ) from exc
        except aiohttp.ClientError as exc:
            raise web.HTTPBadGateway(text=f"upstream request failed: {exc}") from exc

    return handler


def _public_state(guest: Guest) -> dict[str, object]:
    return {
        "ready": True,
        "sandbox_id": guest.sandbox_id,
        "generation": guest.generation,
        "template": TEMPLATE,
        "source": guest.source,
        "sandbox_timeout_seconds": SANDBOX_TIMEOUT_S,
        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_S,
        "restricted_ingress": True,
        "task_service_ports": sorted(LITERAL_PORTS),
        "task_service_port_map": {
            str(local): guest for local, guest in sorted(TASK_SERVICE_PORT_MAP.items())
        },
        # Persistent E2B snapshot ids for this run's save_state names. They are
        # never auto-deleted; consumers record them from here (or the /save
        # response) to inspect or clean up a run's snapshots afterwards.
        "snapshots": manager.snapshot_ids(),
    }


async def health(_: web.Request) -> web.Response:
    return web.json_response(_public_state(await manager.current()))


def _log_background_replace(task: asyncio.Task[Guest]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[relay] shielded replace failed: {exc}", file=sys.stderr)


async def reset(request: web.Request) -> web.Response:
    snapshot_name = None
    if request.can_read_body:
        try:
            snapshot_name = (await request.json()).get("snapshot")
        except Exception as exc:
            raise web.HTTPBadRequest(
                text='reset body must be JSON like {"snapshot": "name"}'
            ) from exc
    # A client disconnect (e.g. the provider's urlopen timing out) cancels this
    # handler mid-swap; shield the revert so it always runs to completion --
    # otherwise the just-created sandbox is abandoned unkilled and unowned.
    task = asyncio.ensure_future(manager.revert(snapshot_name))
    task.add_done_callback(_log_background_replace)
    return web.json_response(_public_state(await asyncio.shield(task)))


async def save(request: web.Request) -> web.Response:
    try:
        name = (await request.json()).get("name")
    except Exception:
        name = None
    if not name:
        raise web.HTTPBadRequest(text='save body must be JSON like {"name": "snapshot-name"}')
    snapshot_id = await manager.save_snapshot(name)
    state = _public_state(await manager.current())
    return web.json_response({"saved": name, "snapshot_id": snapshot_id, **state})


async def volume(request: web.Request) -> web.Response:
    try:
        requested_gb = (await request.json())["requested_gb"]
        result = await manager.check_volume(requested_gb)
    except (KeyError, TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    except RuntimeError as exc:
        raise web.HTTPInsufficientStorage(text=str(exc)) from exc
    return web.json_response(result)


async def stop(_: web.Request) -> web.Response:
    # Shutdown must work even when no guest is live (e.g. a failed replace left
    # the manager empty); report the sandbox id when there is one.
    sandbox_id = None
    with contextlib.suppress(web.HTTPServiceUnavailable):
        sandbox_id = (await manager.current()).sandbox_id
    stop_event.set()
    return web.json_response({"stopping": True, "sandbox_id": sandbox_id})


async def main() -> None:
    await manager.replace()
    heartbeat = asyncio.create_task(manager.heartbeat())
    runners: list[web.AppRunner] = []
    try:
        for local_port in PORT_MAP:
            app = web.Application(client_max_size=1024**3)
            app.router.add_route("*", "/{tail:.*}", make_proxy_handler(local_port))
            runner = web.AppRunner(app)
            await runner.setup()
            # Every configured listener must bind, including task-service
            # listeners: they are explicit config now
            # (OSWORLD_TASK_SERVICE_PORTS, empty under parallel waves), and a
            # collision means another relay would silently serve this worker's
            # task-service dials from the WRONG guest -- fail loudly instead.
            try:
                await web.TCPSite(runner, "127.0.0.1", local_port).start()
            except OSError as exc:
                if local_port in LITERAL_PORTS:
                    raise RuntimeError(
                        f"task-service listener port {local_port} is already owned by "
                        f"another process ({exc}); under parallel relays set "
                        f"choose a unique local:guest OSWORLD_TASK_SERVICE_PORTS "
                        f"mapping instead of reaching another worker's guest"
                    ) from exc
                raise
            runners.append(runner)

        control = web.Application()
        control.router.add_get("/health", health)
        control.router.add_get("/state", health)
        control.router.add_post("/reset", reset)
        control.router.add_post("/save", save)
        control.router.add_post("/volume", volume)
        control.router.add_post("/stop", stop)
        control_runner = web.AppRunner(control)
        await control_runner.setup()
        await web.TCPSite(control_runner, "127.0.0.1", CONTROL_PORT).start()
        runners.append(control_runner)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        print(
            json.dumps({"event": "RELAY_READY", **_public_state(await manager.current())}),
            file=sys.stderr,
        )
        await stop_event.wait()
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await manager.stop()
        for runner in reversed(runners):
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
