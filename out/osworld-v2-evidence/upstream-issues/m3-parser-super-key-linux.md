# M3 parser maps `super` to `command`, so the Super key is silently dropped on Linux

**Repository:** xlang-ai/OSWorld-V2
**Pinned commit tested:** `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`
**File:** `mm_agents/m3/parser.py` (the `key_conversion` table inside the active `key` action branch)
**Status:** draft, not filed.

## Summary

The active `key` branch of the M3 parser rewrites `super` to `command`. PyAutoGUI's X11
backend has no `command` key: `pyautogui.press("command")` returns without error and without
sending anything, so an agent that asks for the Super key gets no keystroke and no failure
signal. The module-level `_NORMALIZE_KEY` table in the same file already maps
`"super": "win"`, but nothing references it, so the correct mapping is dead code. The
neighbouring entry `"super_l": "win"` shows `win` is the intended X11 spelling.

## Minimal repro

On a Linux (X11) guest running the pinned `osworld-server`:

```text
# what the model emits to open the GNOME shell overview
{"action": "key", "text": "super"}
# what mm_agents/m3/parser.py generates for it
pyautogui.press('command')
```

Then, in the guest:

```text
>>> import pyautogui
>>> pyautogui.press("command")   # no key event is delivered; no exception
>>> pyautogui.press("win")       # opens the GNOME Activities overview
```

## Impact

Any task whose solution needs the Super key (application overview, workspace switching) loses
that action with no error recorded anywhere in the trajectory. The step still counts against
`MAX_STEPS`.

## Suggested fix

In the active `key` branch's `key_conversion`, change `"super": "command"` to `"super": "win"`,
matching the adjacent `"super_l": "win"` and the module's own `_NORMALIZE_KEY`. Hosts that
actually need macOS semantics should select by `os_type`, not by a hardcoded table entry.

## Evidence

Sanitized run summaries: `out/osworld-v2-evidence/sample-5/agent-pilot-round2-20260915.summary.json`
(task 103; the model requested
`super`, the emitted call was `pyautogui.press('command')`, and no keystroke reached the
desktop). Task and asset text are not reproduced here: the dataset is gated.
