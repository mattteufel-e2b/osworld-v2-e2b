#!/usr/bin/env python3
"""Host-mapping proxy for the OSWorld-V2 service fleet.

Binds 127.0.0.1:<HOSTMAP_PORT> (default 80) and maps each incoming request's
`Host` header to the fleet sandbox ingress endpoint that actually serves it:

    Host: mailhub.127.0.0.1.nip.io   ->  https://<port>-<websites-sbx>.e2b.app
    Host: gitlab.127.0.0.1.nip.io    ->  https://<port>-<gitlab-sbx>.e2b.app

Why this shape (see out/osworld-v2-evidence/services-websites.json): E2B ingress
only accepts the HTTP Host header `<port>-<sandbox>.e2b.app` for the connected
sandbox host and rejects an overridden Host at the edge ("Invalid host"). So the
proxy must rewrite the outbound Host to the *ingress* host (not the site name)
and select the correct site purely by the sandbox PORT — which is why the fleet
publishes one distinct port per site. The site identity lives in the incoming
Host header; the sandbox routing lives in the port we dial.

The same script runs in two places, both reading the same runtime file:
  * host-side: launched by the service launchers (best-effort; port 80 needs
    elevation on macOS).
  * guest-side: uploaded and started as root by the relay at session start
    (port 80 binds fine as root inside the guest).

Stdlib only, so it runs unchanged inside a guest sandbox.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import ssl
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from e2b import Sandbox as _E2BSandbox
except ImportError:  # guest copy intentionally has no E2B control-plane SDK
    _E2BSandbox = None

RUNTIME_FILE = Path(
    os.environ.get(
        "FLEET_RUNTIME_FILE", str(Path(__file__).resolve().parent / ".runtime.json")
    )
)
HOST_SUFFIX = os.environ.get("WEBSITE_HOST_SUFFIX", "127.0.0.1.nip.io")

# Ingress presents Google-managed *.e2b.app certs; verification is fine.
_SSL_CTX = ssl.create_default_context()
_DIRECT_BODY_THRESHOLD = 900_000
# Hop-by-hop headers must not be forwarded.
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return redirects to the benchmark client instead of consuming them.

    Stateful sites set their session cookie on a 3xx response. If urllib follows
    that redirect inside this proxy, the outer requests.Session never receives
    the cookie before the follow-up request and authenticated login silently
    falls back to the login page.
    """

    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_URL_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CTX),
    _NoRedirect(),
)


def _open_upstream(request: urllib.request.Request):
    """Open an ingress request, retrying short-lived transport failures.

    A high-concurrency task wave can make the E2B ingress edge close a socket
    while urllib is writing it. OSWorld setup already retries the whole state
    operation after a 502; doing the same three times here avoids needlessly
    rebuilding a fresh guest for a transient edge connection.
    """
    for attempt in range(3):
        try:
            return _URL_OPENER.open(request, timeout=120)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, OSError):
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


class _DirectResponse:
    def __init__(self, payload: dict):
        self.status = int(payload["status"])
        self.headers = _HeaderList(payload.get("headers", []))
        self._body = base64.b64decode(payload.get("body_base64", ""))

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _HeaderList:
    def __init__(self, pairs):
        self._pairs = [(str(key), str(value)) for key, value in pairs]

    def items(self):
        return list(self._pairs)


def _open_direct(target: dict, method: str, path: str, headers, body: bytes):
    """Send a large host-side control-plane request inside the fleet guest.

    E2B ingress is retained for ordinary traffic. Large state JSON can exceed
    its request-body boundary, so the authenticated host proxy writes a request
    envelope through the E2B SDK and performs the localhost HTTP request from
    inside the already-restricted fleet sandbox. The guest-side proxy cannot do
    this because it deliberately has no E2B API key or control-plane SDK.
    """
    if _E2BSandbox is None:
        raise RuntimeError("large-body bridge requires the E2B SDK on the host proxy")
    sandbox_id = str(target["sandbox_id"])
    port = int(target["port"])
    request_headers = {
        str(key): str(value)
        for key, value in headers.items()
        if key.lower() not in _HOP
    }
    envelope = {
        "method": method,
        "path": path,
        "port": port,
        "headers": request_headers,
        "body_base64": base64.b64encode(body).decode("ascii"),
    }
    request_path = f"/tmp/osworld-hostmap-{uuid.uuid4().hex}.json"
    sandbox = _E2BSandbox.connect(sandbox_id)
    sandbox.files.write(request_path, json.dumps(envelope))
    remote_code = f"""
import base64, http.client, json
request = json.load(open({request_path!r}))
body = base64.b64decode(request["body_base64"])
connection = http.client.HTTPConnection("127.0.0.1", int(request["port"]), timeout=120)
connection.request(request["method"], request["path"], body=body, headers=request["headers"])
response = connection.getresponse()
payload = {{
    "status": response.status,
    "headers": list(response.getheaders()),
    "body_base64": base64.b64encode(response.read()).decode("ascii"),
}}
print(json.dumps(payload, separators=(",", ":")))
"""
    encoded_code = base64.b64encode(remote_code.encode()).decode("ascii")
    try:
        result = sandbox.commands.run(
            f"python3 -c \"import base64;exec(base64.b64decode('{encoded_code}'))\"",
            timeout=150,
        )
        lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("large-body bridge returned no response metadata")
        return _DirectResponse(json.loads(lines[-1]))
    finally:
        with contextlib.suppress(Exception):
            sandbox.files.remove(request_path)


def _rewrite_absolute_site_urls(payload: bytes, host: str, authority: str) -> bytes:
    """Keep absolute URLs on the same listener the client used.

    The service fleet's canonical URL is portless because guest browsers use
    the root-owned proxy on port 80. The macOS host proxy listens on 8090, so
    setup APIs that return an absolute activation URL must retain that port.
    """
    if not host or not authority or authority == host:
        return payload
    target = f"http://{authority}/".encode()
    return payload.replace(f"http://{host}/".encode(), target).replace(
        f"https://{host}/".encode(), target
    )


def _rewrite_location(value: str, host: str, authority: str) -> str:
    return _rewrite_absolute_site_urls(value.encode(), host, authority).decode()


def _load_rules() -> dict:
    """Build {incoming_host_lower: (ingress_host, token)} from the runtime file.

    Rebuilt on every request so a relaunch of either service is picked up without
    restarting the proxy."""
    rules: dict[str, dict] = {}
    try:
        runtime = json.loads(RUNTIME_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return rules
    web = runtime.get("websites") or {}
    suffix = web.get("host_suffix", HOST_SUFFIX)
    token = web.get("traffic_token")
    sandbox_id = web.get("sandbox_id")
    for site, info in (web.get("sites") or {}).items():
        ingress = info.get("ingress_host")
        if ingress:
            rules[f"{site}.{suffix}".lower()] = {
                "ingress_host": ingress,
                "traffic_token": token,
                "sandbox_id": sandbox_id,
                "port": info.get("port"),
            }
    gl = runtime.get("gitlab") or {}
    if gl.get("host") and gl.get("ingress_host"):
        rules[gl["host"].lower()] = {
            "ingress_host": gl["ingress_host"],
            "traffic_token": gl.get("traffic_token"),
            "sandbox_id": gl.get("sandbox_id"),
            "port": gl.get("port"),
        }
    return rules


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _resolve(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return _load_rules().get(host), host

    def _proxy(self):
        target, host = self._resolve()
        if target is None:
            self.send_error(502, f"no fleet route for Host {host!r}")
            return
        ingress_host = target["ingress_host"]
        token = target.get("traffic_token")
        url = f"https://{ingress_host}{self.path}"
        incoming_authority = self.headers.get("Host") or host
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=self.command)
        for key, value in self.headers.items():
            if key.lower() in _HOP:
                continue
            req.add_header(key, value)
        req.add_header("Host", ingress_host)
        if token:
            req.add_header("e2b-traffic-access-token", token)

        try:
            direct = (
                len(body or b"") > _DIRECT_BODY_THRESHOLD
                and target.get("sandbox_id")
                and target.get("port")
                and _E2BSandbox is not None
            )
            response = (
                _open_direct(target, self.command, self.path, self.headers, body or b"")
                if direct
                else _open_upstream(req)
            )
            with response as resp:
                payload = _rewrite_absolute_site_urls(
                    resp.read(), host, incoming_authority
                )
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() in _HOP or key.lower() == "content-length":
                        continue
                    if key.lower() == "location":
                        value = _rewrite_location(value, host, incoming_authority)
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = _rewrite_absolute_site_urls(exc.read(), host, incoming_authority)
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() in _HOP or key.lower() == "content-length":
                    continue
                if key.lower() == "location":
                    value = _rewrite_location(value, host, incoming_authority)
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001
            self.send_error(502, f"upstream {ingress_host} failed: {exc}")

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy

    def log_message(self, *args):  # quiet
        pass


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    request_queue_size = 128


def main() -> int:
    # HOSTMAP_PORT may be a comma-separated list. V2's build_website_url composes
    # the harness URL as `<site>.<WEBSITE_HOST_SUFFIX>` with no explicit port, so
    # the launcher advertises a suffix that already carries :8090; the guest also
    # serves :80 for any port-less callers. Binding every listed port keeps both
    # the host path (8090 only — :80 needs elevation) and the guest path (80+8090)
    # working from one process. Guest port 8080 is deliberately never in this
    # list: it is reserved by VLC's own baked Lua HTTP interface (see
    # FIDELITY.md), so the hostmap proxy must not double-book it.
    ports = [
        int(p) for p in os.environ.get("HOSTMAP_PORT", "80").split(",") if p.strip()
    ]
    servers = []
    for port in ports:
        try:
            servers.append(ReusableThreadingHTTPServer(("127.0.0.1", port), Handler))
        except OSError as exc:
            # Covers both PermissionError (needs elevation, e.g. :80 on macOS)
            # and EADDRINUSE (another process already owns the port): either way
            # this one port is unusable, so skip it with a warning rather than
            # crashing the whole proxy -- the other listed ports still bind.
            print(
                f"[hostmap_proxy] cannot bind 127.0.0.1:{port} ({exc}); skipping it.",
                file=sys.stderr,
            )
    if not servers:
        print("[hostmap_proxy] no ports bound", file=sys.stderr)
        return 13
    bound = [s.server_address[1] for s in servers]
    print(
        f"[hostmap_proxy] listening on 127.0.0.1:{bound}; runtime={RUNTIME_FILE}",
        file=sys.stderr,
    )
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True) for s in servers[1:]
    ]
    for t in threads:
        t.start()
    with contextlib.suppress(KeyboardInterrupt):
        servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
