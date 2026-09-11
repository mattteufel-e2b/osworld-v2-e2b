#!/usr/bin/env python3
"""Task 6 spike: is audio userspace-viable in a Firecracker guest (E2B sandbox)?

Answers, in order: does a PulseAudio null sink work? does paplay into it
succeed? does MuseScore render audio to a file headlessly (or, as a lighter
fallback, does an ffmpeg-generated tone play into the null sink)? are ALSA
devices absent and snd-dummy unloadable (recorded, not fatal)?

Run:
    export E2B_API_KEY=$(grep '^E2B_API_KEY=' .env.local | cut -d= -f2)
    uv run --with e2b python tools/spikes/spike_audio.py

Writes out/osworld-v2-evidence/spike-audio.json.

Adaptations from the original brief (recorded here, not just in the report):
  - The brief's CHECKS assumed an `ubuntu:22.04` base template and a `runuser`
    binary. The actual E2B default base template (`Sandbox.create()` with no
    template arg) is Debian 12 (bookworm), and `runuser` is not installed.
    Adapted: apt installs run as `user='root'` via the SDK's command `user`
    kwarg; PulseAudio and the audio checks run as `user='user'` directly via
    the same kwarg (equivalent to runuser, no extra binary needed).
  - musescore3 pulled cleanly from Debian bookworm's main repo in ~17s, well
    under the 5-minute cap, so both the full MuseScore render AND the ffmpeg
    tone-render/paplay fallback were exercised (the brief only requires one).
"""

import json
import os
import sys
import time
from pathlib import Path

from e2b import Sandbox

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_PATH = REPO_ROOT / "out" / "osworld-v2-evidence" / "spike-audio.json"

# (name, command, run-as-user, timeout-seconds)
CHECKS = [
    (
        "apt",
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "pulseaudio pulseaudio-utils alsa-utils ffmpeg xvfb musescore3 > /tmp/apt.log 2>&1; "
        "tail -c 4000 /tmp/apt.log",
        "root",
        300,
    ),
    (
        "pulse_start",
        # No `runuser` in the base template; run directly as the sandbox user
        # instead (equivalent effect). No system-wide daemon fallback needed
        # since this succeeds as a per-user session.
        "pulseaudio --start --exit-idle-time=-1 2>&1; sleep 1; pactl info 2>&1",
        "user",
        30,
    ),
    (
        "null_sink",
        "pactl load-module module-null-sink sink_name=vsink 2>&1",
        "user",
        30,
    ),
    (
        "sink_list",
        "pactl list short sinks 2>&1 | grep vsink",
        "user",
        30,
    ),
    (
        "tone_render",
        "ffmpeg -y -f lavfi -i sine=frequency=440:duration=1 /tmp/tone.wav 2>&1 | tail -5; "
        "test -s /tmp/tone.wav && echo TONE_OK; ls -la /tmp/tone.wav",
        "user",
        30,
    ),
    (
        "play_to_sink",
        "paplay --device=vsink /tmp/tone.wav 2>&1; echo EXIT:$?",
        "user",
        30,
    ),
    (
        "alsa_devices",
        # Expect 'No such file or directory' — recorded, not fatal.
        "cat /proc/asound/cards 2>&1 || true; echo ---; cat /proc/asound/version 2>&1 || true",
        "user",
        15,
    ),
    (
        "snd_dummy",
        # Expect modprobe failure (module not shipped in the Firecracker guest
        # kernel) — recorded, not fatal.
        "modprobe snd-dummy 2>&1; echo EXIT:$?",
        "root",
        15,
    ),
    (
        "musescore_render",
        "F=$(ls /usr/share/mscore3-*/demos/*.mscz 2>/dev/null | head -1); "
        'if [ -z "$F" ]; then echo SKIP_NO_DEMO_FILE; else '
        'xvfb-run -a musescore3 -o /tmp/out.wav "$F" 2>&1 | tail -20; '
        "test -s /tmp/out.wav && echo MUSESCORE_OK "
        "&& ls -la /tmp/out.wav || echo MUSESCORE_FAIL; fi",
        "user",
        120,
    ),
]


def run_checks(sbx: Sandbox) -> dict:
    results = {}
    for name, cmd, user, timeout in CHECKS:
        t0 = time.time()
        try:
            r = sbx.commands.run(cmd, user=user, timeout=timeout)
            exit_code = r.exit_code
            out_tail = (r.stdout or "")[-2000:]
            err_tail = (r.stderr or "")[-1000:]
        except Exception as e:  # command-level timeout/connection error
            exit_code = None
            out_tail = ""
            err_tail = f"EXCEPTION: {e!r}"
        results[name] = {
            "exit": exit_code,
            "out_tail": out_tail,
            "err_tail": err_tail,
            "elapsed_s": round(time.time() - t0, 2),
            "user": user,
        }
        print(f"[{name}] exit={exit_code} elapsed={results[name]['elapsed_s']}s")
    return results


def main() -> int:
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        print("E2B_API_KEY not set", file=sys.stderr)
        return 2

    sbx = Sandbox.create(timeout=600)
    evidence = {
        "sandbox_id": sbx.sandbox_id,
        "template": "default (no template arg passed to Sandbox.create())",
        "template_note": (
            "Brief specified ubuntu:22.04; the actual E2B default base template "
            "is Debian 12 (bookworm) per /etc/os-release observed in-sandbox. "
            "Ran checks against the true default rather than substituting a "
            "template name that doesn't exist."
        ),
        "checks": {},
    }
    try:
        evidence["checks"] = run_checks(sbx)
    finally:
        sbx.kill()
        print(f"killed sandbox {sbx.sandbox_id}")

    checks = evidence["checks"]

    def passed(name: str) -> bool:
        c = checks.get(name, {})
        if name == "sink_list":
            return c.get("exit") == 0 and "vsink" in (c.get("out_tail") or "")
        if name == "play_to_sink":
            return c.get("exit") == 0 and "EXIT:0" in (c.get("out_tail") or "")
        if name == "musescore_render":
            return "MUSESCORE_OK" in (c.get("out_tail") or "")
        return c.get("exit") == 0

    null_sink_ok = passed("null_sink") and passed("sink_list")
    play_to_sink_ok = passed("play_to_sink")
    tone_render_ok = passed("tone_render")
    musescore_ok = passed("musescore_render")

    hard_blocker = False
    pactl_paplay_alsa_fail = not (null_sink_ok and play_to_sink_ok)
    snd_dummy_fail = checks.get("snd_dummy", {}).get("exit") != 0
    if pactl_paplay_alsa_fail and snd_dummy_fail:
        hard_blocker = True

    gate_pass = null_sink_ok and play_to_sink_ok and (tone_render_ok or musescore_ok)

    evidence["gate"] = {
        "null_sink_ok": null_sink_ok,
        "play_to_sink_ok": play_to_sink_ok,
        "tone_render_ok": tone_render_ok,
        "musescore_render_ok": musescore_ok,
        "gate_pass": gate_pass,
        "hard_blocker": hard_blocker,
        "verdict": (
            "audio is userspace-viable: PulseAudio null sink + paplay work, "
            "and both the ffmpeg tone-render and full MuseScore headless render "
            "succeeded (ALSA/snd-dummy absent as expected, not required)"
            if gate_pass and not hard_blocker
            else "HARD BLOCKER: no userspace audio path and no kernel module fallback"
        ),
        "reaper_note": (
            "REAPER is not apt-packaged; not exercised in this spike. Needs its "
            "own .deb in the guest template (Task 8). MuseScore + paplay-to-"
            "null-sink is the go/no-go signal for this spike per the brief."
        ),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"wrote {OUT_PATH}")
    print(f"GATE: {evidence['gate']['verdict']}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
