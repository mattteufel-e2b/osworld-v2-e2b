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

When `HOSTMAP_TLS_CERT`/`HOSTMAP_TLS_KEY` are set, the ports listed in
`HOSTMAP_TLS_PORTS` (a subset of `HOSTMAP_PORT`) terminate TLS with that
cert/key pair, and every other listed port stops proxying and instead answers
every request with a portless `301 Location: https://<host><path>` redirect,
so a guest's plain :80 lands on its own :443 -- with no cert/key configured,
behaviour is unchanged. Absolute URLs and `Location` headers rewritten in
response bodies pick up whichever scheme (http/https) the client actually
used to reach this listener. GitLab task fixtures that hardcode a public
GitLab host are routed here as alias hosts (`gitlab.aliases` in the runtime
file); an alias rule carries `canonical_host` so GitLab's own canonical
absolute URLs get rewritten back to the alias authority the client used
instead of our real GitLab host. `/api/state` request bodies also get any
dead upstream task-asset URL (`websites.asset_url_map`) rewritten to the
fleet-served replacement before they are forwarded.

Stdlib only, so it runs unchanged inside a guest sandbox.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
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
_PAGES_HEADER = "X-OSWorld-Pages-Host"
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
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


def _rewrite_absolute_site_urls(
    payload: bytes, host: str, authority: str, scheme: str = "http"
) -> bytes:
    """Keep absolute URLs on the same listener (authority) and scheme the client used.

    The service fleet's canonical URL is portless because guest browsers use
    the root-owned proxy on port 80/443. The macOS host proxy listens on 8090,
    so setup APIs that return an absolute activation URL must retain that
    port. Once TLS is enabled, rewritten URLs must also switch to `https://`
    to match the scheme the client actually used to reach this listener.
    """
    if not host or not authority or (authority == host and scheme == "http"):
        return payload
    target = f"{scheme}://{authority}/".encode()
    return payload.replace(f"http://{host}/".encode(), target).replace(
        f"https://{host}/".encode(), target
    )


def _rewrite_location(
    value: str, host: str, authority: str, scheme: str = "http"
) -> str:
    return _rewrite_absolute_site_urls(value.encode(), host, authority, scheme).decode()


def _rewrite_pages_urls(payload: bytes, suffix: str, authority: str) -> bytes:
    """Keep canonical Pages hosts reachable through the client's TLS listener.

    A GitLab API response can advertise a different Pages hostname, including
    when task 041 accessed GitLab through its alias. Only portless URLs under
    the configured GitLab host qualify; nested labels and other hosts do not.
    """
    if not suffix:
        return payload
    _, separator, port = authority.rpartition(":")
    port_suffix = (
        f":{port}" if separator and port.isdigit() and 0 < int(port) < 65536 else ""
    )
    pattern = (
        rb"https?://("
        + (_DNS_LABEL + r"\." + re.escape(suffix)).encode()
        + rb")(?=[/?#\s\"'<>]|$)"
    )
    return re.sub(
        pattern,
        lambda match: b"https://" + match[1].lower() + port_suffix.encode(),
        payload,
        flags=re.IGNORECASE,
    )


def _load_rules() -> tuple[dict[str, dict], dict[bytes, bytes]]:
    """Read the runtime file once and return (routes, asset_url_map).

    Routes are {incoming_host_lower: {ingress_host, traffic_token, ...}}; the
    asset map is websites.asset_url_map (dead task asset URLs -> fleet-served
    replacements) pre-encoded for `_map_asset_urls`. Both are rebuilt on every
    request so a relaunch of either service is picked up without restarting the
    proxy -- which is exactly why they are read together: one request must not
    parse the same file twice.
    """
    rules: dict[str, dict] = {}
    try:
        runtime = json.loads(RUNTIME_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return rules, {}
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
        gitlab_rule = {
            "ingress_host": gl["ingress_host"],
            "traffic_token": gl.get("traffic_token"),
            "sandbox_id": gl.get("sandbox_id"),
            "port": gl.get("port"),
            "pages_suffix": gl["host"].lower(),
        }
        rules[gl["host"].lower()] = gitlab_rule
        # Task 041 opens a hardcoded public GitLab host; route it to ours and
        # rewrite GitLab's canonical absolute URLs to the alias the client used.
        for alias in gl.get("aliases") or []:
            rules[str(alias).lower()] = {
                **gitlab_rule,
                "canonical_host": gl["host"].lower(),
            }
    asset_url_map = {
        str(dead).encode(): str(served).encode()
        for dead, served in (web.get("asset_url_map") or {}).items()
    }
    return rules, asset_url_map


def _map_asset_urls(body: bytes, path: str, mapping: dict[bytes, bytes]) -> bytes:
    """Only stateful-site seeding (/api/state) carries task asset links."""
    if not body or not mapping or not path.startswith("/api/state"):
        return body
    for old, new in mapping.items():
        body = body.replace(old, new)
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self):
        encoding = self.headers.get("Transfer-Encoding")
        if encoding is None:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative Content-Length")
            return self.rfile.read(length)
        if encoding.lower() != "chunked" or "Content-Length" in self.headers:
            raise ValueError("unsupported or ambiguous request framing")
        body = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if len(line) > 65536 or not line.endswith(b"\r\n"):
                raise ValueError("invalid chunk header")
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size < 0:
                raise ValueError("negative chunk size")
            if size == 0:
                # Consume trailers before accepting another keep-alive request.
                while True:
                    line = self.rfile.readline(65537)
                    if line == b"\r\n":
                        return bytes(body)
                    if len(line) > 65536 or not line.endswith(b"\r\n"):
                        raise ValueError("invalid chunk trailer")
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                raise ValueError("incomplete chunk")
            body.extend(chunk)

    def _resolve(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        rules, asset_url_map = _load_rules()
        target = rules.get(host)
        if target is None and len(host) <= 253:
            for rule in rules.values():
                suffix = rule.get("pages_suffix")
                if suffix and re.fullmatch(
                    _DNS_LABEL + r"\." + re.escape(suffix), host
                ):
                    target = {**rule, "pages_host": host}
                    break
        return target, host, asset_url_map

    def _scheme(self) -> str:
        return "https" if isinstance(self.connection, ssl.SSLSocket) else "http"

    def _relay(
        self, status, headers, payload, rewrite_host, authority, pages_suffix=""
    ):
        """Send one upstream response (success or HTTPError) back to the client.

        A HEAD reply must keep upstream's Content-Length -- it describes the
        body a GET would return -- and must not carry a body of its own;
        recomputing it from the empty HEAD payload reports every resource as
        zero bytes.
        """
        scheme = self._scheme()
        payload = _rewrite_pages_urls(payload, pages_suffix, authority)
        head = self.command == "HEAD"
        upstream_length = None
        self.send_response(status)
        for key, value in headers.items():
            lower = key.lower()
            if lower == "content-length":
                upstream_length = value
                continue
            if lower in _HOP:
                continue
            if lower == "location":
                value = _rewrite_location(value, rewrite_host, authority, scheme)
                value = _rewrite_pages_urls(
                    value.encode(), pages_suffix, authority
                ).decode()
            self.send_header(key, value)
        self.send_header(
            "Content-Length", (upstream_length or "0") if head else str(len(payload))
        )
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def _log_failure(self, status, host, reason):
        """One stderr line per 5xx this handler synthesises or relays.

        `log_message` stays a no-op, so a healthy request is still silent; a
        proxy that starts failing mid-campaign otherwise answered 502/504 with
        nothing in its own log and every worker blamed its own guest.
        """
        print(
            f"[hostmap_proxy] {status} {self.command} {host}{self.path}: {reason}",
            file=sys.stderr,
            flush=True,
        )

    def _proxy(self):
        if self.server.redirect_to_https:
            host = (self.headers.get("Host") or "").split(":")[0]
            self.send_response(301)
            self.send_header("Location", f"https://{host}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        target, host, asset_url_map = self._resolve()
        if target is None:
            reason = f"no fleet route for Host {host!r}"
            self._log_failure(502, host, reason)
            self.send_error(502, reason)
            return
        scheme = self._scheme()
        # Pages keeps its own canonical domain; the alias rule only applies to
        # GitLab itself. Pages URL scheme/port rewriting happens in _relay.
        rewrite_host = (
            "" if target.get("pages_host") else target.get("canonical_host", host)
        )
        ingress_host = target["ingress_host"]
        token = target.get("traffic_token")
        url = f"https://{ingress_host}{self.path}"
        incoming_authority = self.headers.get("Host") or host
        try:
            body = self._read_body()
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        body = _map_asset_urls(body or b"", self.path, asset_url_map) or None

        # The fanout trusts this routing header only after hostname validation.
        # Strip every client spelling before supplying our own on Pages routes.
        forward_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP and key.lower() != _PAGES_HEADER.lower()
        }
        if target.get("pages_host"):
            forward_headers[_PAGES_HEADER] = target["pages_host"]
        req = urllib.request.Request(url, data=body, method=self.command)
        for key, value in forward_headers.items():
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
                _open_direct(
                    target, self.command, self.path, forward_headers, body or b""
                )
                if direct
                else _open_upstream(req)
            )
            with response as resp:
                payload = _rewrite_absolute_site_urls(
                    resp.read(), rewrite_host, incoming_authority, scheme
                )
                self._relay(
                    resp.status,
                    resp.headers,
                    payload,
                    rewrite_host,
                    incoming_authority,
                    target.get("pages_suffix", ""),
                )
        except urllib.error.HTTPError as exc:
            payload = _rewrite_absolute_site_urls(
                exc.read(), rewrite_host, incoming_authority, scheme
            )
            if exc.code >= 500:
                self._log_failure(
                    exc.code, host, f"upstream {ingress_host}: {exc.reason}"
                )
            self._relay(
                exc.code,
                exc.headers,
                payload,
                rewrite_host,
                incoming_authority,
                target.get("pages_suffix", ""),
            )
        except Exception as exc:  # noqa: BLE001
            # The body size is part of the diagnosis: a guest-originated
            # /api/state write large enough to trip E2B ingress's request-body
            # boundary fails here and nowhere else.
            reason = (
                f"upstream {ingress_host} failed "
                f"({len(body or b'')}-byte request body): {exc}"
            )
            self._log_failure(502, host, reason)
            self.send_error(502, reason)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy

    def log_message(self, *args):  # quiet
        pass


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    request_queue_size = 128
    redirect_to_https = False  # per-instance override set by make_server
    tls_context: ssl.SSLContext | None = None  # ditto
    handshake_timeout = 30

    def get_request(self):
        """Accept a connection without performing the TLS handshake here.

        socketserver runs `get_request()` on the single accept thread, so
        wrapping the *listening* socket would make every handshake serialise
        there with no timeout: one silent TCP connection (Chrome preconnects
        constantly) wedges the listener permanently. Wrapping the accepted
        socket with `do_handshake_on_connect=False` defers the handshake to
        the per-connection thread's first read, bounded by a socket timeout.
        """
        sock, addr = self.socket.accept()
        if self.tls_context is None:
            return sock, addr
        sock.settimeout(self.handshake_timeout)
        return (
            self.tls_context.wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False
            ),
            addr,
        )

    def handle_error(self, request, client_address):
        """Stay quiet about one client's broken connection.

        Plain HTTP bytes sent to the TLS port, a client that disappears, or
        one that never completes the handshake all raise on the connection
        thread; none of them is a proxy fault worth a traceback.
        """
        if isinstance(sys.exc_info()[1], (ssl.SSLError, TimeoutError, ConnectionError)):
            return
        super().handle_error(request, client_address)


def make_server(
    port: int,
    tls: tuple[str, str] | None,
    redirect_to_https: bool = False,
) -> ReusableThreadingHTTPServer:
    """Build one listener: plain HTTP, a TLS terminator, or an HTTPS-redirector.

    `tls` is a (certfile, keyfile) pair; when given, the context is stored on
    the server so each accepted connection terminates HTTPS on its own thread
    (see `ReusableThreadingHTTPServer.get_request`). `redirect_to_https` marks
    a plain listener that must answer every request with a 301 to the same
    path on `https://` instead of proxying it.
    """
    server = ReusableThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.redirect_to_https = redirect_to_https
    if tls is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=tls[0], keyfile=tls[1])
        server.tls_context = context
    return server


def main() -> int:
    # HOSTMAP_PORT may be a comma-separated list. V2's build_website_url composes
    # the harness URL as `<site>.<WEBSITE_HOST_SUFFIX>` with no explicit port, so
    # the launcher advertises a suffix that already carries :8090; the guest also
    # serves :80 for any port-less callers. Binding every listed port keeps both
    # the host path (8090 only — :80 needs elevation) and the guest path (80+8090)
    # working from one process. Guest port 8080 is deliberately never in this
    # list: it is reserved by VLC's own baked Lua HTTP interface (see
    # FIDELITY.md), so the hostmap proxy must not double-book it.
    #
    # HOSTMAP_TLS_CERT/HOSTMAP_TLS_KEY name a leaf cert/key pair (see
    # campaign_tls.py); when both are set, every port listed in
    # HOSTMAP_TLS_PORTS (a subset of HOSTMAP_PORT) terminates TLS with that
    # pair, and every other listed port answers only a portless 301 redirect
    # to `https://<host><path>` so a guest's plain :80 lands on its own :443.
    # With no cert/key configured, every port serves plain HTTP exactly as
    # before -- required for the no-model validation ladder that runs ahead
    # of TLS support landing.
    ports = [
        int(p) for p in os.environ.get("HOSTMAP_PORT", "80").split(",") if p.strip()
    ]
    tls_ports = {
        int(p) for p in os.environ.get("HOSTMAP_TLS_PORTS", "").split(",") if p.strip()
    }
    cert = os.environ.get("HOSTMAP_TLS_CERT")
    key = os.environ.get("HOSTMAP_TLS_KEY")
    tls_enabled = bool(cert and key)
    servers = []
    for port in ports:
        try:
            servers.append(
                make_server(
                    port,
                    tls=(cert, key) if tls_enabled and port in tls_ports else None,
                    redirect_to_https=tls_enabled and port not in tls_ports,
                )
            )
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
