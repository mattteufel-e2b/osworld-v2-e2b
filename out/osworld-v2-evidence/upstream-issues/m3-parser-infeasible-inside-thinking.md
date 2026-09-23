# M3 parser treats `[INFEASIBLE]` inside `<mm:think>` as a terminal verdict, discarding the tool call in the same response

**Repository:** xlang-ai/OSWorld-V2
**Pinned commit tested:** `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`
**File:** `mm_agents/m3/parser.py` (the `if "[INFEASIBLE]" in response:` check)
**Status:** draft, not filed.

## Summary

`M3Agent._call_llm` prepends the model's reasoning to the response as
`<mm:think>…</mm:think>`. The parser then scans the **whole** string for `[INFEASIBLE]` and,
on a match, returns `("[INFEASIBLE]", ["FAIL"])` before it ever looks for a tool call. A model
that merely considers infeasibility while reasoning — and then emits a perfectly ordinary
action — terminates the rollout with `FAIL`.

This is a parsing artifact of the thinking channel, not a model verdict: the marker is
specified as a terminal answer, and reasoning text is not an answer.

## Minimal repro

```python
response = (
    "<mm:think>maybe this is [INFEASIBLE]? no — try Ctrl+C first</mm:think>\n"
    '<tool_call>{"action": "key", "text": "ctrl+c"}</tool_call>'
)
# actual:   ("[INFEASIBLE]", ["FAIL"])  -- the tool call is never parsed
# expected: the ctrl+c action
```

## Impact

Rollouts end early with a scored `FAIL` while the agent was still working. In a 2026-09-15
five-task pilot this ended one task outright; a larger 36-task campaign saw the same
termination on three tasks. Because the marker is terminal, there is no recovery step.

## Suggested fix

Strip the thinking block before the terminal-marker scan, e.g.

```python
_THINK = re.compile(r"<mm:think>.*?</mm:think>", re.S)
if "[INFEASIBLE]" in _THINK.sub("", response):
    return "[INFEASIBLE]", ["FAIL"]
```

so the marker counts only where the protocol places it: in the answer, not the reasoning.
An alternative that also works is to parse tool calls first and treat the marker as terminal
only when no tool call is present.

## Evidence

Sanitized run summary: `out/osworld-v2-evidence/sample-5/agent-pilot-round2-20260915.summary.json`
(task 067: the final response contained `[INFEASIBLE]` inside its reasoning followed by an
actual Ctrl+C tool call and a stated intention to continue; the parser returned `FAIL`).
Earlier occurrences: `out/osworld-v2-evidence/sample-36/audit-main36-20260914.json`.
Task and asset text are not reproduced here: the dataset is gated.
