#!/usr/bin/env python3
"""Open each parity application on a fresh guest and record windows + screenshots.

Usage: GUEST_TEMPLATE=osworld-v2-gnome:<build-id> uv run --locked python maintainer/app_smoke.py
Writes out/osworld-v2-raw/app-smoke-<build-id>/{report.json,<app>.png}.
No model calls. Kills the sandbox on exit.

`window_found` only says a window with the expected title fragment existed. It
is not evidence that the document rendered, and it does not clear a first-run
dialog: KiCad's own first-run wizard is titled "KiCad Setup", which matches
"KiCad". `first_run_titles` catches that shape, but it cannot catch a first-run
panel drawn inside the application window rather than in a window of its own -
FreeCAD's "Welcome to FreeCAD" page is exactly that, and it leaves the window
list looking clean. The screenshots are the evidence, and a human reads every
one of them before a run counts as a pass.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from e2b import Sandbox

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tasks" / "assets"
# app -> (local input file or None, window title fragment observed on the guest)
# Each entry is the command and argument the release task itself invokes:
# task 067 `musescore <score>`, task 079 `wpp <deck>`, task 092 `blender`,
# task 107 `kicad <project>`, tasks 103/104 `freecad`.
APPS = {
    "musescore": (ASSETS / "task_067" / "music_init.mscz", "MuseScore"),
    # WPS Presentation titles its window "<file> - Presentation"; "WPS" appears
    # only on the tab strip inside the window, never in the WM title.
    "wpp": (ASSETS / "task_079" / "nexachain_pitch_template.pptx", "Presentation"),
    "blender": (None, "Blender"),
    "kicad": (ASSETS / "task_107" / "heart_rate_seed_project.zip", "KiCad"),
    "freecad": (None, "FreeCAD"),
}
# The X session the guest server screenshots and the tasks act on.
DISPLAY_ENV = {"DISPLAY": ":0"}
# Time for the application to finish painting its document after its window is
# mapped; a screenshot taken the instant wmctrl sees the title catches a blank
# frame and proves nothing. WPS Presentation maps its window in ~19s but is
# still at "Opening: ... 40%" on task 079's 3.3 MB deck 20s later, and
# FreeCAD maps an unpainted window well before it has drawn anything.
SETTLE_S = 45
# Window titles that mean a first-run/licence dialog is still on screen.
FIRST_RUN_MARKERS = ("setup", "welcome", "wizard", "license", "licence", "agreement")
DESKTOP = "/home/user/Desktop"


def _windows(sbx: Sandbox) -> str:
    # commands.run does not interpret a `VAR=x cmd` prefix as an assignment the
    # command inherits; environment has to go through envs=.
    return sbx.commands.run(
        "wmctrl -l", user="user", envs=DISPLAY_ENV, timeout=30
    ).stdout


def main() -> int:
    template = os.environ["GUEST_TEMPLATE"]
    build_id = template.split(":", 1)[1]
    out = ROOT / "out" / "osworld-v2-raw" / f"app-smoke-{build_id}"
    out.mkdir(parents=True, exist_ok=True)
    sbx = Sandbox.create(
        template, timeout=1800, metadata={"workload": "osworld-v2-app-smoke"}
    )
    report = {"template": template, "sandbox_id": sbx.sandbox_id, "apps": {}}
    print(f"sandbox {sbx.sandbox_id} from {template}", flush=True)
    try:
        server = f"https://{sbx.get_host(5000)}"
        headers = {"e2b-traffic-access-token": sbx.traffic_access_token}
        for _ in range(60):
            try:
                if requests.get(f"{server}/screenshot", headers=headers, timeout=10).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(3)
        # A fresh guest has no ~/Desktop. Create it as `user` first: anything
        # that creates it as root (files.write does) leaves the applications,
        # which run as `user`, unable to write their lock and backup files
        # beside the document, and WPS Presentation then never paints the deck.
        sbx.commands.run(f"mkdir -p {DESKTOP}", user="user", timeout=30)
        for app, (local, title) in APPS.items():
            args = [app]
            if local is not None:
                remote = f"{DESKTOP}/{local.name}"
                sbx.files.write(remote, local.read_bytes())
                sbx.commands.run(
                    f"chown -R user:user {DESKTOP}", user="root", timeout=60
                )
                if local.suffix == ".zip":
                    sbx.commands.run(
                        f"cd {DESKTOP} && rm -rf smoke_{app} && mkdir smoke_{app} "
                        f"&& unzip -o -q {local.name} -d smoke_{app}",
                        user="user",
                        timeout=120,
                    )
                    found = sbx.commands.run(
                        f"find {DESKTOP}/smoke_{app} -name '*.kicad_pro' | head -1",
                        user="user",
                        timeout=30,
                    ).stdout.strip()
                    remote = found or remote
                args.append(remote)
            t0 = time.time()
            launch = requests.post(
                f"{server}/setup/launch",
                json={"command": args, "shell": False},
                headers=headers,
                timeout=60,
            )
            windows = ""
            # FreeCAD's extracted AppImage takes ~90s to map its window on a
            # cold guest, so this waits well past that before giving up.
            for _ in range(60):
                windows = _windows(sbx)
                if title.lower() in windows.lower():
                    break
                time.sleep(3)
            seconds_to_window = round(time.time() - t0, 1)
            time.sleep(SETTLE_S)
            windows = _windows(sbx)
            shot = requests.get(f"{server}/screenshot", headers=headers, timeout=30)
            (out / f"{app}.png").write_bytes(shot.content)
            titles = windows.strip().splitlines()
            report["apps"][app] = {
                "args": args,
                "launch_status": launch.status_code,
                "launch_body": launch.text.strip()[:200],
                "window_found": title.lower() in windows.lower(),
                "first_run_titles": [
                    t for t in titles if any(m in t.lower() for m in FIRST_RUN_MARKERS)
                ],
                "seconds_to_window": seconds_to_window,
                "settle_seconds": SETTLE_S,
                "screenshot_bytes": len(shot.content),
                "windows": titles,
            }
            print(json.dumps({app: report["apps"][app]}), flush=True)
            # The bracket makes the pattern miss this pkill's own `bash -l -c`
            # cmdline; a plain `pkill -f musescore` kills its own shell and the
            # SDK then raises on the -1 exit.
            sbx.commands.run(
                f"pkill -f '[{app[0]}]{app[1:]}' || true", user="user", timeout=30
            )
            time.sleep(2)
    finally:
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        sbx.kill()
        print(f"killed {sbx.sandbox_id}", flush=True)
    print(
        json.dumps(
            {
                a: r["window_found"] and not r["first_run_titles"]
                for a, r in report["apps"].items()
            }
        )
    )
    return (
        0
        if all(
            r["window_found"] and not r["first_run_titles"]
            for r in report["apps"].values()
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
