#!/usr/bin/env python3
"""Guest-browser probe over the campaign's HTTPS fleet origins (secure context).

Maintainer-only: not part of the benchmark path. It answers one question about
one guest build against one live fleet campaign, with no agent, no task and no
model call — now that the website and GitLab fleets are served over HTTPS behind
a per-campaign CA, does the guest's own Chrome actually treat those origins as
secure contexts, so the browser APIs the tasks depend on (clipboard,
notifications, ``crypto.randomUUID``) exist and work?

``docs/pr-1-verification.md`` recorded the pre-HTTPS measurement from inside
Chrome: ``isSecureContext=false``, ``navigator.clipboard`` undefined, notification
grants failing, ``crypto.randomUUID`` missing. This probe is the matching
measurement on the HTTPS routing, taken the same way — from inside the guest's
Chrome over CDP, not from the host with ``curl``.

What it does, once:
  1. Creates a single guest through the provider's in-process bridge, exactly as
     ``maintainer/harness.py`` does (``DesktopEnv(provider_name="e2b", ...)``). The bridge installs the in-guest
     Host-mapping proxy, the campaign CA (system trust store *and* Chrome's NSS
     database) and the ``/etc/hosts`` entries from ``OSWORLD_FLEET_RULES``, so the
     fleet origins resolve and verify inside the guest the way they do in a task.
  2. Launches Chrome in the guest through the guest server's ``/setup/launch``
     (``setup_controller.launch``), so it runs as ``user`` on ``DISPLAY=:0``
     exactly as in a task, then issues the same ``socat`` 9222->1337 launch the
     78 upstream Chrome task configs issue (``tasks/task_016.py``). The template
     ships a shim that makes that a no-op when the baked ``osworld-cdp.service``
     is already listening; the probe records whether it was.
  3. Drives ONE CDP target through the bridge's proxied CDP endpoint
     (``bridge.cdp_port`` -> guest ``9222`` -> Chrome ``1337``) across all six
     origins in order, so the cookie-isolation check is a real cross-origin
     check in one browsing session rather than six separate sessions.
  4. Per origin: ``Page.navigate``, wait for ``Page.loadEventFired``, a short
     quiet period, then ``Runtime.evaluate`` of ``isSecureContext`` /
     ``typeof navigator.clipboard`` / ``Notification.permission`` /
     ``typeof crypto.randomUUID`` / ``document.title`` / ``location.href``,
     ``Security.getSecurityState`` (falling back to the ``Security`` domain's
     own events, which is why the domain is enabled — Chrome 153 has removed
     both that command and the older ``securityStateChanged`` event, so the
     value comes from ``visibleSecurityStateChanged``), and every ``Log.entryAdded`` /
     ``Runtime.consoleAPICalled`` message mentioning mixed content or a block —
     with the blocked URLs recorded verbatim. The ``Network`` domain is enabled
     too, and every fetch the page issues is enumerated: a total, a per-scheme
     tally, and the verbatim list of any request whose scheme is not a secure
     transport. That turns "nothing was reported blocked" (an absence) into
     "every subresource this page fetched was HTTPS" (a positive result), which
     is the evidence the deferred cross-host rewrite question actually needs.
  5. On TeamChat additionally: ``Browser.setPermission`` for notifications then
     re-reads ``Notification.permission``; grants the clipboard permissions and
     round-trips ``navigator.clipboard.writeText("probe")`` -> ``readText()``; and
     sets ``document.cookie="probe=1"``, which the CloudCRM leg then asserts is
     absent from its own ``document.cookie``.
  6. Uploads a 2 MiB payload to StreamView, verifies its persisted bytes, and
     clears the isolated probe session. Pushes a 2 MiB Git blob from the guest
     with chunked HTTP, verifies it through GitLab, and deletes the probe project.
  7. Writes ``<output-dir>/browser-probe-<build-id>.json`` and kills the guest.
     That raw record is gitignored; commit its reduction from
     ``maintainer/receipt_summary.py`` under ``out/osworld-v2-evidence/fleet/``.

Acceptance criteria (evaluated into ``acceptance``; the script exits non-zero if
any is false). Per origin: ``secure`` is ``true``, ``typeof navigator.clipboard``
is ``"object"``, the security state is ``"secure"``, the title is non-empty and
is not Chrome's ``Privacy error`` interstitial, zero mixed-content/blocked log
entries were collected (and no ``blockedReason: mixed-content`` load failure),
and zero subresource requests used an insecure scheme. Plus: TeamChat's
``Notification.permission`` becomes ``"granted"`` after ``Browser.setPermission``;
the clipboard round-trip returns exactly ``"probe"``; the cookie set on TeamChat
is absent on CloudCRM; and the task-041 GitLab alias page's title contains
``GitLab``. Both upload and Git push round-trips must succeed.

Four of the booleans guard the probe against scoring itself green on nothing:

  * ``cdp_domains_enabled`` — every ``*.enable`` returned no error. ``cdp.call``
    returns protocol errors rather than raising, so a failed ``Log.enable`` or
    ``Network.enable`` would leave the collectors silent and every
    mixed-content and all-subresources-HTTPS boolean passing on an empty buffer.
  * ``all_origins_probed`` — as many origins recorded as requested, so a
    mid-loop abort fails explicitly instead of depending on which origin
    happened to be last.
  * ``<label>.loaded`` and ``<label>.url_origin_matches`` — the navigation
    actually landed on the origin being scored. A failed navigation that left
    the previous document loaded would otherwise be scored against that page
    and could pass every check while saying nothing about the origin it names.
    The comparison is on origin (scheme + host), not full URL, because these
    apps redirect within their own origin and that is correct.

Before any guest is created, ``_origin_literal_check`` asserts that the literal
``ORIGINS`` list still names the fleet's task origins — the full
``WEBSITE_HOST_SUFFIX`` authority (including port) and the task-041 alias in the
campaign leaf's SAN list — and raises naming both values if not, recording in
``origin_literal_check`` which halves ran and which were skipped for want of an
input. A probe silently testing origins no task uses would read exactly like a
passing one.

Every raw per-origin value is recorded whether it passes or fails: a failure has
to be diagnosable from the JSON alone. A real defect found here (a genuine
mixed-content block, say) is a valid outcome — do not weaken a check to make the
run pass.

Not run in CI and not run by any ladder rung: it needs a live E2B guest build
*and* a running fleet campaign. Point ``GUEST_TEMPLATE`` at the immutable
``name:build_id`` reference under test, export the fleet wiring, and run it from
the pinned checkout root. Start the host proxy as described in README's upstream
runner instructions first; GitLab's host-side verification uses it:

    source runner/common.sh && export_fleet_wiring
    cd OSWorld-V2
    GUEST_TEMPLATE=<name>:<build-id> uv run --locked --extra full --python 3.12 \
        --with e2b==2.34.0 --with aiohttp==3.14.1 \
        python /path/to/maintainer/browser_probe.py \
            --output-dir /path/to/out/osworld-v2-raw/live-$OSWORLD_CAMPAIGN_ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import secrets
import shlex
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import requests


# desktop_env is imported from the pinned checkout, whose root must be the cwd
# (several upstream modules resolve repo-relative paths from it). harness.py
# does the same; keep both able to run directly.
sys.path.insert(0, os.getcwd())

from desktop_env.desktop_env import DesktopEnv  # noqa: E402

# The six origins under test, in the order they are visited. TeamChat is first
# because it is where the probe cookie is set; CloudCRM follows because it is
# where that cookie must NOT be visible. `studio.streamview` is here because the
# cross-host reference StreamView makes is the whole subject of the deferred
# cross-host rewrite -- a probe that never loads it cannot speak to that
# question, and the host is an explicit SAN on the campaign leaf. The last entry
# is task 041's hardcoded public GitLab host, aliased to the guest proxy through
# /etc/hosts.
ORIGINS: tuple[tuple[str, str], ...] = (
    ("teamchat", "https://teamchat.127.0.0.1.nip.io:8090"),
    ("cloudcrm", "https://cloudcrm.127.0.0.1.nip.io:8090"),
    ("mailhub", "https://mailhub.127.0.0.1.nip.io:8090"),
    ("streamview", "https://streamview.127.0.0.1.nip.io:8090"),
    ("studio-streamview", "https://studio.streamview.127.0.0.1.nip.io:8090"),
    ("gitlab-041", "https://54.174.16.65.sslip.io/users/sign_in"),
)
# ORIGINS stays literal on purpose: the probe has to state the origins it
# claims to have tested rather than derive them from the same wiring under
# test. The cost of that is drift -- the two values those literals encode
# duplicate services/fleetlib.py's HOST_SUFFIX and services/campaign_tls.py's
# TASK_041_GITLAB_ALIAS, and if either moved, the probe would keep passing
# while testing hosts no task uses. So they are named here and checked against
# the live campaign at startup by _origin_literal_check. The services modules
# are deliberately not imported: services/campaign_tls.py pulls in fleetlib and
# the e2b SDK and mutates sys.path at import, which the probe (which already
# inserts the upstream checkout at sys.path[0]) must not depend on. The
# campaign's own runtime file is the authority instead.
ORIGIN_HOST_SUFFIX = "127.0.0.1.nip.io:8090"
ORIGIN_GITLAB_ALIAS = "54.174.16.65.sslip.io"
SERVICES_RUNTIME_FILE = (
    Path(__file__).resolve().parents[1] / "services" / ".runtime.json"
)
# The argv the brief pins, and the socat forward all 78 upstream Chrome task
# configs issue right after it (tasks/task_016.py:297-298). The template's
# /usr/local/bin/socat shim turns the second one into a no-op when the baked
# osworld-cdp.service already holds :9222, so issuing it is idempotent and
# removes any dependence on that unit being up.
CHROME_LAUNCH = ["google-chrome", "--remote-debugging-port=1337", "about:blank"]
SOCAT_LAUNCH = ["socat", "tcp-listen:9222,fork", "tcp:localhost:1337"]
# The exact expression the brief pins. Evaluated first on every origin; if it
# throws (a Chrome interstitial need not expose `Notification`), the run records
# the exception and falls back to DEFENSIVE_EXPRESSION rather than losing the leg.
PROBE_EXPRESSION = (
    "JSON.stringify({secure:isSecureContext, clipboard:typeof navigator.clipboard,"
    " notif:Notification.permission, uuid:typeof crypto.randomUUID,"
    " title:document.title, url:location.href})"
)
DEFENSIVE_EXPRESSION = (
    "JSON.stringify({secure:isSecureContext, clipboard:typeof navigator.clipboard,"
    ' notif:(typeof Notification==="undefined"?null:Notification.permission),'
    ' uuid:(typeof crypto==="undefined"?"undefined":typeof crypto.randomUUID),'
    " title:document.title, url:location.href})"
)
CLIPBOARD_ROUNDTRIP = (
    '(async () => { await navigator.clipboard.writeText("probe");'
    " return await navigator.clipboard.readText(); })()"
)
# Permissions API descriptor names Chrome accepts for the clipboard. The brief
# names the first two; the sanitized-write alias is tried as well because Chrome
# splits write into sanitized/unsanitized and a rejected name must not be
# mistaken for a rejected grant.
CLIPBOARD_PERMISSIONS = (
    "clipboard-read",
    "clipboard-write",
    "clipboard-sanitized-write",
)
# Case-insensitive substrings that mark a mixed-content or blocked-subresource
# report in a console or Log entry. Acceptance requires zero matches per origin.
BLOCK_MARKERS = ("mixed content", "blocked")
# Chrome's certificate interstitial. A title containing this means the campaign
# CA is not trusted in the guest, which is the whole thing this probe tests.
INTERSTITIAL_TITLE = "Privacy error"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>)]+")
# Caps on the recorded console/log text, so one enormous console dump cannot
# bloat the record. Generous: the whole point is to keep every entry readable.
MAX_ENTRY_TEXT = 4000
MAX_ENTRIES_PER_ORIGIN = 200
CDP_DOMAINS = ("Page", "Runtime", "Log", "Security", "Network")
EVENTS_OF_INTEREST = ("Log.entryAdded", "Runtime.consoleAPICalled")
# Network events worth keeping. `requestWillBeSent` is the one that turns "no
# mixed content was reported" into "every subresource this page fetched was
# HTTPS" -- an absence of complaint is weak evidence, an enumerated request list
# is strong. WebSocket opens arrive on their own event and never as a request,
# so `ws://` would otherwise be invisible. `loadingFailed` carries CDP's own
# `blockedReason`, which is the most authoritative mixed-content signal there
# is.
NETWORK_BUFFERED = (
    "Network.requestWillBeSent",
    "Network.webSocketCreated",
    "Network.loadingFailed",
)
# Schemes that are secure transports. `wss:` is the WebSocket equivalent of
# `https:`; `ws:` is mixed content and must not be here.
SECURE_SCHEMES = ("https", "wss")
# Not mixed content: these fetch nothing over the network, so they are recorded
# in the per-scheme tally but excluded from the all-subresources-HTTPS
# assertion.
BENIGN_SCHEMES = ("data", "blob", "about", "chrome-extension")
# CDP's own reason string for a subresource Chrome blocked as mixed content.
MIXED_CONTENT_BLOCKED_REASON = "mixed-content"
# Security-state event shapes, most current first. Chrome 153 (the guest build
# under test) has removed both the `Security.getSecurityState` command and the
# `Security.securityStateChanged` event; `visibleSecurityStateChanged` is what
# it actually emits, and it nests the state under `visibleSecurityState`. The
# deprecated event is still read so an older build is still reportable.
SECURITY_EVENTS = (
    "Security.visibleSecurityStateChanged",
    "Security.securityStateChanged",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _origin_of(url: str) -> str:
    """Scheme + host of a URL, lowercased — what Browser.setPermission wants,
    and the unit the loaded-page check compares.

    Deliberately origin, not full URL: these apps redirect within their own
    origin (TeamChat lands on /channel/general), which is correct behaviour and
    must still match. Returns "" for anything without a scheme//host shape,
    such as ``about:blank``, so a non-navigation can never accidentally match.
    """
    parts = url.split("/")
    if len(parts) < 3 or not parts[0].endswith(":") or parts[1] != "" or not parts[2]:
        return ""
    return f"{parts[0]}//{parts[2]}".lower()


def _origin_literal_check(runtime_file: Path | None = None) -> dict:
    """Check ORIGINS against the live campaign before a guest is created.

    Three things, all cheap, none of which changes which origins are probed:

      1. The literals are self-consistent -- every host in ORIGINS is either
         under ``ORIGIN_HOST_SUFFIX`` or is exactly ``ORIGIN_GITLAB_ALIAS``.
         A typo'd entry fails here instead of producing a green record for an
         origin nothing serves.
      2. ``WEBSITE_HOST_SUFFIX`` matches the full suffix, including the task port.
      3. ``services/.runtime.json``'s ``tls.hosts`` -- the campaign leaf's own
         SAN list -- contains ``ORIGIN_GITLAB_ALIAS``.
         ``campaign_tls.ensure_campaign_tls`` folds ``TASK_041_GITLAB_ALIAS``
         into every leaf unconditionally, so its absence means the constant
         here is no longer that one.

    Raises ``RuntimeError`` naming both values on a mismatch: a probe testing
    the wrong origins is worse than no probe, because its record reads the
    same. Where an input is unavailable (no ``WEBSITE_HOST_SUFFIX`` exported,
    no runtime file, or a runtime file with no ``tls`` section) the check is
    recorded as skipped rather than quietly passed -- the returned dict goes
    into the receipt so a reader can see which halves actually ran.
    """
    runtime_file = SERVICES_RUNTIME_FILE if runtime_file is None else runtime_file
    hosts = [_origin_of(url).split("//", 1)[-1] for _, url in ORIGINS]
    result: dict = {
        "origin_host_suffix": ORIGIN_HOST_SUFFIX,
        "gitlab_alias": ORIGIN_GITLAB_ALIAS,
        "probed_hosts": hosts,
        "runtime_file": str(runtime_file),
    }

    stray = [
        host
        for host in hosts
        if host != ORIGIN_GITLAB_ALIAS and not host.endswith(f".{ORIGIN_HOST_SUFFIX}")
    ]
    if stray:
        raise RuntimeError(
            "browser_probe ORIGINS is internally inconsistent: "
            f"{stray} is neither under {ORIGIN_HOST_SUFFIX!r} nor the task-041 "
            f"alias {ORIGIN_GITLAB_ALIAS!r}"
        )

    suffix_env = os.environ.get("WEBSITE_HOST_SUFFIX")
    if not suffix_env:
        result["host_suffix_checked"] = False
        result["host_suffix_skipped_because"] = "WEBSITE_HOST_SUFFIX is not set"
    else:
        result["website_host_suffix"] = suffix_env
        if suffix_env != ORIGIN_HOST_SUFFIX:
            raise RuntimeError(
                "browser_probe would test the wrong fleet origins: "
                f"WEBSITE_HOST_SUFFIX={suffix_env!r} but "
                f"ORIGINS is built on {ORIGIN_HOST_SUFFIX!r}. The fleet suffix "
                "moved; update ORIGINS and ORIGIN_HOST_SUFFIX together."
            )
        result["host_suffix_checked"] = True

    tls_hosts = None
    if runtime_file.exists():
        try:
            tls_hosts = (json.loads(runtime_file.read_text()).get("tls") or {}).get(
                "hosts"
            )
        except (OSError, ValueError) as error:
            result["runtime_file_error"] = f"{type(error).__name__}: {error}"
    if not tls_hosts:
        result["gitlab_alias_checked"] = False
        result["gitlab_alias_skipped_because"] = (
            f"{runtime_file} has no tls.hosts (no live campaign recorded)"
        )
    else:
        result["tls_hosts"] = list(tls_hosts)
        if ORIGIN_GITLAB_ALIAS not in tls_hosts:
            raise RuntimeError(
                "browser_probe would test the wrong task-041 GitLab origin: "
                f"{ORIGIN_GITLAB_ALIAS!r} is not in the campaign leaf's SAN list "
                f"{sorted(tls_hosts)!r} from {runtime_file}. campaign_tls.py's "
                "TASK_041_GITLAB_ALIAS moved; update ORIGIN_GITLAB_ALIAS and the "
                "gitlab-041 entry in ORIGINS together."
            )
        result["gitlab_alias_checked"] = True

    return result


def _short_exception(details: dict | None) -> dict | None:
    """Trim Runtime.evaluate's exceptionDetails to the diagnosable fields."""
    if not details:
        return None
    exception = details.get("exception") or {}
    return {
        "text": details.get("text"),
        "description": exception.get("description"),
        "class_name": exception.get("className"),
    }


def _event_text(event: dict) -> str:
    """Flatten a Log/console event into one searchable string."""
    method, params = event.get("method"), event.get("params") or {}
    if method == "Log.entryAdded":
        entry = params.get("entry") or {}
        return " ".join(
            str(entry.get(key) or "") for key in ("source", "level", "text", "url")
        )
    if method == "Runtime.consoleAPICalled":
        parts = [str(params.get("type") or "")]
        for argument in params.get("args") or []:
            parts.append(
                str(
                    argument.get("value")
                    if "value" in argument
                    else argument.get("description") or ""
                )
            )
        return " ".join(parts)
    return ""


def _scheme_of(url: str) -> str:
    """The URL's scheme, lowercased, or "" if it has none."""
    return url.split(":", 1)[0].lower() if ":" in url else ""


def _security_state_of(event: dict) -> str | None:
    """The securityState carried by either security-event shape."""
    params = event.get("params") or {}
    visible = params.get("visibleSecurityState") or {}
    return visible.get("securityState") or params.get("securityState")


def _event_urls(event: dict, text: str) -> list[str]:
    """Every URL the event names, verbatim — a blocked URL must be actionable."""
    urls = []
    entry = (event.get("params") or {}).get("entry") or {}
    if entry.get("url"):
        urls.append(entry["url"])
    urls.extend(URL_PATTERN.findall(text))
    seen, unique = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


class CdpSession:
    """Minimal CDP client over one target's WebSocket.

    A single reader task owns the socket: it resolves command futures and
    buffers every event for the whole session, tagged with the origin that was
    loading when it arrived. The domains are enabled once, before the first
    navigation, so no mixed-content report can land before a listener exists —
    attaching listeners after the load is how a run records a false "zero
    mixed-content" result.
    """

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._load_event = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self.events: list[dict] = []
        self.origin_label = "startup"
        # Per-method counts for the high-volume Network events that are counted
        # but deliberately not buffered (see _read_loop).
        self.unbuffered: dict[str, int] = {}
        # requestId -> URL, so a Network.loadingFailed can be reported with the
        # URL it was for.
        self._request_urls: dict[str, str] = {}

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _read_loop(self) -> None:
        async for message in self._ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            if "id" in payload:
                future = self._pending.pop(payload["id"], None)
                if future is not None and not future.done():
                    future.set_result(payload)
                continue
            method = payload.get("method")
            # Enabling Network makes Chrome chatty (dataReceived, per-chunk
            # progress, the ExtraInfo pairs). Those carry no evidence this probe
            # needs, so they are counted but not buffered: one media-heavy page
            # must not balloon the record. Every event is still accounted for.
            if isinstance(method, str) and method.startswith("Network."):
                if method not in NETWORK_BUFFERED:
                    self.unbuffered[method] = self.unbuffered.get(method, 0) + 1
                    continue
                if method == "Network.requestWillBeSent":
                    request_id = payload.get("params", {}).get("requestId")
                    url = (payload.get("params", {}).get("request") or {}).get("url")
                    if request_id and url:
                        # loadingFailed names only a requestId, so the URL a
                        # blocked request was for has to come from here.
                        self._request_urls[request_id] = url
            payload["_origin"] = self.origin_label
            payload["_at"] = utc_now()
            self.events.append(payload)
            if method == "Page.loadEventFired":
                self._load_event.set()

    async def call(
        self, method: str, params: dict | None = None, timeout: float = 30.0
    ) -> dict:
        """Issue a CDP command. A timeout or a domain error is returned, not
        raised: a deprecated command or one failing origin must not abort the run."""
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._ws.send_str(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._pending.pop(message_id, None)
            return {
                "error": {
                    "message": f"no CDP response within {timeout}s",
                    "method": method,
                }
            }

    def reset_load_event(self) -> None:
        self._load_event.clear()

    async def wait_for_load(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._load_event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def network(self, label: str) -> dict:
        """Every network fetch the page issued while `label` was loading.

        This is the positive form of the mixed-content question. "Nothing was
        reported blocked" is an absence; this enumerates what the page actually
        fetched, so a reader can see for themselves that every subresource came
        over HTTPS -- or exactly which ones did not, verbatim.

        `insecure_requests` is the assertion's subject: every request whose
        scheme is neither a secure transport (`https:`, `wss:`) nor one of the
        schemes that fetch nothing at all (`data:`, `blob:`, `about:`,
        `chrome-extension:`). A `ws:` or `http:` URL lands here, which is the
        point.
        """
        requests: list[dict] = []
        failures: list[dict] = []
        for event in self.events:
            if event.get("_origin") != label:
                continue
            method, params = event.get("method"), event.get("params") or {}
            if method == "Network.requestWillBeSent":
                url = (params.get("request") or {}).get("url") or ""
                requests.append(
                    {
                        "url": url,
                        "scheme": _scheme_of(url),
                        "resource_type": params.get("type"),
                        "source": "Network.requestWillBeSent",
                    }
                )
            elif method == "Network.webSocketCreated":
                url = params.get("url") or ""
                requests.append(
                    {
                        "url": url,
                        "scheme": _scheme_of(url),
                        "resource_type": "WebSocket",
                        "source": "Network.webSocketCreated",
                    }
                )
            elif method == "Network.loadingFailed":
                failures.append(
                    {
                        "url": self._request_urls.get(params.get("requestId"))
                        or "<unknown requestId>",
                        "resource_type": params.get("type"),
                        "error_text": params.get("errorText"),
                        "blocked_reason": params.get("blockedReason"),
                        "canceled": params.get("canceled"),
                    }
                )
        schemes: dict[str, int] = {}
        for request in requests:
            schemes[request["scheme"]] = schemes.get(request["scheme"], 0) + 1
        insecure = [
            request
            for request in requests
            if request["scheme"] not in SECURE_SCHEMES
            and request["scheme"] not in BENIGN_SCHEMES
        ]
        return {
            "total_requests": len(requests),
            "schemes": dict(sorted(schemes.items())),
            "insecure_requests": insecure,
            "insecure_urls": sorted({request["url"] for request in insecure}),
            "mixed_content_blocked": [
                failure
                for failure in failures
                if failure["blocked_reason"] == MIXED_CONTENT_BLOCKED_REASON
            ],
            "failures": failures,
            "requests": requests[:MAX_ENTRIES_PER_ORIGIN],
            "requests_truncated": len(requests) > MAX_ENTRIES_PER_ORIGIN,
        }

    def console_entries(self, label: str) -> list[dict]:
        """EVERY Log/console entry collected while `label` was loading.

        `findings()` below is the filtered subset acceptance reads; this is the
        unfiltered list, because "zero mixed-content entries" is only checkable
        by a reader who can see what the entries actually were. Text is capped
        per entry so one enormous console dump cannot bloat the record; the
        caller caps the list length and records whether it did.
        """
        entries = []
        for event in self.events:
            if (
                event.get("_origin") != label
                or event.get("method") not in EVENTS_OF_INTEREST
            ):
                continue
            text = _event_text(event)
            entries.append(
                {
                    "method": event["method"],
                    "at": event.get("_at"),
                    "text": text[:MAX_ENTRY_TEXT],
                    "text_truncated": len(text) > MAX_ENTRY_TEXT,
                }
            )
        return entries

    def findings(self, label: str) -> list[dict]:
        """Mixed-content / blocked reports collected while `label` was loading."""
        findings = []
        for event in self.events:
            if (
                event.get("_origin") != label
                or event.get("method") not in EVENTS_OF_INTEREST
            ):
                continue
            text = _event_text(event)
            lowered = text.lower()
            matched = [marker for marker in BLOCK_MARKERS if marker in lowered]
            if not matched:
                continue
            findings.append(
                {
                    "method": event["method"],
                    "markers": matched,
                    "text": text,
                    "urls": _event_urls(event, text),
                    "at": event.get("_at"),
                    "params": event.get("params"),
                }
            )
        return findings

    def security_events(self, label: str) -> list[dict]:
        """The security-state events Chrome emitted while `label` was loading,
        verbatim, so an empty state is diagnosable from the JSON alone."""
        return [
            {
                "method": event["method"],
                "at": event.get("_at"),
                "params": event.get("params"),
            }
            for event in self.events
            if event.get("method") in SECURITY_EVENTS and event.get("_origin") == label
        ]

    def last_security_state(self, label: str) -> tuple[str | None, str | None]:
        """Latest security state for `label` and the event it came from.

        Chrome 153 has removed both the `Security.getSecurityState` command and
        the `Security.securityStateChanged` event; `visibleSecurityStateChanged`
        is the current one and carries the state one level down. All three are
        read, newest event of the most current shape first, so the probe reports
        the value Chrome actually publishes on whatever build it meets.
        """
        for method in SECURITY_EVENTS:
            states = [
                _security_state_of(event)
                for event in self.events
                if event.get("method") == method and event.get("_origin") == label
            ]
            states = [state for state in states if state]
            if states:
                return states[-1], method
        return None, None

    def event_methods(self) -> dict[str, int]:
        """Per-method event counts for the whole session, buffered events and
        the counted-but-unbuffered Network ones alike. Cheap, and it is what
        tells a reader whether a listener was simply never fed."""
        counts: dict[str, int] = dict(self.unbuffered)
        for event in self.events:
            method = str(event.get("method"))
            counts[method] = counts.get(method, 0) + 1
        return dict(sorted(counts.items()))


async def _evaluate(
    cdp: CdpSession,
    expression: str,
    await_promise: bool = False,
    timeout: float = 30.0,
) -> dict:
    response = await cdp.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
        timeout=timeout,
    )
    result = response.get("result") or {}
    return {
        "value": (result.get("result") or {}).get("value"),
        "error": response.get("error"),
        "exception": _short_exception(result.get("exceptionDetails")),
    }


async def _http_json(
    session: aiohttp.ClientSession, url: str, method: str = "GET"
) -> tuple[int, str]:
    async with session.request(
        method, url, timeout=aiohttp.ClientTimeout(total=20)
    ) as response:
        return response.status, await response.text()


async def _wait_for_cdp(
    session: aiohttp.ClientSession, base: str, timeout_s: float
) -> dict:
    """Chrome needs settle time after /setup/launch before CDP answers, and how
    much varies; poll rather than sleeping a fixed amount."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            status, text = await _http_json(session, f"{base}/json/version")
            if status == 200:
                return json.loads(text)
            last = f"HTTP {status}: {text[:200]}"
        except Exception as error:  # noqa: BLE001 - any transport error is a retry
            last = repr(error)
        await asyncio.sleep(2)
    raise RuntimeError(f"CDP /json/version never answered on {base} ({last})")


async def _page_target(session: aiohttp.ClientSession, base: str) -> dict:
    """The page target to drive. The bridge rewrites the advertised CDP host in
    /json* responses, so the webSocketDebuggerUrl it hands back is used verbatim."""
    status, text = await _http_json(session, f"{base}/json/list")
    targets = json.loads(text) if status == 200 else []
    pages = [
        target
        for target in targets
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
    ]
    if pages:
        return pages[0]
    # Chrome >= 111 requires PUT on /json/new; older builds accept GET.
    status, text = await _http_json(
        session, f"{base}/json/new?about:blank", method="PUT"
    )
    if status != 200:
        status, text = await _http_json(session, f"{base}/json/new?about:blank")
    if status != 200:
        raise RuntimeError(f"no CDP page target and /json/new returned HTTP {status}")
    return json.loads(text)


async def _security_state(cdp: CdpSession, label: str) -> dict:
    """The origin's security state, plus which CDP source produced it.

    `Security.getSecurityState` is deprecated and is *gone* on the guest build
    under test (Chrome 153 answers ``-32601 ... wasn't found``), which is why
    `Security.enable` is issued: the value then comes from the domain's events.
    The deprecated command's failure never fails the probe by itself -- but a
    null value with no event to explain it is a probe defect, not a fleet
    result, so the raw events are recorded alongside.
    """
    response = await cdp.call("Security.getSecurityState", {})
    result = response.get("result") or {}
    if response.get("error") is None and result.get("securityState"):
        return {
            "value": result["securityState"],
            "source": "Security.getSecurityState",
            "command_error": None,
            "events": cdp.security_events(label),
        }
    value, source = cdp.last_security_state(label)
    return {
        "value": value,
        "source": source,
        "command_error": response.get("error"),
        "events": cdp.security_events(label),
    }


async def _probe_origin(
    cdp: CdpSession, label: str, url: str, args: argparse.Namespace
) -> dict:
    cdp.origin_label = label
    cdp.reset_load_event()
    entry: dict = {"label": label, "requested_url": url, "requested_at": utc_now()}

    navigation = await cdp.call(
        "Page.navigate", {"url": url}, timeout=args.navigate_timeout_seconds
    )
    entry["navigate"] = {
        "error": navigation.get("error"),
        "result": navigation.get("result"),
    }
    entry["load_event_fired"] = await cdp.wait_for_load(args.load_timeout_seconds)
    # Late console/log entries land after the load event; snapshot the buffer
    # only once the page has been quiet for a moment.
    await asyncio.sleep(args.quiet_seconds)

    probe = await _evaluate(cdp, PROBE_EXPRESSION)
    if not isinstance(probe["value"], str):
        entry["probe_expression_failure"] = {
            "error": probe["error"],
            "exception": probe["exception"],
        }
        probe = await _evaluate(cdp, DEFENSIVE_EXPRESSION)
        entry["probe_expression"] = "defensive"
    else:
        entry["probe_expression"] = "brief"
    entry["raw_probe"] = probe["value"]
    entry["evaluate_error"] = probe["error"]
    entry["evaluate_exception"] = probe["exception"]
    values: dict = {}
    if isinstance(probe["value"], str):
        try:
            values = json.loads(probe["value"])
        except json.JSONDecodeError:
            pass
    entry["values"] = values

    entry["security"] = await _security_state(cdp, label)
    entry["network"] = cdp.network(label)
    all_entries = cdp.console_entries(label)
    entry["console_entries"] = all_entries[:MAX_ENTRIES_PER_ORIGIN]
    # Matches network's `requests_truncated`. The cap only ever hides entries
    # from a reader, never from acceptance: `findings()` below scans every
    # buffered event, uncapped.
    entry["console_entries_truncated"] = len(all_entries) > MAX_ENTRIES_PER_ORIGIN
    entry["log_findings"] = cdp.findings(label)
    entry["blocked_urls"] = sorted(
        {url for finding in entry["log_findings"] for url in finding["urls"]}
    )
    return entry


async def _teamchat_extras(
    cdp: CdpSession, origin: str, args: argparse.Namespace
) -> dict:
    extras: dict = {"origin": origin}

    notifications = await cdp.call(
        "Browser.setPermission",
        {
            "permission": {"name": "notifications"},
            "setting": "granted",
            "origin": origin,
        },
    )
    extras["set_permission_notifications_error"] = notifications.get("error")
    after = await _evaluate(
        cdp, 'typeof Notification==="undefined"?null:Notification.permission'
    )
    extras["notification_permission_after_grant"] = after["value"]
    extras["notification_permission_error"] = after["error"]

    grants: dict = {}
    for name in CLIPBOARD_PERMISSIONS:
        response = await cdp.call(
            "Browser.setPermission",
            {"permission": {"name": name}, "setting": "granted", "origin": origin},
        )
        grants[name] = response.get("error")
    extras["clipboard_permission_errors"] = grants

    # readText() requires a focused document. Raising the window is the ordinary
    # way to get that; focus emulation is only a second attempt, and which one
    # produced the value is recorded so the result stays honest.
    bring_to_front = await cdp.call("Page.bringToFront")
    extras["bring_to_front_error"] = bring_to_front.get("error")
    attempts = []
    first = await _evaluate(
        cdp,
        CLIPBOARD_ROUNDTRIP,
        await_promise=True,
        timeout=args.clipboard_timeout_seconds,
    )
    attempts.append({"focus_emulation": False, **first})
    value = first["value"]
    if value != "probe":
        focus = await cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        second = await _evaluate(
            cdp,
            CLIPBOARD_ROUNDTRIP,
            await_promise=True,
            timeout=args.clipboard_timeout_seconds,
        )
        attempts.append(
            {
                "focus_emulation": True,
                "set_focus_emulation_error": focus.get("error"),
                **second,
            }
        )
        value = second["value"]
    extras["clipboard_roundtrip"] = {"value": value, "attempts": attempts}

    cookie = await _evaluate(cdp, 'document.cookie = "probe=1"; document.cookie')
    extras["cookie_after_set"] = cookie["value"]
    extras["cookie_set_error"] = cookie["error"]
    return extras


async def _streamview_upload(cdp: CdpSession) -> dict:
    # Exercise the browser's real upload/download path with an isolated cookie.
    # This is a transport payload, not a playable-video or rendering test.
    return await _evaluate(
        cdp,
        """(async () => {
        const oldCookie = document.cookie.split('; ').find(c => c.startsWith('user_id='));
        document.cookie = `user_id=probe-${crypto.randomUUID()}; Path=/; SameSite=Lax`;
        const title = `probe-${crypto.randomUUID()}`;
        const bytes = new Uint8Array(2 * 1024 * 1024).fill(37);
        const request = (url, options = {}) => fetch(url, {
            ...options, signal: AbortSignal.timeout(30000),
        });
        try {
            const form = new FormData();
            form.append('file', new Blob([bytes], {type: 'video/mp4'}), 'probe.mp4');
            form.append('title', title);
            const upload = await request('/api/streamview/videos', {method: 'POST', body: form});
            if (!upload.ok) throw new Error(`upload: ${upload.status}`);
            const state = await (await request('/api/streamview/bootstrap')).json();
            const video = state.data.videos.find(v => v.title === title);
            if (!video) throw new Error('uploaded video missing from state');
            const response = await request(video.asset.url);
            const saved = new Uint8Array(await response.arrayBuffer());
            return response.ok && saved.length === bytes.length && saved.every(b => b === 37);
        } finally {
            try {
                const cleared = await request('/api/state', {method: 'DELETE'});
                if (!cleared.ok) throw new Error(`cleanup: ${cleared.status}`);
            } finally {
                document.cookie = oldCookie ? `${oldCookie}; Path=/` : 'user_id=; Path=/; Max-Age=0';
            }
        }
    })()""",
        await_promise=True,
        timeout=180,
    )


def _gitlab_push(controller) -> bool:
    """Create an isolated project, push from the guest, verify its blob, delete it.

    Only a project-scoped token reaches the guest; the campaign admin token stays
    on the host. The random payload exceeds Git's normal chunking threshold.
    """
    base = os.environ["GITLAB_URL"].rstrip("/")
    with requests.Session() as session:
        session.headers["PRIVATE-TOKEN"] = os.environ["GITLAB_PRIVATE_TOKEN"]

        def api(method, path, **kwargs):
            response = session.request(
                method, f"{base}/api/v4{path}", timeout=60, **kwargs
            )
            response.raise_for_status()
            return response

        project = api(
            "POST", "/projects", json={"name": "osworld-probe-" + secrets.token_hex(6)}
        ).json()
        path = f"/projects/{project['id']}"
        try:
            token = api(
                "POST",
                path + "/access_tokens",
                json={
                    "name": "guest-push-probe",
                    "scopes": ["write_repository"],
                    "access_level": 40,
                    "expires_at": (datetime.now(UTC) + timedelta(days=1))
                    .date()
                    .isoformat(),
                },
            ).json()["token"]
            url = (
                base.replace("https://", f"https://oauth2:{token}@", 1)
                + f"/{project['path_with_namespace']}.git"
            )
            result = (
                controller.run_bash_script(
                    'set -eu\nwork=$(mktemp -d)\ntrap \'rm -rf "$work"\' EXIT\ncd "$work"\n'
                    "git init -q\npython3 -c \"import os;open('payload.bin','wb').write(os.urandom(2*1024*1024))\"\n"
                    "git add payload.bin\ngit -c user.name=Probe -c user.email=probe@example.test commit -qm probe\n"
                    f"git -c http.postBuffer=1024 push -q {shlex.quote(url)} HEAD:refs/heads/main\n"
                    "git hash-object payload.bin\n",
                    timeout=180,
                )
                or {}
            )
            if result.get("returncode") != 0:
                return False
            remote = api(
                "HEAD", path + "/repository/files/payload.bin", params={"ref": "main"}
            )
            return (
                remote.headers.get("X-Gitlab-Blob-Id")
                == (result.get("output") or "").strip()
            )
        finally:
            api("DELETE", path)


async def run_cdp_probe(base: str, record: dict, args: argparse.Namespace) -> None:
    async with aiohttp.ClientSession() as session:
        version = await _wait_for_cdp(session, base, args.cdp_timeout_seconds)
        target = await _page_target(session, base)
        record["cdp"] = {
            "endpoint": base,
            "browser_version": version,
            "target": {key: target.get(key) for key in ("id", "type", "title", "url")},
            "ws_url": target.get("webSocketDebuggerUrl"),
        }
        async with session.ws_connect(
            target["webSocketDebuggerUrl"], max_msg_size=0
        ) as ws:
            cdp = CdpSession(ws)
            cdp.start()
            # Enabled before the first navigation, on the one target every
            # origin is loaded into.
            record["cdp"]["enable_errors"] = {
                domain: (await cdp.call(f"{domain}.enable")).get("error")
                for domain in CDP_DOMAINS
            }
            try:
                for label, url in ORIGINS:
                    entry = await _probe_origin(cdp, label, url, args)
                    record["origins"].append(entry)
                    print(
                        f"{label}: secure={entry['values'].get('secure')} "
                        f"clipboard={entry['values'].get('clipboard')} "
                        f"state={entry['security'].get('value')} "
                        f"title={entry['values'].get('title')!r} "
                        f"findings={len(entry['log_findings'])} "
                        f"requests={entry['network']['total_requests']} "
                        f"insecure={len(entry['network']['insecure_requests'])}",
                        flush=True,
                    )
                    if label == "teamchat":
                        record["teamchat"] = await _teamchat_extras(
                            cdp, _origin_of(url), args
                        )
                    if label == "cloudcrm":
                        cookie = await _evaluate(cdp, "document.cookie")
                        entry["cookie_visible"] = cookie["value"]
                        entry["cookie_read_error"] = cookie["error"]
                    if label == "studio-streamview":
                        record["streamview_upload"] = await _streamview_upload(cdp)
            finally:
                record["cdp"]["event_count"] = len(cdp.events)
                record["cdp"]["event_methods"] = cdp.event_methods()
                await cdp.close()


def _port_9222_listening(controller) -> bool | None:
    """Whether the baked osworld-cdp.service already holds :9222 in the guest.
    Recorded so the socat launch below is known to be a no-op or not."""
    result = (
        controller.run_bash_script(
            "ss -ltn 2>/dev/null | grep -c ':9222 ' || true", timeout=30
        )
        or {}
    )
    text = (result.get("output") or "").strip()
    try:
        return int(text.splitlines()[-1]) > 0
    except (IndexError, ValueError):
        return None


def _acceptance(record: dict) -> dict:
    acceptance: dict = {
        "streamview.upload_roundtrip": (record.get("streamview_upload") or {}).get(
            "value"
        )
        is True,
        "gitlab.push_roundtrip": record.get("gitlab_push") is True,
    }
    # Every CDP domain enabled cleanly. `cdp.call` returns protocol errors and
    # timeouts rather than raising, so a failed `Log.enable` or `Network.enable`
    # would leave the collectors silent and every "no mixed content" / "all
    # subresources HTTPS" boolean passing on an empty buffer. Without this the
    # strongest checks in the probe could be vacuously green.
    enable_errors = (record.get("cdp") or {}).get("enable_errors")
    acceptance["cdp_domains_enabled"] = bool(enable_errors) and all(
        error is None for error in enable_errors.values()
    )
    # Every requested origin was actually probed. A mid-loop abort must fail
    # explicitly rather than incidentally because some particular origin
    # happened to be last and carried a boolean of its own.
    acceptance["all_origins_probed"] = len(record["origins"]) == len(
        record.get("origins_requested") or ORIGINS
    )

    for entry in record["origins"]:
        label, values = entry["label"], entry.get("values") or {}
        title = values.get("title")
        # The navigation actually landed on the origin being scored. Without
        # these two, a failed navigation that left the PREVIOUS document loaded
        # would be scored against that previous page and could pass every
        # check -- real title, clipboard present, securityState secure, no new
        # findings -- while saying nothing at all about the origin it claims.
        acceptance[f"{label}.loaded"] = entry.get("load_event_fired") is True
        acceptance[f"{label}.url_origin_matches"] = bool(
            isinstance(values.get("url"), str)
            and _origin_of(values["url"])
            and _origin_of(values["url"]) == _origin_of(entry["requested_url"])
        )
        acceptance[f"{label}.secure"] = values.get("secure") is True
        acceptance[f"{label}.clipboard_object"] = values.get("clipboard") == "object"
        acceptance[f"{label}.security_state_secure"] = (
            entry.get("security") or {}
        ).get("value") == "secure"
        acceptance[f"{label}.title_not_interstitial"] = bool(
            isinstance(title, str) and title.strip() and INTERSTITIAL_TITLE not in title
        )
        network = entry.get("network") or {}
        # Two independent readings of the same question. The log/console one is
        # what the brief pins; CDP's own `blockedReason: mixed-content` on a
        # failed load is the most authoritative signal there is, so a block that
        # Chrome reported only there must fail this too. Folding it in can only
        # tighten the check.
        acceptance[f"{label}.no_mixed_content"] = not entry.get(
            "log_findings"
        ) and not network.get("mixed_content_blocked")
        # The positive form: every subresource the page actually fetched came
        # over a secure transport. `total_requests > 0` guards against a vacuous
        # pass -- an empty request list would otherwise satisfy "no insecure
        # requests" without the Network domain ever having reported anything.
        # A real navigation always produces at least its own document request.
        acceptance[f"{label}.all_subresources_https"] = bool(
            network.get("total_requests") and not network.get("insecure_requests")
        )

    teamchat = record.get("teamchat") or {}
    acceptance["teamchat.notifications_granted"] = (
        teamchat.get("notification_permission_after_grant") == "granted"
    )
    acceptance["teamchat.clipboard_roundtrip_probe"] = (
        teamchat.get("clipboard_roundtrip") or {}
    ).get("value") == "probe"

    cloudcrm = next(
        (entry for entry in record["origins"] if entry["label"] == "cloudcrm"), None
    )
    acceptance["cookie_isolation_teamchat_to_cloudcrm"] = bool(
        cloudcrm is not None
        and isinstance(cloudcrm.get("cookie_visible"), str)
        and "probe" not in cloudcrm["cookie_visible"]
        and "probe=1" in (teamchat.get("cookie_after_set") or "")
    )

    gitlab = next(
        (entry for entry in record["origins"] if entry["label"] == "gitlab-041"), None
    )
    gitlab_title = ((gitlab or {}).get("values") or {}).get("title")
    acceptance["gitlab_041_title_contains_gitlab"] = bool(
        isinstance(gitlab_title, str) and "GitLab" in gitlab_title
    )
    return acceptance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out/osworld-v2-raw"),
        help="raw (gitignored) output directory",
    )
    parser.add_argument(
        "--cdp-timeout-seconds",
        type=float,
        default=90.0,
        help="bounded poll of /json/version after the Chrome launch",
    )
    parser.add_argument("--navigate-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--load-timeout-seconds",
        type=float,
        default=60.0,
        help="bounded wait for Page.loadEventFired; a miss is recorded, not fatal",
    )
    parser.add_argument(
        "--quiet-seconds",
        type=float,
        default=3.0,
        help="settle time after load so late console/log entries are collected",
    )
    parser.add_argument("--clipboard-timeout-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template = os.environ["GUEST_TEMPLATE"]
    build_id = template.split(":", 1)[1] if ":" in template else template
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Before any guest exists, so a drifted origin list costs nothing and
    # cannot produce a record at all.
    origin_literal_check = _origin_literal_check()

    record: dict = {
        "schema_version": 1,
        "purpose": (
            "guest-browser secure-context probe over the campaign HTTPS fleet "
            "origins; not a benchmark score"
        ),
        "started_at": utc_now(),
        "template": template,
        "build_id": build_id,
        "campaign_id": os.environ.get("OSWORLD_CAMPAIGN_ID"),
        "website_host_suffix": os.environ.get("WEBSITE_HOST_SUFFIX"),
        "origin_literal_check": origin_literal_check,
        "origins_requested": [{"label": label, "url": url} for label, url in ORIGINS],
        "chrome_launch": CHROME_LAUNCH,
        "socat_launch": SOCAT_LAUNCH,
        "host": {"python": platform.python_version(), "platform": platform.platform()},
        "origins": [],
    }

    env = None
    try:
        env = DesktopEnv(
            provider_name="e2b",
            os_type="Ubuntu",
            action_space="pyautogui",
            client_password="osworld-public-evaluation",
            require_a11y_tree=False,
            require_terminal=False,
            screen_size=(1920, 1080),
            headless=True,
            enable_proxy=False,
        )
        record["bridge"] = env.provider.bridge.state()

        # Everything past a successfully created guest is recorded rather than
        # raised: a probe that dies mid-run must still leave the JSON it got so
        # far, because that JSON is the evidence. A DesktopEnv construction
        # failure above is deliberately left to propagate -- there is nothing to
        # record and the real exception must not be masked.
        try:
            # Chrome runs as `user` on DISPLAY=:0 through the guest server's
            # /setup/launch, exactly as in a task.
            env.setup_controller.launch(CHROME_LAUNCH)
            record["cdp_9222_listening_before_socat"] = _port_9222_listening(
                env.controller
            )
            env.setup_controller.launch(SOCAT_LAUNCH)

            base = f"http://127.0.0.1:{env.provider.bridge.cdp_port}"
            asyncio.run(run_cdp_probe(base, record, args))
            record["gitlab_push"] = _gitlab_push(env.controller)
        except Exception as error:  # noqa: BLE001 - recorded, then reported by exit code
            record["error"] = {"type": type(error).__name__, "message": str(error)}
            print(f"probe error: {type(error).__name__}: {error}", file=sys.stderr)
    finally:
        if env is not None:
            # The close must still be attempted, but it must not be able to
            # discard the evidence: everything below -- finished_at, the
            # acceptance evaluation, the JSON write -- happens after this
            # block, so an exception escaping here would throw away the whole
            # record the run exists to produce. Recorded loudly instead, since
            # a failed close can mean a guest left running.
            try:
                env.close()
            except Exception as error:  # noqa: BLE001 - recorded, never fatal
                record["close_error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                print(
                    f"WARNING: env.close() failed ({type(error).__name__}: {error}); "
                    "the guest may still be running",
                    file=sys.stderr,
                )

    record["finished_at"] = utc_now()
    record["acceptance"] = _acceptance(record)
    record["passed"] = all(value is True for value in record["acceptance"].values())
    output = output_dir / f"browser-probe-{build_id}.json"
    output.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps(record["acceptance"], indent=2, sort_keys=True))
    print(f"wrote {output}")
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
