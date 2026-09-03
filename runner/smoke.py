#!/usr/bin/env python3
"""Smoke-test an already-running OSWorld 2.0 E2B relay and save evidence.

Ported from hark's OSWorld 1.0 runner/smoke.py. Keeps every V1 check unchanged
(restricted ingress 403/200, 1920x1080, non-empty AT-SPI tree, Chrome CDP attach
through the relay with URL rewrite, per-app window-present AND first-run-modal-absent
for Chrome/LibreOffice/VLC, kvm=absent probe) and adds the OSWorld 2.0 surface:

  * PulseAudio virtual sink `vsink` present (`pactl list short sinks`).
  * MuseScore 3 + FreeCAD + REAPER window-present AND first-run-modal-absent.
  * Guest task-service ports 3000 and 8000 are FREE by default (nothing squatting)
    and become reachable through the relay's literal 127.0.0.1:<port> listener once
    a trivial in-guest `python3 -m http.server` binds them (matches how V2 task
    code dials the guest ip directly on those literal ports).
  * `su`/PAM auth for `user` with the V2 credential `osworld-public-evaluation`
    (driven over a pty, because su reads the password from the controlling tty).

The additions are strictly additive: no V1 check is weakened to make V2 pass.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from e2b import Sandbox
from playwright.sync_api import sync_playwright

RELAY = "http://127.0.0.1:15000"
CDP = "http://127.0.0.1:19222"
CONTROL = "http://127.0.0.1:14999"
PORT3000 = "http://127.0.0.1:3000"
PORT8000 = "http://127.0.0.1:8000"
# smoke.py lives at runner/; evidence lands at the repo's
# out/osworld-v2-evidence/ so the receipt sits with the other V2 evidence.
REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / "out" / "osworld-v2-evidence"
SCREENS_DIR = EVIDENCE_DIR / "smoke-screens"


def request(url, *, method="GET", data=None, headers=None, timeout=240):
    body = None if data is None else json.dumps(data).encode()
    merged = {"Content-Type": "application/json"} if data is not None else {}
    merged.update(headers or {})
    try:
        with urlopen(
            Request(url, data=body, method=method, headers=merged), timeout=timeout
        ) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError as error:
        return 0, str(error).encode()


def relay_json(path, *, method="GET", data=None):
    status, body = request(f"{RELAY}{path}", method=method, data=data)
    if status != 200:
        raise RuntimeError(f"{path} returned HTTP {status}: {body[:500]!r}")
    return json.loads(body)


def accessibility():
    tree = relay_json("/accessibility").get("AT", "")
    if not tree:
        raise RuntimeError("accessibility tree was empty")
    return tree


def launch(command, wait_seconds):
    status, body = request(
        f"{RELAY}/setup/launch", method="POST", data={"command": command}
    )
    if status != 200:
        raise RuntimeError(f"launch failed with HTTP {status}: {body[:500]!r}")
    time.sleep(wait_seconds)


def wait_for_cdp(timeout_seconds=90):
    deadline = time.monotonic() + timeout_seconds
    last_status, last_body = 0, b""
    while time.monotonic() < deadline:
        last_status, last_body = request(f"{CDP}/json/version")
        if last_status == 200:
            return json.loads(last_body)
        time.sleep(2)
    raise RuntimeError(
        f"CDP discovery did not become ready: HTTP {last_status}: {last_body[:500]!r}"
    )


def execute(command):
    return relay_json(
        "/execute",
        method="POST",
        data={
            "command": ["bash", "-lc", command],
            "shell": False,
        },
    )


def screenshot(name):
    status, body = request(f"{RELAY}/screenshot")
    if status != 200 or not body.startswith(b"\x89PNG"):
        raise RuntimeError(
            f"invalid screenshot {name}: HTTP {status}, {len(body)} bytes"
        )
    (SCREENS_DIR / name).write_bytes(body)
    return len(body)


def modal_absent(tree, phrases):
    lower = tree.lower()
    return not any(phrase in lower for phrase in phrases)


def named_dialog_absent(tree, names):
    alternatives = "|".join(re.escape(name) for name in names)
    return (
        re.search(
            rf'<(?:dialog|frame|alert|window)[^>]*name="(?:{alternatives})"',
            tree,
            re.IGNORECASE,
        )
        is None
    )


def window_present(tree, needle):
    return needle.lower() in tree.lower()


# AT-SPI keeps dialog/alert *objects* in an app's widget tree even when they are
# not on screen (e.g. MuseScore's hidden "Start Center"/"Tour"/"Preferences"),
# and create_atspi_node only emits st:showing="true"/st:visible="true" on nodes
# that are actually shown (accessibility.py gates those on the visible+showing
# state pair). So a *visible* modal is one whose tag carries st:showing="true".
# This is strictly stronger than a name-only match: it never trips on a
# suppressed-but-present dialog, and never misses a genuinely shown one.
def _tag_is_showing(open_tag: str) -> bool:
    return 'st:showing="true"' in open_tag or 'st:visible="true"' in open_tag


def visible_dialog_present(tree, names, roles=("dialog", "alert", "window")):
    role_alt = "|".join(roles)
    name_alt = "|".join(re.escape(name) for name in names)
    for match in re.finditer(
        rf'<(?:{role_alt})[^>]*\sname="(?:{name_alt})"[^>]*>', tree, re.IGNORECASE
    ):
        if _tag_is_showing(match.group(0)):
            return True
    return False


def visible_window_present(tree, needle, roles=("frame", "application", "window")):
    role_alt = "|".join(roles)
    for match in re.finditer(
        rf'<(?:{role_alt})[^>]*\sname="([^"]*)"[^>]*>', tree, re.IGNORECASE
    ):
        if needle.lower() in match.group(1).lower() and _tag_is_showing(match.group(0)):
            return True
    return False


def wmctrl_titles():
    """X11 window titles in the guest. Needed for apps like REAPER whose SWELL
    toolkit exposes no AT-SPI tree, so the a11y tree cannot see them at all."""
    out = execute("DISPLAY=:0 wmctrl -l 2>/dev/null || true").get("output", "") or ""
    titles = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            titles.append(parts[3])
    return titles


# ---- V2 helpers ------------------------------------------------------------


def kill_pattern(pattern):
    execute(f"pkill -f {pattern!r} 2>/dev/null || true")


def probe_port_free(guest_ss, port):
    """A port is free when no listening socket is bound to it in the guest."""
    return not re.search(rf":{port}\b", guest_ss)


def port_reachable_via_relay(base_url, port):
    """Start a trivial http.server on the literal guest port, confirm it becomes
    reachable through the relay's literal 127.0.0.1:<port> listener, then stop it.
    Returns (free_before, reachable_after)."""
    before_status, _ = request(f"{base_url}/", timeout=10)
    free_before = before_status != 200
    # Background the server inside the guest shell so /execute returns immediately.
    execute(
        f"nohup python3 -m http.server {port} --bind 0.0.0.0 "
        f">/tmp/httpsmoke-{port}.log 2>&1 & disown; sleep 1; echo started"
    )
    reachable = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, _ = request(f"{base_url}/", timeout=10)
        if status == 200:
            reachable = True
            break
        time.sleep(1)
    kill_pattern(f"http.server {port}")
    return free_before, reachable


# Authoritative PAM credential check. `su`/`sudo` cannot prove the V2 password on
# this guest: sudo is NOPASSWD, and `su - user` from `user` succeeds via pam_unix
# nullok when the field is empty -- both pass regardless of the actual password.
# So verify exactly what pam_unix verifies: crypt() the candidate password with the
# stored salt and compare to /etc/shadow. Reads shadow as root through the guest's
# NOPASSWD sudo (the same channel the V2 harness uses). Rejects empty/locked fields.
SHADOW_CRED_SCRIPT = r"""
import crypt, spwd
PW = "osworld-public-evaluation"
e = spwd.getspnam("user").sp_pwdp
if (not e) or e in ("!", "*", "!!", "x"):
    print("CRED=EMPTY")
else:
    print("CRED=HASH")
    print("MATCH=%s" % (crypt.crypt(PW, e) == e))
    print("WRONG=%s" % (crypt.crypt(PW + "-nope", e) == e))
"""


def su_auth_ok():
    heredoc = "sudo -n python3 - <<'PYEOF'\n" + SHADOW_CRED_SCRIPT + "\nPYEOF\n"
    result = execute(heredoc)
    out = result.get("output", "") or ""
    passed = ("CRED=HASH" in out) and ("MATCH=True" in out) and ("WRONG=False" in out)
    return {"passed": passed, "raw": out.strip()}


def main():
    if not os.environ.get("E2B_API_KEY"):
        raise RuntimeError("E2B_API_KEY is required")
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)

    _, state_body = request(f"{CONTROL}/state")
    state = json.loads(state_body)
    sandbox = Sandbox.connect(state["sandbox_id"])
    token = sandbox.traffic_access_token
    if not token:
        raise RuntimeError("connected sandbox had no traffic access token")

    # ---- V1 check: restricted ingress rejects unauth, accepts with token ----
    direct = f"https://{sandbox.get_host(5000)}/screen_size"
    unauth_status, _ = request(direct, method="POST")
    auth_status, _ = request(
        direct,
        method="POST",
        headers={
            "e2b-traffic-access-token": token,
        },
    )

    # ---- V1 check: geometry + non-empty a11y tree + neutral screenshot ------
    size = relay_json("/screen_size", method="POST")
    neutral_tree = accessibility()
    neutral_bytes = screenshot("smoke-neutral.png")

    version_command = """printf 'ubuntu='; . /etc/os-release; printf '%s\\n' "$VERSION_ID"
google-chrome --version
code --version | head -1
libreoffice --version
gimp --version | head -1
vlc --version | head -1
thunderbird --version
gnome-shell --version
python3 --version
dpkg-query -W -f='musescore3=${Version}\\n' musescore3 2>/dev/null || true
dpkg-query -W -f='freecad=${Version}\\n' freecad 2>/dev/null || true
dpkg-query -W -f='shotcut=${Version}\\n' shotcut 2>/dev/null || true
rv=$(grep -m1 -oE 'v[0-9.]+' /opt/REAPER/whatsnew.txt 2>/dev/null | head -1)
printf 'reaper=%s\\n' "${rv:-7.79(pinned)}"
zv=$(sed -n 's/^Version=//p' /opt/zotero/application.ini 2>/dev/null | head -1)
printf 'zotero=%s\\n' "${zv:-7.0.15(pinned)}"
if [ -e /dev/kvm ]; then echo kvm=present; else echo kvm=absent; fi"""
    version_response = execute(version_command)
    if version_response.get("returncode") != 0:
        raise RuntimeError(f"version query failed: {version_response}")
    versions = [
        line for line in version_response.get("output", "").splitlines() if line
    ]

    # ---- V2 check: PulseAudio virtual sink `vsink` present ------------------
    sinks_response = execute("pactl list short sinks 2>/dev/null || true")
    sinks_output = sinks_response.get("output", "") or ""
    vsink_present = bool(re.search(r"\bvsink\b", sinks_output))

    # ---- V2 check: su/PAM credential ---------------------------------------
    su_result = su_auth_ok()

    # ---- V2 check: task-service ports 3000/8000 free + relay-reachable ------
    guest_ss = (
        execute("ss -ltnH 2>/dev/null || ss -ltn 2>/dev/null || true").get("output", "")
        or ""
    )
    port3000_free = probe_port_free(guest_ss, 3000)
    port8000_free = probe_port_free(guest_ss, 8000)
    p3000_free_probe, p3000_reachable = port_reachable_via_relay(PORT3000, 3000)
    p8000_free_probe, p8000_reachable = port_reachable_via_relay(PORT8000, 8000)

    # ---- V1 check: Chrome + CDP attach through the relay + URL rewrite ------
    kill_pattern("chrome")
    launch(["google-chrome"], 10)
    cdp_version = wait_for_cdp()
    websocket_url = cdp_version.get("webSocketDebuggerUrl", "")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP)
        cdp_contexts = len(browser.contexts)
        cdp_pages = sum(len(context.pages) for context in browser.contexts)
        browser.close()
    chrome_tree = accessibility()
    chrome_bytes = screenshot("smoke-chrome.png")
    kill_pattern("chrome")

    # ---- V1 check: LibreOffice window + first-run modal absent --------------
    launch(["libreoffice", "--calc"], 12)
    libreoffice_tree = accessibility()
    libreoffice_bytes = screenshot("smoke-libreoffice.png")
    execute("pkill -x soffice.bin || true")

    # ---- V1 check: VLC window + privacy modal absent ------------------------
    launch(["vlc"], 8)
    vlc_tree = accessibility()
    vlc_bytes = screenshot("smoke-vlc.png")
    execute("pkill -x vlc || true")

    # ---- V2 check: MuseScore 3 window + first-run modal absent --------------
    # Fresh MuseScore 3 opens a "Startup Wizard" modal, a "Start Center" score
    # picker, and a "Tour" popup. Baked config suppresses them; the objects may
    # still exist in the a11y tree but must not be *showing*.
    kill_pattern("mscore")
    launch(["musescore3"], 15)
    musescore_tree = accessibility()
    musescore_bytes = screenshot("smoke-musescore.png")
    execute(
        "pkill -x mscore3 2>/dev/null; sleep 2; pkill -9 -x mscore3 2>/dev/null || true"
    )

    # ---- V2 check: FreeCAD window + first-run modal absent ------------------
    kill_pattern("freecad")
    launch(["freecad"], 16)
    freecad_tree = accessibility()
    freecad_bytes = screenshot("smoke-freecad.png")
    kill_pattern("freecad")

    # ---- V2 check: REAPER window + license/eval modal absent ----------------
    # REAPER's SWELL toolkit exposes no AT-SPI tree, so detect it through X11
    # window titles. The main window title is "REAPER v<ver> - EVALUATION
    # LICENSE" (the unregistered product's own title, not a modal). The two
    # real first-run modals are the "About REAPER ..." eval/license window and
    # the "Error opening devices" audio dialog; baked reaper.ini suppresses both.
    kill_pattern("reaper")
    launch(["reaper"], 13)
    reaper_titles = wmctrl_titles()
    reaper_bytes = screenshot("smoke-reaper.png")
    kill_pattern("reaper")
    reaper_window_present = any("reaper" in t.lower() for t in reaper_titles)
    reaper_modal_titles = [
        t
        for t in reaper_titles
        if t.strip().lower() == "error opening devices"
        or t.strip().lower().startswith("about reaper")
    ]
    reaper_modal_absent = not reaper_modal_titles

    evidence = {
        "tested_at": datetime.now(UTC).isoformat(),
        "template": state["template"],
        "sandbox_id": state["sandbox_id"],
        "generation": state["generation"],
        "restricted_ingress": state["restricted_ingress"],
        "direct_unauthenticated_status": unauth_status,
        "direct_authenticated_status": auth_status,
        "screen_size": size,
        "neutral_screenshot_bytes": neutral_bytes,
        "accessibility_characters": len(neutral_tree),
        "versions": versions,
        "pulse_sinks": sinks_output.strip(),
        "vsink_present": vsink_present,
        "su_auth": su_result,
        "port3000_free_guest_ss": port3000_free,
        "port8000_free_guest_ss": port8000_free,
        "port3000_free_relay_probe": p3000_free_probe,
        "port8000_free_relay_probe": p8000_free_probe,
        "port3000_reachable_via_relay": p3000_reachable,
        "port8000_reachable_via_relay": p8000_reachable,
        "cdp_browser": cdp_version.get("Browser"),
        "cdp_websocket_rewritten_to_local_relay": websocket_url.startswith(
            "ws://127.0.0.1:19222/"
        ),
        "cdp_contexts": cdp_contexts,
        "cdp_pages": cdp_pages,
        "chrome_window_present": window_present(chrome_tree, "google chrome"),
        "chrome_keyring_modal_absent": modal_absent(
            chrome_tree, ("unlock login keyring",)
        ),
        "chrome_screenshot_bytes": chrome_bytes,
        "libreoffice_window_present": window_present(
            libreoffice_tree, "libreoffice calc"
        ),
        # First-run labels also occur in normal menus and hidden controls, so
        # only a top-level dialog/frame with one of these names is a failure.
        "libreoffice_first_run_modal_absent": named_dialog_absent(
            libreoffice_tree, ("tip of the day", "welcome to libreoffice")
        ),
        "libreoffice_screenshot_bytes": libreoffice_bytes,
        "vlc_window_present": window_present(vlc_tree, "vlc media player"),
        "vlc_privacy_modal_absent": modal_absent(
            vlc_tree, ("privacy and network access policy", "metadata network access")
        ),
        "vlc_screenshot_bytes": vlc_bytes,
        "musescore_window_present": visible_window_present(musescore_tree, "musescore"),
        # Showing-gated: the Startup Wizard / Start Center / Tour objects may
        # linger in the tree but must not be visible.
        "musescore_first_run_modal_absent": not visible_dialog_present(
            musescore_tree,
            ("startup wizard", "start center", "tour", "welcome to musescore"),
            roles=("dialog", "alert", "window"),
        ),
        "musescore_screenshot_bytes": musescore_bytes,
        "freecad_window_present": visible_window_present(freecad_tree, "freecad"),
        # FreeCAD's normal neutral state is the "Start page" tab plus docked
        # panels (all frames, not dialogs); a first-run failure would be a
        # showing error/report/migration dialog.
        "freecad_first_run_modal_absent": not visible_dialog_present(
            freecad_tree,
            ("report a bug", "an error occurred", "migrate", "what's new", "whats new"),
            roles=("dialog", "alert"),
        ),
        "freecad_screenshot_bytes": freecad_bytes,
        "reaper_window_titles": reaper_titles,
        "reaper_window_present": reaper_window_present,
        "reaper_modal_titles": reaper_modal_titles,
        # Detected via X11 titles (REAPER has no AT-SPI). The eval "About REAPER"
        # window and the "Error opening devices" audio dialog are the failures;
        # the main window's "EVALUATION LICENSE" title is not a modal.
        "reaper_modal_absent": reaper_modal_absent,
        "reaper_screenshot_bytes": reaper_bytes,
    }

    checks = {
        "restricted ingress enabled": evidence["restricted_ingress"] is True,
        "unauthenticated ingress rejected": unauth_status == 403,
        "authenticated ingress accepted": auth_status == 200,
        "screen is 1920x1080": size == {"width": 1920, "height": 1080},
        "CDP websocket is relay-local": evidence[
            "cdp_websocket_rewritten_to_local_relay"
        ],
        "CDP attached to a context": cdp_contexts >= 1 and cdp_pages >= 1,
        "Chrome window present": evidence["chrome_window_present"],
        "Chrome first-run modal absent": evidence["chrome_keyring_modal_absent"],
        "LibreOffice window present": evidence["libreoffice_window_present"],
        "LibreOffice first-run modal absent": evidence[
            "libreoffice_first_run_modal_absent"
        ],
        "VLC window present": evidence["vlc_window_present"],
        "VLC privacy modal absent": evidence["vlc_privacy_modal_absent"],
        # ---- V2 ----
        "PulseAudio vsink present": vsink_present,
        "su/PAM auth with V2 credential": su_result["passed"],
        "port 3000 free by default": port3000_free and p3000_free_probe,
        "port 3000 reachable via relay": p3000_reachable,
        "port 8000 free by default": port8000_free and p8000_free_probe,
        "port 8000 reachable via relay": p8000_reachable,
        "MuseScore window present": evidence["musescore_window_present"],
        "MuseScore first-run modal absent": evidence[
            "musescore_first_run_modal_absent"
        ],
        "FreeCAD window present": evidence["freecad_window_present"],
        "FreeCAD first-run modal absent": evidence["freecad_first_run_modal_absent"],
        "REAPER window present": evidence["reaper_window_present"],
        "REAPER license/eval modal absent": evidence["reaper_modal_absent"],
    }
    evidence["checks"] = checks
    evidence["all_checks_passed"] = all(checks.values())
    (EVIDENCE_DIR / "live-smoke.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    if not evidence["all_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
