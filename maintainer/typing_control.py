#!/usr/bin/env python3
"""Isolated long-typing control for the patched controller deadline (blocks (g)+(h)).

Maintainer-only: not part of the benchmark path. It answers one question about
one guest build, with no agent, no task and no model call — does an agent action
that outlives the guest's own 120 s deadline now stop cleanly instead of
continuing to type under the next action?

What it does, once:
  1. Creates a single guest through the provider's in-process bridge, exactly as
     ``maintainer/harness.py`` does (``DesktopEnv(provider_name="e2b", ...)``
     starts the sandbox and builds the ``PythonController``).
  2. Opens a graphical text editor in the guest and focuses its text area.
  3. Issues ONE deliberately slow action — ``--presses`` (default 3,000) chained
     ``pyautogui.press()`` calls — through ``PythonController.execute_python_command``. With the
     guest's 0.1 s PyAutoGUI pause that is ~300 s of work behind a 120 s guest
     deadline, so the guest is certain to kill it.
  4. Records the wall time of that call and its return value, takes a screenshot
     5 s after the call returns and a second one 15 s later, and counts the
     guest's own ``POST /execute`` log lines across the action.
  5. Writes ``out/osworld-v2-raw/typing-control-<build-id>.json`` (raw output is
     gitignored; commit a sanitized copy under
     ``out/osworld-v2-evidence/controls/`` after review).

Acceptance criteria (all three must hold for the patched checkout; the script
evaluates them into ``acceptance`` and exits non-zero if any fails):

  * ``returned_none`` — ``execute_python_command`` returns ``None`` after roughly
    120–130 s. The guest kills the action at its own 120 s deadline and answers
    ``500`` with subprocess's ``timed out after`` text; block (h) breaks out of
    the retry loop so the call falls through to upstream's own ``return None``.
    A return at ~90 s means block (g) is missing; a return well past 130 s, or a
    dict, means the guest did not kill the action.
  * ``screenshots_match`` — the two screenshots are identical inside the text
    area. Typing stopped when the guest reported; if they differ, input is still
    arriving after the client moved on, which is the defect these patches fix.
    (The full frames are expected to differ: the GNOME clock ticks.)
  * ``single_execute`` — exactly one ``POST /execute`` reaches the guest for that
    action. More than one means the timeout ``500`` was retried and a partially
    applied action was replayed, which block (h) exists to prevent.

Not run in CI and not run by any ladder rung: it needs a live E2B guest build.
Point ``GUEST_TEMPLATE`` at the immutable ``name:build_id`` reference under test
and run it from the pinned checkout root, the same way ``maintainer/validate.sh``
launches ``harness.py``:

    GUEST_TEMPLATE=<name>:<build-id> uv run --env-file .env.local --locked \
        python /path/to/maintainer/typing_control.py \
            --output-dir /path/to/out/osworld-v2-raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


# desktop_env is imported from the pinned checkout, whose root must be the cwd
# (several upstream modules resolve repo-relative paths from it). harness.py does
# the same; keep both able to run directly.
sys.path.insert(0, os.getcwd())

from desktop_env.desktop_env import DesktopEnv  # noqa: E402

# One of these must exist in the guest; the first one found is used.
EDITOR_CANDIDATES = ("gnome-text-editor", "gedit", "xed", "mousepad")
# Conservative text-area crop for a maximized editor at 1920x1080: below the
# GNOME top bar and the editor's own header bar, above the status bar. The top
# bar carries a ticking clock, so the full frames always differ.
DEFAULT_TEXT_AREA = (0, 140, 1920, 980)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bash(controller, script: str, timeout: int = 30) -> dict:
    """Guest shell helper. Uses /run_bash_script, never /execute, so it cannot
    disturb the /execute count this control measures."""
    return controller.run_bash_script(script, timeout=timeout) or {}


def _pick_editor(controller) -> str:
    result = _bash(
        controller,
        "for candidate in " + " ".join(EDITOR_CANDIDATES) + "; do "
        'command -v "$candidate" && break; done',
    )
    editor = (result.get("output") or "").strip().splitlines()
    if not editor:
        raise RuntimeError(
            "no text editor in the guest; tried: " + ", ".join(EDITOR_CANDIDATES)
        )
    return editor[0]


def _count_execute_requests(controller, log_path: str) -> int | None:
    """The guest server's own access log records one line per POST /execute."""
    result = _bash(controller, f"grep -c 'POST /execute' {log_path} || true")
    text = (result.get("output") or "").strip()
    try:
        return int(text.splitlines()[-1])
    except (IndexError, ValueError):
        return None


def _crop(png: bytes, box: tuple[int, int, int, int]) -> bytes | None:
    """Text-area crop. Pillow ships with the checkout's environment; if it is
    missing the control still runs and records that only full frames compared."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(png)) as image:
        buffer = io.BytesIO()
        image.crop(box).save(buffer, format="PNG")
        return buffer.getvalue()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--presses", type=int, default=3000)
    parser.add_argument("--character", default="a")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out/osworld-v2-raw"),
        help="raw (gitignored) output directory",
    )
    parser.add_argument(
        "--guest-log",
        default="/tmp/osworld/server.log",
        help="guest control-server log (template/files/session_inner.sh writes it here)",
    )
    parser.add_argument(
        "--text-area",
        default=",".join(str(value) for value in DEFAULT_TEXT_AREA),
        help="left,top,right,bottom crop compared between the two screenshots",
    )
    parser.add_argument("--editor-settle-seconds", type=float, default=15.0)
    parser.add_argument("--first-screenshot-delay", type=float, default=5.0)
    parser.add_argument("--second-screenshot-delay", type=float, default=15.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template = os.environ["GUEST_TEMPLATE"]
    build_id = template.split(":", 1)[1] if ":" in template else template
    text_area = tuple(int(value) for value in args.text_area.split(","))
    if len(text_area) != 4:
        raise SystemExit("--text-area must be left,top,right,bottom")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / f"typing-control-{build_id}"
    frames_dir.mkdir(parents=True, exist_ok=True)

    record: dict = {
        "schema_version": 1,
        "purpose": (
            "isolated long-typing control for the disclosed controller patches "
            "(g)+(h); not a benchmark score"
        ),
        "started_at": utc_now(),
        "template": template,
        "build_id": build_id,
        "campaign_id": os.environ.get("OSWORLD_CAMPAIGN_ID"),
        "presses": args.presses,
        "host": {"python": platform.python_version(), "platform": platform.platform()},
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
        controller = env.controller

        editor = _pick_editor(controller)
        record["editor"] = editor
        controller.execute_python_command(
            f"import subprocess; subprocess.Popen([{editor!r}])"
        )
        time.sleep(args.editor_settle_seconds)
        # Focus the text area before typing into it.
        controller.execute_python_command("import pyautogui; pyautogui.click(960, 540)")
        time.sleep(2)

        execute_before = _count_execute_requests(controller, args.guest_log)

        # ONE upstream-style action: the shape mm_agents/m3/parser.py emits for
        # typed text, one pyautogui.press() per character.
        action = "import pyautogui; " + "; ".join(
            f"pyautogui.press({args.character!r})" for _ in range(args.presses)
        )
        record["action_characters"] = len(action)
        started = time.monotonic()
        returned = controller.execute_python_command(action)
        elapsed = time.monotonic() - started
        record["elapsed_seconds"] = round(elapsed, 2)
        record["returned"] = returned
        record["returned_type"] = type(returned).__name__

        time.sleep(args.first_screenshot_delay)
        first = controller.get_screenshot() or b""
        time.sleep(args.second_screenshot_delay)
        second = controller.get_screenshot() or b""
        (frames_dir / "after-return.png").write_bytes(first)
        (frames_dir / "after-return-plus-15s.png").write_bytes(second)

        first_area, second_area = _crop(first, text_area), _crop(second, text_area)
        record["screenshots"] = {
            "delays_seconds": [
                args.first_screenshot_delay,
                args.second_screenshot_delay,
            ],
            "text_area": list(text_area),
            "full_frame_sha256": [_sha256(first), _sha256(second)],
            "text_area_sha256": (
                [_sha256(first_area), _sha256(second_area)]
                if first_area is not None and second_area is not None
                else None
            ),
            "full_frames_identical": bool(first) and first == second,
        }

        execute_after = _count_execute_requests(controller, args.guest_log)
        record["execute_requests"] = {
            "before": execute_before,
            "after": execute_after,
            "delta": (
                execute_after - execute_before
                if execute_before is not None and execute_after is not None
                else None
            ),
        }

        record["acceptance"] = {
            # (h): the guest's timeout 500 is not retried, so the call falls
            # through to upstream's own `return None` at roughly 120-130 s.
            "returned_none": returned is None and 110.0 <= elapsed <= 145.0,
            # Typing stopped when the guest reported.
            "screenshots_match": (
                first_area == second_area
                if first_area is not None and second_area is not None
                else None
            ),
            # Exactly one /execute for the action: no replay of a partial action.
            "single_execute": record["execute_requests"]["delta"] == 1,
        }
    finally:
        if env is not None:
            env.close()

    record["finished_at"] = utc_now()
    record["passed"] = all(
        value is True for value in record.get("acceptance", {}).values()
    )
    output = output_dir / f"typing-control-{build_id}.json"
    output.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps(record.get("acceptance", {}), sort_keys=True, default=str))
    print(f"wrote {output}")
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
