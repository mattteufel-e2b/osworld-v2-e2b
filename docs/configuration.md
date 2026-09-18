# Agent and run configuration

Run these commands in Bash from the repository root after completing the
[Quick start](../README.md#quick-start). Set `RUN_ROOT` to your run directory and render
`$RUN_ROOT/full-manifest.json` before using the examples below.

## Tasks, limits, and results

Add repeated `--task-id` arguments to `runner/render_manifest.py` to select tasks.
`PARALLEL_CONCURRENCY` defaults to and is capped at 80; optional retry concurrency defaults
to and is capped at four. The per-task deadline defaults to four hours. Use
`AGENT_TASK_TIMEOUT_SECONDS=28800` for full 500-turn runs, which can exceed four hours.

The campaign receipt is written to the launch command's `OUTPUT` path. The
`AGENT RECEIPT GATE: PASS`/`FAIL` line reports whether every task has a complete, attested,
uniquely-sandboxed record. A passing receipt does not mean the agent solved every task.
`REQUIRE_NO_MODEL_COVERAGE=0` is the default; a preceding no-model run is optional and
belongs to the [maintainer validation workflow](../maintainer/README.md).

## Judge and user simulator configuration

Agent credentials are separate from the upstream judge and simulator credentials. The default
uses OpenAI with `OPENAI_API_KEY`; select another endpoint using upstream's settings.

### Recommended: Haiku 4.5 on Bedrock

This AWS Mantle configuration passed the
[native spreadsheet and visual-judge controls](../out/osworld-v2-evidence/sample-36/judge-controls-20260913.json)
used for this repo's validation: Claude Haiku 4.5 returns a compliant verdict within the
small output-token budgets some task judges enforce. Set `AWS_MANTLE` to your Mantle API key:

```bash
export OSWORLD_EVAL_MODEL_PROVIDER=anthropic
export OSWORLD_EVAL_MODEL_NAME=anthropic.claude-haiku-4-5
export OSWORLD_EVAL_MODEL_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/anthropic
export OSWORLD_EVAL_MODEL_API_KEY_ENV=AWS_MANTLE
export OSWORLD_USER_SIM_MODEL=anthropic.claude-haiku-4-5
```

OpenAI remains available:

```bash
export OSWORLD_EVAL_MODEL_PROVIDER=openai
export OSWORLD_EVAL_MODEL_NAME=gpt-4o   # key via OPENAI_API_KEY
```

| Setting | Judge | User simulator |
| --- | --- | --- |
| Provider | `OSWORLD_EVAL_MODEL_PROVIDER` | `OSWORLD_USER_SIM_PROVIDER` |
| Model | `OSWORLD_EVAL_MODEL_NAME` | `OSWORLD_USER_SIM_MODEL` |
| Endpoint | `OSWORLD_EVAL_MODEL_BASE_URL` | `OSWORLD_USER_SIM_BASE_URL` |
| API key | `OSWORLD_EVAL_MODEL_API_KEY` | `OSWORLD_USER_SIM_API_KEY` |
| Key environment variable | `OSWORLD_EVAL_MODEL_API_KEY_ENV` | `OSWORLD_USER_SIM_API_KEY_ENV` |
| Output token limit | `OSWORLD_EVAL_MODEL_MAX_OUTPUT_TOKENS` | `OSWORLD_USER_SIM_MAX_TOKENS` |

Set the simulator model explicitly when changing providers. The release's LLM simulator tasks
specify `gpt-4o`; that task setting takes precedence over the judge model. The simulator inherits
judge provider, endpoint, and credentials only where neither its own environment nor task config
overrides them. `OSWORLD_USER_SIM_MODEL_NAME` and `OSWORLD_USER_SIM_MODEL_BASE_URL` are invalid.
Literal API keys take precedence over key-variable names; use `OSWORLD_USER_SIM_API_KEY`
when overriding a literal judge key.

Some task judges allow only 5–16 output tokens. Use a model that can return a verdict at that
budget; reasoning can consume it before a verdict appears. Task-specific token limits take
precedence over the judge environment setting. The agent's thinking budget is independent of
these judge settings. Fireworks M3 returned false positives on four of five blank-slide judge
controls even with reasoning disabled; its successful agent image calls do not validate it as
a judge.

Each per-task receipt records `model_usage` per role (`agent`, `judge`, `simulator`): each role
carries `calls`, `input_tokens`, `output_tokens`, and `unmeasured_calls`. The campaign receipt
sums those per-task roles under `summary.model_usage`.

Agent usage is measured for the `m3` kind, whose SDK client the tracker wraps. The `prompt` kind
sends requests with `requests` and reports `agent.calls = 0` with `input_tokens`/`output_tokens`
`null`; judge and simulator usage is measured regardless of agent kind.

Before creating rollout guests, the coordinator checks a text answer, reads random digits from
an image, and checks the selected tasks' LLM simulator configurations through upstream's own
clients. The text-answer check sends upstream's own binary system prompt and accepts any verdict
upstream's own parser accepts, where the first alphabetic token of the reply decides YES. The
image check compares the model's reply against the digits rendered, after stripping every
non-digit character from that reply. You can run these checks before starting the service fleets
as well:

```bash
(cd OSWorld-V2 && uv run --locked --extra full python ../runner/check_models.py \
    --manifest "$RUN_ROOT/full-manifest.json" --tasks-dir ../tasks)
```

These probes establish basic text/image behavior and nonempty simulator responses, not
benchmark scoring parity.
Changing judge or simulator models changes the experiment configuration. A failed or empty judge
or simulator call invalidates the run even if upstream returns zero; simulator calls cannot satisfy
judge coverage. Task rollouts are not automatically retried.

Preflight verifies the actual upstream commit and exact adapter patches; on drift it prints the
`runner/setup.sh --restore` + re-apply command that repairs the checkout. The coordinator admits
a run only if both fleets outlast the worst-case budget of its waves (every wave charged its full
`AGENT_TASK_TIMEOUT_SECONDS`), and re-checks before each retry wave against the tasks that
actually failed, skipping that wave when it no longer fits. Fleets are not renewed mid-run.
The coordinator writes one `{attempt, task_ids}` entry per retry wave, and the campaign receipt
republishes that ledger verbatim as `execution.retry_waves`. Point `RAW_DIR` at a fresh directory
for every campaign: a previous run's `result.txt` under the same directory counts as a scored
attempt and suppresses the retry of a task this run never scored.

`run_agent_parallel.sh` stops both service fleets when an admitted run exits. A run rejected
before admission (preflight, lifetime) leaves them running so the rejection can be acted on with
the same fleets; stop them yourself with
`uv run --env-file .env.local --locked python services/stop.py --campaign-id "$OSWORLD_CAMPAIGN_ID"`.
Set `TEARDOWN_FLEETS_ON_EXIT=0` to retain the fleets after an admitted run as well.
Run-scoped raw trajectories, service receipts, and secrets stay in ignored paths; committed
evidence is published only after allowlist sanitization.

## Bring your own agent

The workflow is upstream's: write an agent, point the runner at it, pick tasks, run, read the
OSWorld outputs. `runner/agents.py` is the one file to edit. It constructs the agent for
`AGENT_KIND` and holds each kind's upstream generation defaults; `agent_runner.py` and the
receipts never look inside the agent.

- `AGENT_KIND=prompt` is upstream's `PromptAgent` routed at any OpenAI-compatible
  chat-completions endpoint (`MODEL_BASE_URL`, `MODEL_API_KEY`, `MODEL` passed verbatim).
  `AGENT_KIND=m3` is upstream's MiniMax-M3 agent over its Anthropic Messages transport.
- To run your own, implement upstream's `reset()` / `predict(instruction, observation)`
  interface (see `OSWorld-V2/mm_agents/` for reference), put the class under `runner/` next to
  `agents.py` rather than inside the checkout (`setup.sh --restore` resets tracked files there),
  add a builder to `AGENT_KINDS`, and launch with `AGENT_KIND=<your name>`. Prompts, model calls, memory and context policy live
  in your class, as in upstream's `run_multienv_*.py` runners.
- Generation settings mirror upstream `run.py` flags and are optional environment variables on
  the same launch command: `MAX_TOKENS`, `TEMPERATURE`, `TOP_P`, `MAX_TRAJECTORY_LENGTH`. Unset
  means the agent kind's upstream default; the resolved values are recorded in every receipt
  as `agent_settings`. Observation is `screenshot` and actions are `pyautogui`, which is what
  the E2B guest exposes today.
- `ENABLE_RECORDING=1` is upstream's `--enable_recording`: the guest records the screen for the
  whole rollout and `recording.mp4` lands in the task's result directory. Off by default;
  receipts record `recording_enabled`. Live desktop view (VNC) is tracked separately in
  [issue #2](https://github.com/mattteufel-e2b/osworld-v2-e2b/issues/2).

Each admitted run stops its service fleets by default. Before this example, create a fresh
campaign and launch both fleets using the [Quick start commands](../README.md#quick-start). Set all three provider
values for your chosen OpenAI-compatible endpoint; the placeholders below are not a live
provider configuration.

```bash
export MODEL_BASE_URL="https://your-provider.example/v1"
export MODEL_API_KEY="..."
export MODEL="your-model-id"
export AGENT_KIND=prompt TEMPERATURE=0.2 MAX_TRAJECTORY_LENGTH=5
AGENT_MANIFEST="$RUN_ROOT/full-manifest.json" MAX_STEPS=75 \
    RAW_DIR="$RUN_ROOT/agent-raw" OUTPUT="$RUN_ROOT/agent.json" runner/run_agent_parallel.sh
```
