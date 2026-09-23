# `execute_python_command`'s 90 s client timeout expires before the guest's own 120 s deadline, so a killed action is invisible and its input overlaps the next one

**Repository:** xlang-ai/OSWorld-V2 (client) with `xlang-ai/osworld-server` (guest)
**Pinned commit tested:** `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`
**Files:** `desktop_env/controllers/python.py` (`PythonController.execute_python_command`),
guest `osworld-server` `/execute` (`data.get("timeout", 120)`)
**Status:** draft, not filed.

## Summary

The two ends of one request disagree about who owns the deadline:

* the client posts to `/execute` with `timeout=90`;
* the guest runs the action under `subprocess` with a 120 s timeout.

Between 90 s and 120 s the client has already raised `ReadTimeout`, broken out of its retry
loop and returned `None`, while the guest is still typing. The rollout proceeds to the next
action, whose keystrokes interleave with the first action's remaining input. `None` is also
what a legitimate failure returns, so the trajectory records no distinguishable event.

This is not hypothetical for long typing actions: upstream's M3 parser emits one
`pyautogui.press()` per character, and PyAutoGUI's default 0.1 s pause makes a 1,000-character
action take ~100 s — already past 90 s, still inside 120 s.

## Minimal repro

1. Start any guest running the pinned `osworld-server`.
2. Open a text editor in the guest.
3. Issue **one** action through `PythonController.execute_python_command` consisting of 3,000
   `pyautogui.press()` calls (about 300 s of guest-side pauses).
4. Observe: the call returns `None` at ~90 s. Screenshot the text area at return + 5 s and
   again 15 s later — the two differ, because the guest is still typing.
5. At ~120 s the guest kills the action and answers `500` with subprocess's
   `Command '…' timed out after 120 seconds` text, which no client is listening for.

## Impact

* Silently misexecuted actions: partial input, then overlapping input from the next action.
* Indistinguishable from a real failure, so campaigns cannot separate "the guest refused" from
  "the guest was still working".
* Because a non-200 is retried up to `retry_times`, a client that *did* wait long enough would
  replay a partially applied action unless the timeout response is special-cased.

In a 2026-09-15 five-task pilot, four tasks showed oversized typing actions returning control
at ~90–96 s with input still arriving afterwards; an earlier 36-task campaign logged 350
native command-failure messages across 25 tasks alongside 343 actions of more than 900
keypress calls.

## Suggested fix

Make the client's deadline strictly longer than the guest's (the guest owns the kill), and
treat the guest's timeout answer as final rather than retryable:

* raise the `/execute` request timeout above the guest deadline (we use 130 s for the guest's
  120 s, leaving headroom for the response to travel back);
* on a `500` whose body carries the `timed out after` text, stop retrying and return the same
  value a client-side `ReadTimeout` returns, so no partially applied action is replayed and
  no caller sees a success-shaped result.

Returning a `200` with empty output instead would be worse than the current behaviour:
several evaluator getters read `execute_python_command(...)["output"]` directly, so an
infrastructure failure would become a silent zero score.

## Evidence

Isolated control: `out/osworld-v2-evidence/sample-36/runtime-controls-20260914.json` — a
1,000-character action returned `None` at 90.12 s while typing continued; the completion
marker appeared at 102.61 s. Campaign observations:
`out/osworld-v2-evidence/sample-5/agent-pilot-round2-20260915.summary.json` (tasks 093, 059,
079, 082). Task and asset text are not reproduced here: the dataset is gated.
