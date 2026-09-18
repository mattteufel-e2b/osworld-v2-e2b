# Maintainer validation ladder

Maintainer-only: not required to run the benchmark (that path lives entirely under
`runner/`). These scripts qualify a guest template build before it ships: they exercise every
environment path of all 108 tasks with **no model calls** (`OSWORLD_EVAL_MODEL_MODE=stub`),
fail closed at any evaluator model boundary, and produce the no-model receipt that the
optional `REQUIRE_NO_MODEL_COVERAGE=1` gate in `runner/run_agent_parallel.sh` consumes.

| Script | Purpose |
|---|---|
| `validate_parallel.sh` | All manifest tasks, one worker each (default and cap of 80 concurrent), aggregate gate |
| `run_path_task.sh` | One no-model task in its own process (worker for the above) |
| `validate.sh` | Sequential variant, `VALIDATION_RUNS` passes over a manifest |
| `harness.py` | The no-agent rollout: reset, observe, fail-closed evaluate, receipt |
| `no_model.py`, `readiness.py` | Evaluator stubs and bounded observation retries used by the harness |
| `profile_resources.sh` | Ladder rung 7: publish measured CPU/RAM/disk from evidence |
| `receipt_summary.py` | Reduce a no-model aggregate receipt to the committable summary (also summarizes an agent campaign receipt from `runner/aggregate_agent.py`) |
| `app_smoke.py` | Open each parity application on a fresh guest of one build on its pinned task input, and record windows plus a screenshot per application (`GUEST_TEMPLATE=...`); the screenshots are the evidence and a human reads them |
| `typing_control.py` | Isolated long-typing control for the disclosed controller patches (g)+(h): one 3,000-keypress action against one live guest, run by hand against a candidate build |
| `browser_probe.py` | Guest-browser secure-context probe over the campaign's HTTPS fleet origins: drives one guest's own Chrome over CDP across TeamChat, CloudCRM, MailHub, StreamView, `studio.streamview` and the task-041 GitLab alias, and records `isSecureContext`, `navigator.clipboard`, notification grants, the clipboard round-trip, cookie isolation, every mixed-content block, and the full per-origin request list that proves every subresource was fetched over HTTPS (needs a live fleet campaign; `GUEST_TEMPLATE=...` plus `export_fleet_wiring`) |

Each harness process owns its E2B guest through the provider's in-process bridge; there is
no separate relay to start or clean up. Shared path and environment gates live in
`runner/common.sh`.

Run the commands below in Bash from the repository root, after building the templates,
downloading the gated data, and starting a fleet campaign as described in the
[Quick start](../README.md#quick-start). Export `FIREWORKS_API_KEY` for the M3 examples.

## Release validation (maintainers)

Everything in this section lives under `maintainer/` and is **not required to run the
benchmark**. `runner/` holds only the benchmark path; `tools/spikes/` keeps the one-off
probes that shaped the port (their evidence is cited from `FIDELITY.md`).

This exercises the harness paths across all 108 tasks with zero external model calls,
then optionally gates a full-model benchmark receipt on that coverage. A passing path does
not establish application compatibility or task success. It's how maintainers
qualify a release before it ships — it is **not** required to run the benchmark (see the [Quick start](../README.md#quick-start)). When the gate is enabled, the resulting campaign receipt records
`execution.no_model_coverage_enforced: true`, so gated and ungated runs stay distinguishable
from the receipt alone.

```bash
RUN_ROOT="$(mktemp -d)"
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/full-manifest.json"
VALIDATION_MANIFEST="$RUN_ROOT/full-manifest.json" VALIDATION_RUNS=1 \
    PARALLEL_CONCURRENCY=24 RAW_DIR="$RUN_ROOT/no-model-raw" \
    EVIDENCE_DIR="$RUN_ROOT/no-model-evidence" OUTPUT="$RUN_ROOT/no-model.json" \
    maintainer/validate_parallel.sh            # all 108 tasks, zero external model calls
```

Commit the summary, not the receipt: `python3 maintainer/receipt_summary.py --input "$RUN_ROOT/no-model.json" --output out/osworld-v2-evidence/full-suite/<name>.summary.json`, and keep the full receipt under the gitignored `out/osworld-v2-raw/`.

The no-model receipt distinguishes an evaluator path that returned normally (`PATH_PASS`) from one
that propagated the intentional disabled-model sentinel (`MODEL_BOUNDARY_PASS`). Both are validated
no-model outcomes. The full-model sample must exercise every task that either propagated that
sentinel or attempted an evaluator-model call that an upstream metric converted into a zero score.

The validation coordinators leave the fleets running so later rungs can reuse the campaign;
stop it with `uv run --env-file .env.local --locked python services/stop.py --campaign-id "$OSWORLD_CAMPAIGN_ID"` when you are done with it.

Before a large inference campaign, run `browser_probe.py` against the candidate guest and
live fleet with the host proxy running (see its usage header). It checks the task origins
on port 8090, a 2 MiB StreamView upload and download, and a chunked Git push from the guest.
The upload checks transport and persistence, not video playback. The probe removes its
temporary website state and GitLab project.

Also inspect a small full-agent run's receipts for successful judge and simulator calls
through the task loop. `check_models.py` verifies client connectivity; zero calls in an
agent receipt do not establish coverage. M3's prompt omits `call_user`, so simulator
coverage needs an explicit task-loop canary; a normal M3 rollout may never exercise it.

To gate a full-model run on that coverage instead of running it ungated, set
`REQUIRE_NO_MODEL_COVERAGE=1` and point `NO_MODEL_RECEIPT` at the receipt above — this is the
same `run_agent_parallel.sh` invocation as the [Quick start](../README.md#quick-start), plus those two variables:

```bash
AGENT_MANIFEST="$RUN_ROOT/full-manifest.json" REQUIRE_NO_MODEL_COVERAGE=1 \
    NO_MODEL_RECEIPT="$RUN_ROOT/no-model.json" PARALLEL_CONCURRENCY=80 MAX_STEPS=500 \
    AGENT_TASK_TIMEOUT_SECONDS=28800 RAW_DIR="$RUN_ROOT/agent-raw" \
    OUTPUT="$RUN_ROOT/agent-full.json" runner/run_agent_parallel.sh
```

`REQUIRE_NO_MODEL_COVERAGE=1` fails closed with `NO_MODEL_RECEIPT is required for full-agent
coverage` unless `NO_MODEL_RECEIPT` is also set, and then runs `model_coverage.py` against it
before any agent launches.

After the all-task no-model run passes, use one fresh fleet campaign for a three-step canary and
another for the 24-task representative sample. The sample covers selected model-based evaluator
paths, multiphase tasks, task 082's local service, and a spread of task complexity. The runner does
not retry completed model rollouts.

```bash
export MODEL_API_KEY="$FIREWORKS_API_KEY"
export MODEL_BASE_URL="https://api.fireworks.ai/inference"
export MODEL="accounts/fireworks/models/minimax-m3"
export AGENT_KIND=m3 M3_THINKING_BUDGET=2048 M3_MAX_LLM_RETRIES=0
export OPENAI_API_KEY="..."                # upstream judge / simulator credentials
export NO_MODEL_RECEIPT="$RUN_ROOT/no-model.json"

export OSWORLD_CAMPAIGN_ID="osworld-v2-canary-$(date -u +%Y%m%dT%H%M%SZ)"
uv run --env-file .env.local --locked python services/websites/launch.py
uv run --env-file .env.local --locked python services/gitlab/launch.py
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/canary-manifest.json" --task-id 003
AGENT_MANIFEST="$RUN_ROOT/canary-manifest.json" REQUIRE_NO_MODEL_COVERAGE=0 \
    PARALLEL_CONCURRENCY=1 MAX_STEPS=3 AGENT_TASK_TIMEOUT_SECONDS=900 \
    AGENT_RETRY_ATTEMPTS=0 RAW_DIR="$RUN_ROOT/canary-raw" \
    OUTPUT="$RUN_ROOT/canary.json" runner/run_agent_parallel.sh

export M3_MAX_LLM_RETRIES=2
export OSWORLD_CAMPAIGN_ID="osworld-v2-sample24-$(date -u +%Y%m%dT%H%M%SZ)"
uv run --env-file .env.local --locked python services/websites/launch.py
uv run --env-file .env.local --locked python services/gitlab/launch.py
sample_args=()
for task_id in 003 008 011 015 019 026 035 038 046 048 050 053 057 059 067 069 079 082 083 092 093 103 105 107; do
    sample_args+=(--task-id "$task_id")
done
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/sample24-manifest.json" "${sample_args[@]}"
AGENT_MANIFEST="$RUN_ROOT/sample24-manifest.json" REQUIRE_NO_MODEL_COVERAGE=1 \
    PARALLEL_CONCURRENCY=12 MAX_STEPS=500 \
    AGENT_TASK_TIMEOUT_SECONDS=28800 AGENT_RETRY_ATTEMPTS=0 AGENT_START_STAGGER_SECONDS=1 \
    RUN_TASK_082_CONCURRENT=1 RAW_DIR="$RUN_ROOT/sample24-raw" \
    OUTPUT="$RUN_ROOT/sample24.json" runner/run_agent_parallel.sh
```

## Verification ladder

Rungs run in order; PATH_PASS is never reported as task success:

1. Static checks (typecheck, compile, bridge unit tests).
2. Live desktop smoke (windows present, first-run modals absent, non-empty a11y tree).
3. Two-pass environment-path validation with fresh-sandbox-per-task proof.
4. Snapshot save/revert probe.
5. No-agent evaluator run (expected zeros).
6. Small full-agent run with audited trajectories.
7. Resource profiling (`maintainer/profile_resources.sh`) → `resource-requirements.json`;
   run soon after the ladder while metrics are within E2B's retention window.

For a full single-pass run: `VALIDATION_MANIFEST=validation/full-manifest.json`,
`VALIDATION_RUNS=1`, a distinct `EVIDENCE_DIR`, then `maintainer/validate.sh`.
