# Agent and run configuration

Run these commands in Bash from the repository root after completing the
[Quick start](../README.md#quick-start). Set `RUN_ROOT` to your run directory and render
`$RUN_ROOT/full-manifest.json` before using the examples below.

## Tasks, limits, and results

Add repeated `--task-id` arguments to `runner/render_manifest.py` to select tasks.
`PARALLEL_CONCURRENCY` defaults to 80 and is capped at 120; optional retry concurrency
defaults to and is capped at four. The per-task deadline now defaults to 28800 seconds
(8 hours), budgeting a full 500-step rollout with headroom; set `AGENT_TASK_TIMEOUT_SECONDS`
to something shorter for a canary. See [Coordinator knobs](#coordinator-knobs) for every
environment variable the launch command reads.

The campaign receipt is written to the launch command's `OUTPUT` path. The
`AGENT RECEIPT GATE: PASS`/`FAIL` line reports whether every task has a complete, attested,
uniquely-sandboxed record. A passing receipt does not mean the agent solved every task.
`REQUIRE_NO_MODEL_COVERAGE=0` is the default; a preceding no-model run is optional and
belongs to the [maintainer validation workflow](../maintainer/README.md).

Token receipts retain each provider's input/output semantics. Anthropic cache-write and
cache-read tokens are reported separately and must be included in cost estimates;
`input_tokens` alone excludes them. Missing cache telemetry is `null`, including in
aggregates that contain older receipts without these counters.

## Coordinator knobs

Every environment variable `runner/run_agent_parallel.sh` reads, its default, and what it
does. A default of "none (required)" means the launch command exits 2 if the variable is
unset. `POOL_POLL_SECONDS`, `PROXY_WATCHDOG_SECONDS`, `FLEET_LIVENESS_SECONDS`, and
`ATTEMPT_SUFFIX` are internal to the coordinator and are not operator knobs.

### Required

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL_API_KEY` | none (required) | API key for the agent model endpoint. |
| `MODEL_BASE_URL` | none (required) | Base URL for the agent model endpoint. |
| `MODEL` | none (required) | Agent model id, forwarded as `--model`. |
| `AGENT_KIND` | none (required) | `prompt`, `m3`, or `claude`; forwarded as `--agent-kind`. |
| `GUEST_TEMPLATE` | none (required) | Immutable `name:build_id` guest template reference (`runner/common.sh`). |
| `OSWORLD_CAMPAIGN_ID` | none (required) | Campaign id tagging every sandbox this run creates (`runner/common.sh`). |
| `E2B_API_KEY` | none (required) | E2B account key; read from `.env.local` when unset (`runner/common.sh`). |

### Run identity and paths

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_MANIFEST` | `validation/full-manifest.json` | Task manifest the campaign selects tasks from. |
| `RUN_ID` | `<UTC timestamp>-$$` | Identifies this run; seeds the `RAW_DIR`/`OUTPUT` defaults below. |
| `RESUME_RUN_ID` | unset | A previous `RUN_ID` to resume. Kept tasks (an existing `result.txt`) are skipped and their receipts count; unscored tasks rerun; the previous run's nonce is recovered. A task with a `result.txt` but no receipt is never rerun automatically. |
| `RAW_DIR` | `out/osworld-v2-raw/agent-full/$RUN_ID` | Per-worker trajectories, receipts, and coordinator logs. |
| `OUTPUT` | `out/osworld-v2-evidence/full-suite/agent-$RUN_ID.json` | Campaign receipt path. |

### Scheduling

| Variable | Default | Meaning |
| --- | --- | --- |
| `PARALLEL_CONCURRENCY` | 80 (cap 120) | Rolling-pool worker slots; a new task starts as soon as one frees, instead of waiting on a fixed batch. |
| `RUN_TASK_082_CONCURRENT` | 1 | Set to 0 to run task 082 solo, reserving the literal host port 3000. |
| `AGENT_START_STAGGER_SECONDS` | 0.25 | Delay between launching consecutive workers in a pool. |
| `TEARDOWN_FLEETS_ON_EXIT` | 1 | Set to 0 to keep the service fleets running after an admitted run exits. |

### Deadlines and retries

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_TASK_TIMEOUT_SECONDS` | 28800 | Per-task wall-clock deadline, forwarded as `--deadline-seconds`. |
| `GUEST_READY_TIMEOUT_S` | 180 | Seconds to wait for a guest to become ready. |
| `AGENT_RETRY_ATTEMPTS` | 0 | Retry waves for infrastructure/path failures; completed low-scoring tasks are never resampled. |
| `AGENT_RETRY_CONCURRENCY` | 4 (cap 4) | Worker slots during a retry wave. |

### Agent generation

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_STEPS` | 500 | Rollout step budget, forwarded as `--max-steps`. |
| `SLEEP_AFTER_EXECUTION` | unset (upstream default 3.0s); auto 0 when `AGENT_KIND=claude` and unset | Pause after each native action, forwarded as `--sleep-after-execution` only when set. |
| `MAX_TOKENS` | unset (upstream default) | Forwarded as `--max-tokens` only when set. |
| `TEMPERATURE` | unset (upstream default) | Forwarded as `--temperature` only when set. |
| `TOP_P` | unset (upstream default) | Forwarded as `--top-p` only when set. |
| `MAX_TRAJECTORY_LENGTH` | unset (upstream default) | Forwarded as `--max-trajectory-length` only when set. |
| `ENABLE_RECORDING` | 0 | Set to 1 to record `recording.mp4` per task, forwarded as `--enable-recording`. |
| `M3_THINKING_MODE` | unset | Optional M3 thinking mode, included in the campaign receipt when set. |
| `M3_THINKING_BUDGET` | none (required when `AGENT_KIND=m3`) | Positive integer thinking-token budget for M3. |
| `M3_MAX_LLM_RETRIES` | none (required when `AGENT_KIND=m3`) | Non-negative retry count for M3's own LLM calls. |

### Judge retries

| Variable | Default | Meaning |
| --- | --- | --- |
| `OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS` | 8 | Judge/simulator retry attempts on a failed call, exported when unset. |
| `OSWORLD_EVAL_MODEL_RETRY_DELAY` | 10 | Seconds between judge/simulator retries, exported when unset. |

### Maintainer-only

| Variable | Default | Meaning |
| --- | --- | --- |
| `REQUIRE_NO_MODEL_COVERAGE` | 0 | Gate the run on a prior no-model coverage receipt; see [maintainer validation](../maintainer/README.md). |
| `NO_MODEL_RECEIPT` | none (required when `REQUIRE_NO_MODEL_COVERAGE=1`) | Path to that no-model receipt. |

## Comparing with upstream Claude

The public [pinned sample launcher](https://github.com/xlang-ai/OSWorld-V2/blob/d578d2d4e0dc82b43e270fdaa7fa89d9708cd154/scripts/bash/run_multienv_claude.sh)
uses Sonnet 4.6 for agent, judge, and simulator. The tested Bedrock key lists Opus 5 but not
Sonnet 4.6. `AGENT_KIND=claude MODEL=anthropic.claude-opus-5` uses the pinned native Claude
agent and a model with an official result on our **August 8** task release: 31.43% binary,
68.31% partial, max effort, batch tool, 500 steps. The [official result data](https://osworld-v2.xlang.ai/static/data/leaderboard/official-results.json?v=leaderboard-v21-v1)
also lists newer 2.1 scores; those use a different task release.

The published aggregate is a comparison target, not proof of environment parity. Its exact
batch configuration, judge/simulator settings, action pauses, and checkpoint settings must
match before attributing a score difference to E2B. Our generic runner defaults to a
three-second action pause; set `SLEEP_AFTER_EXECUTION=0` to match the pinned Claude launcher.
That launcher also enables inline checkpoints at 150/300. The coordinator does not expose those checkpoints. Use matched
per-task reference trajectories or rerun the same agent configuration on the reference VM
for a controlled environment comparison. Haiku judging and short samples do not reproduce
the published baseline.

Native Claude `computer` waits use the worker clock on E2B, preserving the requested
duration before the next observation. This avoids the guest command's 120-second deadline
without changing the agent's prompts, action history, or ordinary command limits.
Native Unicode typing also isolates the clipboard helper's output streams so its background
process cannot keep a completed command's response open. Ordinary command output remains
captured. The `claude` adapter applies both repairs to batch members in order, retaining
the original batch in the trajectory and one observation and action pause per batch.
These execution repairs address behavior also present in the pinned upstream
runtime and should be applied consistently to a matched reference run. The upstream
Ctrl+V paste shortcut is preserved; GNOME Terminal normally requires Ctrl+Shift+V.

## Judge and user simulator configuration

Agent credentials are separate from the upstream judge and simulator credentials. The default
uses OpenAI with `OPENAI_API_KEY`; select another endpoint using upstream's settings.
The Claude adapter scopes its Anthropic endpoint and bearer token to each prediction,
restoring the environment before any judge or simulator call, including on interruption.

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

Task 092 has a legacy `OPENAI_API_KEY` presence check before its provider-neutral video
judge. With Bedrock judging, keep that check nonempty using a placeholder; the explicit
judge provider, endpoint, and key above still select Bedrock:

```bash
export OPENAI_API_KEY="${OPENAI_API_KEY:-bedrock-legacy-presence-only}"
```

Without this variable, upstream silently skips the video judge, worth 0.20 of the score.
Live-run preflight rejects this configuration when task 092 is selected.

Some task judges allow only 5–16 output tokens. Use a model that can return a verdict at that
budget; reasoning can consume it before a verdict appears. Task-specific token limits take
precedence over the judge environment setting. The agent's thinking budget is independent of
these judge settings. Fireworks M3 returned false positives on four of five blank-slide judge
controls even with reasoning disabled; its successful agent image calls do not validate it as
a judge.

Each per-task receipt records `model_usage` per role (`agent`, `judge`, `simulator`): each role
carries `calls`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, and `unmeasured_calls`. The campaign receipt sums those per-task
roles under `summary.model_usage`, preserving unknown cache telemetry as `null`.

Agent usage is measured for the `m3` and `claude` kinds through the Anthropic standard and
beta Messages SDK clients, including separate cache-read and cache-write counts. The `prompt` kind
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

Alongside `workers/`, `RAW_DIR` holds three coordinator-owned files: `hostmap-proxy.log` (the
host proxy's stdout/stderr, appended across resumes, never truncated), `hostmap-proxy.pid`
(the live proxy pid, rewritten whenever the watchdog restarts it), and `fleet-liveness.log`
(one line per liveness probe of the website and GitLab fleets). A background watchdog
restarts the host proxy if it dies and probes fleet liveness on its own schedule; after three
consecutive failed probes of the same fleet it prints one `FLEET LIVENESS: <fleet> unreachable
for 3 probes; see fleet-liveness.log` warning to stderr (not repeated every probe) and keeps
running. A liveness warning does not abort the run; the workers' own receipts still decide
whether the campaign gate passes.

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
