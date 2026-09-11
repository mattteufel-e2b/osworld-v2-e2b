#!/usr/bin/env bash
# One REAL-agent rollout (rung 6) for a single OSWorld-V2 manifest task, isolated
# on its own namespaced relay so many can run in parallel. Each invocation:
#   * launches a relay with OSWORLD_RELAY_PORT_BASE=$PORT_BASE (disjoint loopback
#     port block: control/server/CDP/VLC all shifted by the base; optional
#     task-service listeners use OSWORLD_TASK_SERVICE_PORTS `local:guest`
#     mappings so host ports remain unique while guest ports stay unchanged),
#   * runs agent_runner.py (DesktopEnv on provider e2b + the AGENT_KIND agent
#     built by runner/agents.py),
#   * tears the relay (and its guest sandbox) down.
# Relay start-up, watchdog and process-group teardown come from worker_lib.sh.
# Receipts land in RESULT_DIR (gitignored raw). No task/evaluator text is emitted.
#
# Required env: TASK_ID, DOMAIN, PORT_BASE, GUEST_TEMPLATE, E2B_API_KEY,
#               MODEL_API_KEY. Optional: MODEL_BASE_URL, MODEL, AGENT_KIND, MAX_STEPS,
#               and the upstream generation settings MAX_TOKENS, TEMPERATURE, TOP_P,
#               MAX_TRAJECTORY_LENGTH (unset = the agent kind's upstream default),
#               ENABLE_RECORDING=1 (upstream --enable_recording; mp4 per task).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/worker_lib.sh"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw}"
MAX_STEPS="${MAX_STEPS:-75}"

: "${TASK_ID:?TASK_ID required}"
: "${DOMAIN:?DOMAIN required}"
: "${PORT_BASE:?PORT_BASE required}"

require_immutable_guest_template
require_campaign_id
resolve_e2b_api_key

RESULT_DIR="${RESULT_DIR:-$RAW_DIR/agent/task_${TASK_ID}_pb${PORT_BASE}}"
OUTPUT="${OUTPUT:-$RAW_DIR/agent/receipt_task_${TASK_ID}.json}"
RELAY_LOG="${RELAY_LOG:-$RAW_DIR/agent/relay_task_${TASK_ID}_pb${PORT_BASE}.log}"

# Resolve and clear the receipt before preflight so a failed fresh invocation
# can never leave a previous task result looking current.
RAW_DIR="$(abspath "$RAW_DIR")"
RESULT_DIR="$(abspath "$RESULT_DIR")"
OUTPUT="$(abspath "$OUTPUT")"
RELAY_LOG="$(abspath "$RELAY_LOG")"
mkdir -p "$RESULT_DIR" "$(dirname "$OUTPUT")" "$(dirname "$RELAY_LOG")"
rm -f "$OUTPUT"

# run_agent_parallel.sh has already run the same checks for the whole manifest
# moments ago; 80 workers re-verifying the checkout at once is pure load.
if [ "${OSWORLD_PREFLIGHT_VERIFIED:-0}" != "1" ] && ! python3 "$HERE/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "${AGENT_MANIFEST:-$V2ROOT/validation/full-manifest.json}" \
    --task-id "$TASK_ID"; then
    exit 2
fi
if [ -z "${OSWORLD_RUN_NONCE:-}" ]; then
    echo "OSWORLD_RUN_NONCE is required" >&2
    exit 2
fi
export OSWORLD_RUN_NONCE

# ---- namespacing + agent model wiring -------------------------------------
namespace_relay "$PORT_BASE"
MODEL_BASE_URL="${MODEL_BASE_URL:-https://openrouter.ai/api/v1}"
MODEL_API_KEY="${MODEL_API_KEY:-${OPENROUTER_API_KEY:-}}"
: "${MODEL_API_KEY:?MODEL_API_KEY required}"
MODEL="${MODEL:-openai/gpt-4o}"
AGENT_KIND="${AGENT_KIND:-prompt}"
export MODEL_BASE_URL MODEL_API_KEY MODEL AGENT_KIND MAX_STEPS

export_fleet_wiring

echo "[task ${TASK_ID}/${DOMAIN}] port_base=${PORT_BASE} control=${CONTROL_PORT} agent=${AGENT_KIND} model=${MODEL}"

install_worker_traps
if ! start_relay "$RELAY_LOG"; then
    echo "[task ${TASK_ID}] relay did not become ready" >&2
    exit 1
fi

# Upstream generation flags, forwarded only when set so agents.py keeps the
# upstream default otherwise. (`${arr[@]+...}` keeps bash 3.2 happy under set -u.)
generation_args=()
for pair in MAX_TOKENS:--max-tokens TEMPERATURE:--temperature TOP_P:--top-p \
    MAX_TRAJECTORY_LENGTH:--max-trajectory-length; do
    name="${pair%%:*}"
    if [ -n "${!name:-}" ]; then generation_args+=("${pair#*:}" "${!name}"); fi
done
if [ "${ENABLE_RECORDING:-0}" = "1" ]; then
    export ENABLE_RECORDING  # receipts record the opt-in
    generation_args+=(--enable-recording)
fi
start_in_new_session "$OSWORLD_ROOT" "${WORKER_UV[@]}" python "$HERE/agent_runner.py" \
    --task-id "$TASK_ID" \
    --domain "$DOMAIN" \
    --tasks-dir "$TASKS_DIR" \
    --result-dir "$RESULT_DIR" \
    --output "$OUTPUT" \
    --agent-kind "$AGENT_KIND" \
    --model "$MODEL" \
    --max-steps "$MAX_STEPS" \
    --client-password "osworld-public-evaluation" \
    ${generation_args[@]+"${generation_args[@]}"} &
rollout_pid=$!
started_at=$SECONDS
wait_for_rollout "$AGENT_TASK_TIMEOUT_SECONDS"
status=$?
if [ "$status" -eq 124 ]; then
    echo "[task ${TASK_ID}] exceeded ${AGENT_TASK_TIMEOUT_SECONDS}s deadline" >&2
    # agent_runner publishes its receipt atomically, so a non-empty OUTPUT is a
    # complete result that finished just as the deadline fired — keep it.
    if [ -s "$OUTPUT" ]; then
        echo "[task ${TASK_ID}] completed receipt found at deadline; keeping it" >&2
    elif ! python3 "$HERE/write_timeout_receipt.py" \
        --output "$OUTPUT" \
        --task-id "$TASK_ID" \
        --domain "$DOMAIN" \
        --port-base "$PORT_BASE" \
        --model "$MODEL" \
        --agent-kind "$AGENT_KIND" \
        --max-steps "$MAX_STEPS" \
        --timeout-seconds "$AGENT_TASK_TIMEOUT_SECONDS" \
        --wall-clock-seconds "$((SECONDS - started_at))"; then
        echo "[task ${TASK_ID}] failed to write timeout receipt" >&2
    fi
fi

echo "[task ${TASK_ID}] agent_runner exit=${status}"
exit "$status"
