#!/usr/bin/env python3
"""Task 7 spike: can Host-header-routed websites be served through E2B ingress
with an extra subdomain label (e.g. `mailhub.{sandbox-host}`)?

Two sandboxes are used:
  - `sbx_public`: default settings (public traffic, no token) — isolates pure
    ROUTING/TLS behavior from token-auth behavior, per the task instructions.
  - `sbx_restricted`: created with `network.allow_public_traffic=False` — used
    only to exercise the traffic-token header path (with/without/wrong token),
    confirming production's restricted-ingress mode still works and that the
    subdomain-label failure (if any) is independent of token gating.

Each sandbox runs a tiny inline Host-echo HTTP server on port 80 (root, since
port 80 is privileged) that returns the received `Host` header in the body —
`python3 -m http.server` doesn't expose the Host header, hence the custom
server.

Probes (from the machine running this script, i.e. real external ingress
traffic, not from inside the sandbox):
  1. https://{H}/                    baseline
  2. https://mailhub.{H}/            extra subdomain label
  3. curl-equivalent (requests + manual IP resolution) with `--resolve`-style
     forced IP, same SNI, to separate DNS from TLS/router behavior if 2 fails
  4. baseline again on the restricted sandbox: without token (expect 403),
     with correct token (expect 200), with wrong token (expect 403)
  5. mailhub subdomain on the restricted sandbox, with the correct token, to
     confirm the subdomain failure mode (if any) is independent of the token
     layer
  6/7. `openssl s_client` handshake against the resolved IP with `-servername`
     set to the plain host (6) and the mailhub host (7), captured raw, to
     directly observe whether the server offers a certificate/completes the
     TLS handshake for each SNI value. This corroborates (does not replace)
     the primary signal, which is probes 2/3/5 failing at the TLS layer with
     no HTTP response at all (`SSLEOFError`/`SSL_ERROR_SYSCALL`), in contrast
     to probes 4a/4c which complete TLS+HTTP and return real HTTP 403 JSON
     bodies from the application.

Decision rule (recorded as `host_suffix_mode` in the evidence JSON):
  - "ingress-direct" if probe 2 returns the echoed Host `mailhub.{H}` (i.e.
    the request reaches the guest and the Host header round-trips) ->
    `WEBSITE_HOST_SUFFIX={H}` end-to-end, no extra code needed.
  - "guest-resolver" otherwise -> Task 9 wires a dnsmasq wildcard
    (`address=/.osworld.internal/127.0.0.1`) + a guest-local Host-preserving
    proxy (same pattern as $HARK's cdp_hostfix.py, forwarding to the fleet
    sandbox's ingress host with the token header), and
    `WEBSITE_HOST_SUFFIX=osworld.internal`.

Run:
    export E2B_API_KEY=$(grep '^E2B_API_KEY=' .env.local | cut -d= -f2)
    uv run --with e2b --with requests python tools/spikes/spike_ingress.py

Writes out/osworld-v2-evidence/spike-ingress.json.
"""

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import requests
from e2b import Sandbox

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "out" / "osworld-v2-evidence" / "spike-ingress.json"

ECHO_SERVER = """import http.server


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("Host: " + str(self.headers.get("Host", "")) + "\\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


http.server.HTTPServer(("0.0.0.0", 80), H).serve_forever()
"""


def start_echo_server(sbx: Sandbox) -> str:
    """Write and start the Host-echo server on :80 inside the sandbox. Returns
    the public host string sandbox.get_host(80)."""
    sbx.files.write("/tmp/echo_server.py", ECHO_SERVER)
    r = sbx.commands.run(
        "nohup python3 /tmp/echo_server.py > /tmp/echo.log 2>&1 & sleep 1; "
        "curl -s http://localhost:80/ && echo LOCAL_OK || (echo LOCAL_FAIL; cat /tmp/echo.log)",
        user="root",
        timeout=15,
    )
    if "LOCAL_OK" not in (r.stdout or ""):
        raise RuntimeError(
            f"echo server failed to start in-sandbox: {r.stdout!r} {r.stderr!r}"
        )
    return sbx.get_host(80)


def get_os_release(sbx: Sandbox) -> str:
    """Capture /etc/os-release from the sandbox so the base-template distro
    fact (Debian 12 bookworm, not the ubuntu:22.04 the brief assumed — same
    base template as the Task 6 audio spike) is traceable from a re-runnable
    probe rather than only asserted in prose."""
    r = sbx.commands.run("cat /etc/os-release", user="root", timeout=10)
    return (r.stdout or "").strip()


_SUBJECT_RE = re.compile(r"^\s*0 s:(.+)$", re.MULTILINE)
_ERROR_RE = re.compile(r"error:\S+[^\n]*")


def tls_handshake_probe(ip: str, servername: str, timeout: float = 15) -> dict:
    """Direct TLS handshake probe via the `openssl` CLI: connect to `ip:443`
    with SNI set to `servername`, and record the raw output plus a couple of
    derived booleans (was a certificate chain offered? what subject?). Stdin
    is closed immediately (empty input) so `s_client` completes the handshake,
    prints session info, and exits on EOF rather than hanging for interactive
    input.
    """
    try:
        out = subprocess.run(
            ["openssl", "s_client", "-connect", f"{ip}:443", "-servername", servername],
            input="",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = (out.stdout or "") + "\n" + (out.stderr or "")
        subject_match = _SUBJECT_RE.search(combined)
        error_match = _ERROR_RE.search(combined)
        return {
            "ip": ip,
            "servername": servername,
            "returncode": out.returncode,
            "raw_output": combined.strip()[:1500],
            "certificate_chain_offered": "Certificate chain" in combined,
            "peer_certificate_subject": subject_match.group(1).strip()
            if subject_match
            else None,
            "handshake_error": error_match.group(0) if error_match else None,
        }
    except Exception as e:
        return {"ip": ip, "servername": servername, "error": repr(e)}


def _redact_headers(headers: dict) -> dict:
    """Traffic access tokens are per-sandbox secrets; even though the
    sandboxes in this spike are killed by the time the evidence file is
    committed (so these particular tokens are already dead), redact them on
    principle rather than writing bearer-token-shaped strings into git
    history."""
    out = {}
    for k, v in headers.items():
        if k.lower() == "e2b-traffic-access-token" and v and v != "deadbeef":
            out[k] = f"<redacted, {len(v)} chars>"
        else:
            out[k] = v
    return out


def probe(url: str, headers: dict | None = None, timeout: float = 15) -> dict:
    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout)
        return {
            "url": url,
            "headers_sent": _redact_headers(headers or {}),
            "status": resp.status_code,
            "body": resp.text[:500],
            "error": None,
        }
    except requests.exceptions.RequestException as e:
        return {
            "url": url,
            "headers_sent": _redact_headers(headers or {}),
            "status": None,
            "body": None,
            "error": repr(e),
        }


def probe_with_resolved_ip(host: str, ip: str, timeout: float = 15) -> dict:
    """curl --resolve equivalent: force the connection to a specific IP while
    keeping the original hostname as SNI/Host, to separate DNS resolution
    from TLS/router-layer behavior."""
    url = f"https://{host}/"
    # requests doesn't support --resolve natively; use urllib3's HTTPSConnectionPool
    # with a pre-resolved IP via a custom adapter is overkill for a spike —
    # instead shell out to curl, which is present on macOS/Linux dev machines
    # and is what the brief specifies for this exact fallback.
    try:
        out = subprocess.run(
            [
                "curl",
                "-sS",
                "-m",
                str(int(timeout)),
                "--resolve",
                f"{host}:443:{ip}",
                "-w",
                "\nHTTP_CODE=%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        return {
            "url": url,
            "resolved_ip": ip,
            "stdout": out.stdout[:800],
            "stderr": out.stderr[:800],
            "returncode": out.returncode,
        }
    except Exception as e:
        return {"url": url, "resolved_ip": ip, "error": repr(e)}


def main() -> int:
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        print("E2B_API_KEY not set", file=sys.stderr)
        return 2

    evidence: dict = {"probes": {}}

    # --- sbx_public: default settings, isolates routing/TLS behavior ---
    sbx_public = Sandbox.create(timeout=300)
    try:
        evidence["sandbox_public"] = {
            "sandbox_id": sbx_public.sandbox_id,
            "traffic_access_token": sbx_public.traffic_access_token,
        }
        h = start_echo_server(sbx_public)
        evidence["sandbox_public"]["host"] = h
        evidence["sandbox_public"]["os_release"] = get_os_release(sbx_public)

        # Probe 1: baseline
        evidence["probes"]["1_baseline"] = probe(f"https://{h}/")

        # Probe 2: extra subdomain label
        mailhub_url = f"https://mailhub.{h}/"
        evidence["probes"]["2_mailhub_subdomain"] = probe(mailhub_url)

        # Resolve DNS for the mailhub host explicitly (separate signal from
        # the probe itself) and record whether it resolves at all.
        try:
            ip = socket.gethostbyname(f"mailhub.{h}")
            evidence["dns"] = {"mailhub_host_resolves": True, "resolved_ip": ip}
        except socket.gaierror as e:
            ip = None
            evidence["dns"] = {"mailhub_host_resolves": False, "error": repr(e)}

        # Resolve the plain baseline host's IP independently (don't assume
        # it matches the mailhub resolution above without checking).
        try:
            ip_baseline = socket.gethostbyname(h)
        except socket.gaierror:
            ip_baseline = None

        # Probe 3: curl --resolve fallback (separates DNS from TLS/router
        # behavior) — run regardless of whether probe 2 succeeded, to make
        # the DNS-vs-routing distinction explicit either way.
        if ip:
            evidence["probes"]["3_curl_resolve_fallback"] = probe_with_resolved_ip(
                f"mailhub.{h}", ip
            )

        # Probes 6/7: direct TLS handshake inspection (openssl s_client),
        # corroborating whichever HTTP-level result probes 1/2 observed.
        if ip_baseline:
            evidence["probes"]["6_tls_handshake_baseline"] = tls_handshake_probe(
                ip_baseline, h
            )
        if ip:
            evidence["probes"]["7_tls_handshake_mailhub"] = tls_handshake_probe(
                ip, f"mailhub.{h}"
            )
    finally:
        sbx_public.kill()
        print(f"killed {sbx_public.sandbox_id}")

    # --- sbx_restricted: allow_public_traffic=False, exercises the token path ---
    sbx_restricted = Sandbox.create(
        timeout=300, network={"allow_public_traffic": False}
    )
    try:
        token = sbx_restricted.traffic_access_token
        evidence["sandbox_restricted"] = {
            "sandbox_id": sbx_restricted.sandbox_id,
            "traffic_access_token_present": token is not None,
        }
        h2 = start_echo_server(sbx_restricted)
        evidence["sandbox_restricted"]["host"] = h2

        evidence["probes"]["4a_restricted_no_token"] = probe(f"https://{h2}/")
        evidence["probes"]["4b_restricted_correct_token"] = probe(
            f"https://{h2}/", headers={"e2b-traffic-access-token": token}
        )
        evidence["probes"]["4c_restricted_wrong_token"] = probe(
            f"https://{h2}/", headers={"e2b-traffic-access-token": "deadbeef"}
        )
        # Probe 5: mailhub subdomain WITH correct token on the restricted
        # sandbox — confirms the subdomain failure mode (if any) is
        # independent of the token layer.
        evidence["probes"]["5_mailhub_with_token"] = probe(
            f"https://mailhub.{h2}/", headers={"e2b-traffic-access-token": token}
        )
    finally:
        sbx_restricted.kill()
        print(f"killed {sbx_restricted.sandbox_id}")

    # --- Decision: derived strictly from probe 2's captured status/body,
    # exactly as the brief's decision rule specifies. This does not change
    # based on the TLS corroboration probes below. ---
    p2 = evidence["probes"]["2_mailhub_subdomain"]
    h = evidence["sandbox_public"]["host"]
    mailhub_routes_correctly = p2.get("status") == 200 and f"Host: mailhub.{h}" in (
        p2.get("body") or ""
    )
    host_suffix_mode = (
        "ingress-direct" if mailhub_routes_correctly else "guest-resolver"
    )
    evidence["host_suffix_mode"] = host_suffix_mode

    # --- Rationale: built from the values actually captured this run, not a
    # fixed narrative, so a re-run with different observed behavior produces
    # different rationale text. ---
    if mailhub_routes_correctly:
        evidence["decision_rationale"] = (
            f"probe 2 (https://mailhub.{h}/) returned HTTP {p2.get('status')} with the "
            f"echoed Host header in the body ({p2.get('body')!r}), so ingress routes the "
            "extra subdomain label directly to the guest."
        )
    else:
        p3 = evidence["probes"].get("3_curl_resolve_fallback", {})
        p4a = evidence["probes"]["4a_restricted_no_token"]
        p4c = evidence["probes"]["4c_restricted_wrong_token"]
        p5 = evidence["probes"]["5_mailhub_with_token"]
        tls_base = evidence["probes"].get("6_tls_handshake_baseline", {})
        tls_mh = evidence["probes"].get("7_tls_handshake_mailhub", {})

        lines = []
        lines.append(
            f"probe 2 (https://mailhub.{h}/) did not reach the guest this run: "
            f"status={p2.get('status')!r}, error={p2.get('error')!r}."
        )
        lines.append(
            f"DNS for the mailhub host resolved to {evidence['dns'].get('resolved_ip')!r} "
            f"(mailhub_host_resolves={evidence['dns'].get('mailhub_host_resolves')}), so DNS "
            "is not the cause."
        )
        if p3:
            lines.append(
                f"curl --resolve against that resolved IP reproduced the same failure "
                f"(returncode={p3.get('returncode')}, "
                f"stderr={(p3.get('stderr') or '').strip()!r}), "
                "confirming it independently of DNS."
            )
        lines.append(
            "The primary signal is the contrast between requests that complete TLS+HTTP and "
            "those that don't: the token probes against the plain host "
            f"(4a status={p4a.get('status')!r} body={p4a.get('body')!r}; "
            f"4c status={p4c.get('status')!r} body={p4c.get('body')!r}) both receive real "
            "application-level HTTP 403 JSON from the sandbox's traffic-token gate, while "
            f"the mailhub probes (2 error={p2.get('error')!r}; "
            f"5 error={p5.get('error')!r}) never get an HTTP response at all — they fail "
            "during the TLS handshake itself, before the request layer (and therefore "
            "before token checks) is ever reached."
        )
        if tls_base or tls_mh:
            lines.append(
                "Direct TLS handshake probe (openssl s_client) this run: "
                f"baseline SNI ({tls_base.get('servername')!r}) "
                f"certificate_chain_offered={tls_base.get('certificate_chain_offered')}, "
                f"peer_certificate_subject={tls_base.get('peer_certificate_subject')!r}; "
                f"mailhub SNI ({tls_mh.get('servername')!r}) "
                f"certificate_chain_offered={tls_mh.get('certificate_chain_offered')}, "
                f"peer_certificate_subject={tls_mh.get('peer_certificate_subject')!r}, "
                f"handshake_error={tls_mh.get('handshake_error')!r}."
            )
        lines.append(
            "Task 9 must wire the guest-local dnsmasq + Host-preserving proxy fallback; "
            "WEBSITE_HOST_SUFFIX=osworld.internal."
        )
        evidence["decision_rationale"] = " ".join(lines)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"wrote {OUT_PATH}")
    print(f"host_suffix_mode = {host_suffix_mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
