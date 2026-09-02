#!/usr/bin/env python3
"""Shared helpers for the OSWorld-V2 service-fleet launchers.

Both services/websites/launch.py and services/gitlab/launch.py stand up a
docker-compose stack inside a single long-lived E2B sandbox and expose it to the
OSWorld host + guest through a Host-mapping proxy. This module holds the pieces
they share: E2B key loading, the gitignored runtime file (services/.runtime.json)
that lets later tasks reuse the same fleet, sandbox reuse-or-create, dockerd
bring-up, and (re)starting the host-side Host-mapping proxy.

Addressing background (see out/osworld-v2-evidence/spike-ingress.json and the
services receipts): E2B ingress only accepts the HTTP Host header
`<port>-<sandbox>.e2b.app` for the connected sandbox host — an overridden Host is
rejected at the edge with "Invalid host" (probed directly, recorded in the
website receipt). So the site name cannot be carried in the Host header across
ingress; it must be carried in the PORT. Each service therefore publishes a
distinct sandbox port per site, and the Host-mapping proxy maps an incoming
`<site>.127.0.0.1.nip.io` Host to that site's `sandbox.get_host(<port>)`.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from e2b import Sandbox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from e2b_policy import require_immutable_template_ref, sandbox_network_policy  # noqa: E402

SERVICES_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVICES_DIR.parent
RUNTIME_FILE = SERVICES_DIR / ".runtime.json"
PROXY_SCRIPT = SERVICES_DIR / "hostmap_proxy.py"
PROXY_PIDFILE = SERVICES_DIR / ".hostmap_proxy.pid"

# Wildcard-DNS suffix: <name>.127.0.0.1.nip.io resolves to 127.0.0.1 on both the
# OSWorld host and inside a guest sandbox (verified in the website receipt), so
# site URLs land on the loopback Host-mapping proxy without any /etc/hosts edits.
HOST_SUFFIX = "127.0.0.1.nip.io"

SANDBOX_TIMEOUT_S = int(os.environ.get("FLEET_SANDBOX_TIMEOUT_S", str(6 * 3600)))

# The E2B SDK sets vCPU/RAM at template-build time (not per-create), so the
# fleet's 4 vCPU / 8192 MB sizing lives in this reusable base template. Docker is
# baked in so per-launch bring-up only has to start dockerd, not apt-install it.
FLEET_TEMPLATE_NAME = os.environ.get("FLEET_TEMPLATE_NAME", "osworld-v2-fleet-base")
FLEET_CPU = 4
FLEET_MEMORY_MB = 8192


def ensure_fleet_template() -> str:
    """Return the prebuilt immutable fleet reference required by launchers.

    Template construction is deliberately absent from the run path. Build it
    first with ``services/build_fleet_template.py`` and export the emitted
    ``FLEET_TEMPLATE=name:build_id`` value.
    """
    reference = require_immutable_template_ref(
        os.environ.get("FLEET_TEMPLATE"),
        "FLEET_TEMPLATE",
    )
    log(f"using immutable fleet template {reference}")
    return reference


def load_e2b_key() -> str:
    key = os.environ.get("E2B_API_KEY")
    if not key:
        env_local = REPO_ROOT / ".env.local"
        if env_local.is_file():
            for line in env_local.read_text().splitlines():
                if line.startswith("E2B_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        raise SystemExit("E2B_API_KEY not set and not found in .env.local")
    os.environ["E2B_API_KEY"] = key
    return key


def log(msg: str) -> None:
    print(f"[fleet] {msg}", file=sys.stderr, flush=True)


# --- runtime file -----------------------------------------------------------


def read_runtime() -> dict:
    if RUNTIME_FILE.is_file():
        try:
            return json.loads(RUNTIME_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_runtime_section(section: str, data: dict) -> dict:
    runtime = read_runtime()
    runtime[section] = data
    payload = json.dumps(runtime, indent=2, sort_keys=True) + "\n"
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RUNTIME_FILE.parent,
            prefix=f".{RUNTIME_FILE.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, RUNTIME_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return runtime


# --- sandbox lifecycle ------------------------------------------------------


def _connect(sandbox_id: str) -> Sandbox | None:
    """Reconnect to a still-running sandbox, or None if it is gone."""
    try:
        sbx = Sandbox.connect(sandbox_id)
        # Cheap liveness check.
        sbx.commands.run("true", timeout=15)
        return sbx
    except Exception as exc:  # noqa: BLE001
        log(f"cannot reuse sandbox {sandbox_id}: {exc}")
        return None


def reuse_or_create(section: str, *, template: str | None = None) -> tuple[Sandbox, bool]:
    """Return (sandbox, created). Reuse the sandbox recorded for `section` in the
    runtime file if it is still alive, otherwise create a fresh restricted-ingress
    sandbox from the sized fleet template and extend its timeout. vCPU/RAM come
    from the template (FLEET_CPU / FLEET_MEMORY_MB)."""
    template = require_immutable_template_ref(
        template or os.environ.get("FLEET_TEMPLATE"),
        "FLEET_TEMPLATE",
    )
    existing = read_runtime().get(section, {})
    sid = existing.get("sandbox_id")
    existing_template = existing.get("template")
    if sid and existing_template == template:
        sbx = _connect(sid)
        if sbx is not None:
            with contextlib.suppress(Exception):
                sbx.set_timeout(SANDBOX_TIMEOUT_S)
            log(f"reusing {section} sandbox {sid}")
            return sbx, False
    elif sid:
        log(
            f"not reusing {section} sandbox {sid}: runtime template "
            f"{existing_template!r} does not match {template!r}"
        )

    log(f"creating {section} sandbox from {template}")
    sbx = Sandbox.create(
        template,
        timeout=SANDBOX_TIMEOUT_S,
        secure=True,
        network=sandbox_network_policy(),
        metadata={"workload": "osworld-v2-services", "section": section},
    )
    if not getattr(sbx, "traffic_access_token", None):
        sbx.kill()
        raise RuntimeError("E2B did not return a traffic access token for the fleet sandbox")
    log(f"created {section} sandbox {sbx.sandbox_id}")
    return sbx, True


def run(sbx: Sandbox, cmd: str, *, timeout: int = 120, check: bool = True, quiet: bool = False):
    if not quiet:
        log(f"$ {cmd if len(cmd) < 160 else cmd[:157] + '...'}")
    res = sbx.commands.run(cmd, user="root", timeout=timeout)
    if check and res.exit_code != 0:
        raise RuntimeError(
            f"command failed (exit {res.exit_code}): {cmd}\n"
            f"STDOUT:{res.stdout[-2000:]}\nSTDERR:{res.stderr[-2000:]}"
        )
    return res


def ensure_swap(sbx: Sandbox, gb: int = 8) -> None:
    """Add a swap file so a heavy parallel docker build can't OOM-kill the box
    (23 web images in 8 GB RAM otherwise wedges envd). Idempotent."""
    have = sbx.commands.run("swapon --show=NAME --noheadings | head -1", user="root", timeout=20)
    if (have.stdout or "").strip():
        return
    log(f"adding {gb}G swap")
    sbx.commands.run(
        f"fallocate -l {gb}G /swapfile && chmod 600 /swapfile "
        "&& mkswap /swapfile && swapon /swapfile",
        user="root",
        timeout=120,
    )


def poll_cmd(
    sbx: Sandbox,
    cmd: str,
    *,
    timeout: int = 30,
    retries: int = 6,
    envs: dict[str, str] | None = None,
) -> str | None:
    """Run a short command tolerating transient envd unresponsiveness (the
    command daemon can stall while a 23-image docker build saturates the box).
    Returns stdout, or None if every attempt timed out."""
    for _ in range(retries):
        try:
            return (
                sbx.commands.run(
                    cmd,
                    user="root",
                    timeout=timeout,
                    request_timeout=90,
                    envs=envs,
                ).stdout
                or ""
            )
        except Exception:  # noqa: BLE001
            time.sleep(15)
    return None


def ensure_docker(sbx: Sandbox) -> float:
    """Install docker + compose-v2 (if missing) and make sure dockerd is up.
    Returns seconds spent. Idempotent."""
    t0 = time.time()
    have = sbx.commands.run(
        "command -v docker >/dev/null "
        "&& docker compose version >/dev/null 2>&1 "
        "&& echo yes || echo no",
        user="root",
        timeout=30,
    )
    if "yes" not in (have.stdout or ""):
        log("installing docker.io + docker-compose-v2 (base template has no docker)")
        run(sbx, "apt-get update -qq", timeout=300)
        run(
            sbx,
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "docker.io docker-compose-v2 git >/dev/null",
            timeout=600,
        )
    # Start dockerd if the socket is not answering (no systemd in the base template).
    up = sbx.commands.run(
        "docker info >/dev/null 2>&1 && echo up || echo down", user="root", timeout=30
    )
    if "up" not in (up.stdout or ""):
        log("starting dockerd")
        run(sbx, "nohup dockerd >/var/log/dockerd.log 2>&1 & disown", timeout=30, check=False)
        deadline = time.time() + 90
        while time.time() < deadline:
            chk = sbx.commands.run(
                "docker info >/dev/null 2>&1 && echo up || echo down", user="root", timeout=30
            )
            if "up" in (chk.stdout or ""):
                break
            time.sleep(3)
        else:
            tail = sbx.commands.run("tail -30 /var/log/dockerd.log", user="root", timeout=15)
            raise RuntimeError(f"dockerd did not come up:\n{tail.stdout}\n{tail.stderr}")
    log(f"docker ready in {time.time() - t0:.0f}s")
    return time.time() - t0


# --- host-side Host-mapping proxy ------------------------------------------


def restart_host_proxy() -> dict:
    """(Re)start the host-side Host-mapping proxy from the current runtime file.
    Best-effort: on this macOS host, binding 127.0.0.1:80 needs elevation, so a
    PermissionError is reported (not fatal) — the functional guest-side proxy is
    installed as root inside the guest by the relay at session start."""
    # Stop any previous instance.
    if PROXY_PIDFILE.is_file():
        try:
            old = int(PROXY_PIDFILE.read_text().strip())
            os.kill(old, 15)
        except (ProcessLookupError, ValueError):
            pass
        PROXY_PIDFILE.unlink(missing_ok=True)

    ports_env = os.environ.get("HOSTMAP_PORT", "8090")
    ports = [int(p) for p in ports_env.split(",") if p.strip()]
    # Advertised port = the one embedded in WEBSITE_HOST_SUFFIX / GITLAB_URL, i.e.
    # the highest listed (8090), since :80 needs host elevation the harness won't
    # have. 8090, not 8080: guest port 8080 is reserved by VLC's own baked Lua
    # HTTP interface, and the guest hostmap proxy must not double-book it.
    advertised = max(ports)
    logfile = SERVICES_DIR / ".hostmap_proxy.log"
    with logfile.open("w") as output:
        proc = subprocess.Popen(
            [sys.executable, str(PROXY_SCRIPT)],
            stdout=output,
            stderr=subprocess.STDOUT,
            env={**os.environ, "HOSTMAP_PORT": ports_env},
        )
    time.sleep(1.5)
    if proc.poll() is not None:
        return {
            "running": False,
            "port": advertised,
            "ports": ports,
            "pid": None,
            "note": f"host proxy exited immediately (see {logfile.name})",
        }
    # Confirm the advertised port actually accepts connections.
    bound = False
    import socket

    for _ in range(10):
        try:
            with socket.create_connection(("127.0.0.1", advertised), timeout=1):
                bound = True
                break
        except OSError:
            time.sleep(0.3)
    PROXY_PIDFILE.write_text(str(proc.pid))
    return {"running": bound, "port": advertised, "ports": ports, "pid": proc.pid}


def verify_host_proxy_path(sample_host: str, path: str, port: int) -> dict:
    """Drive the full Host-mapping proxy path from the host: GET
    http://<sample_host>:<port><path>. nip.io resolves <sample_host> to
    127.0.0.1, so this exercises the proxy's Host->fleet-ingress routing."""
    import urllib.request

    url = f"http://{sample_host}:{port}{path}"
    try:
        req = urllib.request.Request(url)  # Host header defaults to sample_host
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(300).decode("utf-8", "replace")
            return {"url": url, "status": resp.status, "body_prefix": body}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "status": None, "error": repr(exc)[:200]}
