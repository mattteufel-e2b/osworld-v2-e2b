#!/usr/bin/env bash
# One REAL-agent rollout (rung 6) for a single OSWorld-V2 manifest task, isolated
# on its own namespaced relay so many can run in parallel. Each invocation:
#   * launches a relay with OSWORLD_RELAY_PORT_BASE=$PORT_BASE (disjoint loopback
#     port block: control/server/CDP/VLC all shifted by the base; optional
#     task-service listeners use OSWORLD_TASK_SERVICE_PORTS `local:guest`
#     mappings so host ports remain unique while guest ports stay unchanged),
#   * runs agent_runner.py (DesktopEnv on provider e2b + reference PromptAgent via
#     an OpenAI-compatible chat-completions endpoint),
#   * tears the relay (and its guest sandbox) down.
# Receipts land in RESULT_DIR (gitignored raw). No task/evaluator text is emitted.
#
# Required env: TASK_ID, DOMAIN, PORT_BASE, GUEST_TEMPLATE, E2B_API_KEY,
#               MODEL_API_KEY. Optional: MODEL_BASE_URL, MODEL, MAX_STEPS.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$V2ROOT"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw}"
MAX_STEPS="${MAX_STEPS:-75}"
AGENT_TASK_TIMEOUT_SECONDS="${AGENT_TASK_TIMEOUT_SECONDS:-14400}"
PROCESS_TERMINATION_GRACE_SECONDS="${PROCESS_TERMINATION_GRACE_SECONDS:-10}"
RELAY_STOP_REQUEST_TIMEOUT_SECONDS="${RELAY_STOP_REQUEST_TIMEOUT_SECONDS:-10}"
UV=(uv run --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1)

: "${TASK_ID:?TASK_ID required}"
: "${DOMAIN:?DOMAIN required}"
: "${PORT_BASE:?PORT_BASE required}"
if [[ ! "$AGENT_TASK_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "AGENT_TASK_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
fi
for timeout_name in PROCESS_TERMINATION_GRACE_SECONDS RELAY_STOP_REQUEST_TIMEOUT_SECONDS; do
    if [[ ! "${!timeout_name}" =~ ^[1-9][0-9]*$ ]]; then
        echo "$timeout_name must be a positive integer" >&2
        exit 2
    fi
done
if [[ ! "${AGENT_WATCHDOG_POLL_SECONDS:-5}" =~ ^[1-9][0-9]*$ ]]; then
    echo "AGENT_WATCHDOG_POLL_SECONDS must be a positive integer" >&2
    exit 2
fi

if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9-]+:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "GUEST_TEMPLATE must be an immutable name:build_id reference (got: '${GUEST_TEMPLATE:-<unset>}')" >&2
    exit 2
fi
export GUEST_TEMPLATE
if [ -z "${OSWORLD_CAMPAIGN_ID:-}" ]; then echo "OSWORLD_CAMPAIGN_ID is required" >&2; exit 2; fi
export OSWORLD_CAMPAIGN_ID

if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

RESULT_DIR="${RESULT_DIR:-$RAW_DIR/agent/task_${TASK_ID}_pb${PORT_BASE}}"
OUTPUT="${OUTPUT:-$RAW_DIR/agent/receipt_task_${TASK_ID}.json}"
RELAY_LOG="${RELAY_LOG:-$RAW_DIR/agent/relay_task_${TASK_ID}_pb${PORT_BASE}.log}"

abspath() {
    python3 - "$1" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
}

# Resolve and clear the receipt before preflight so a failed fresh invocation
# can never leave a previous task result looking current.
RAW_DIR="$(abspath "$RAW_DIR")"
RESULT_DIR="$(abspath "$RESULT_DIR")"
OUTPUT="$(abspath "$OUTPUT")"
RELAY_LOG="$(abspath "$RELAY_LOG")"
mkdir -p "$RESULT_DIR" "$(dirname "$OUTPUT")" "$(dirname "$RELAY_LOG")"
rm -f "$OUTPUT"

if ! python3 "$HERE/preflight.py" \
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
export OSWORLD_RELAY_PORT_BASE="$PORT_BASE"
CONTROL_PORT=$((14999 + PORT_BASE))
export E2B_RELAY_CONTROL_URL="http://127.0.0.1:${CONTROL_PORT}"
MODEL_BASE_URL="${MODEL_BASE_URL:-https://openrouter.ai/api/v1}"
MODEL_API_KEY="${MODEL_API_KEY:-${OPENROUTER_API_KEY:-}}"
: "${MODEL_API_KEY:?MODEL_API_KEY required}"
export OPENAI_BASE_URL="$MODEL_BASE_URL"
export OPENAI_API_KEY="$MODEL_API_KEY"
export ANTHROPIC_BASE_URL="$MODEL_BASE_URL"
export ANTHROPIC_API_KEY="$MODEL_API_KEY"
MODEL="${MODEL:-openai/gpt-4o}"
AGENT_KIND="${AGENT_KIND:-prompt}"
export MODEL_BASE_URL MODEL AGENT_KIND MAX_STEPS

# Some release tasks use model-based evaluators. Keep that endpoint explicit
# because a provider's Anthropic- and OpenAI-compatible base URLs can differ.
if [ -n "${EVAL_MODEL_BASE_URL:-}" ]; then
    export OSWORLD_EVAL_MODEL_PROVIDER="openai_compatible"
    export OSWORLD_EVAL_MODEL_BASE_URL="$EVAL_MODEL_BASE_URL"
    export OSWORLD_EVAL_MODEL_API_KEY="${EVAL_MODEL_API_KEY:-$MODEL_API_KEY}"
    export OSWORLD_EVAL_MODEL_NAME="${EVAL_MODEL:-$MODEL}"
    export OSWORLD_USER_SIM_PROVIDER="openai_compatible"
    export OSWORLD_USER_SIM_BASE_URL="$EVAL_MODEL_BASE_URL"
    export OSWORLD_USER_SIM_API_KEY="${USER_SIM_API_KEY:-${EVAL_MODEL_API_KEY:-$MODEL_API_KEY}}"
    export OSWORLD_USER_SIM_MODEL="${USER_SIM_MODEL:-${EVAL_MODEL:-$MODEL}}"
fi

# ---- fleet + asset wiring (same as validate.sh) ---------------------------
read -r WEBSITE_HOST_SUFFIX GITLAB_URL < <(python3 - "$SERVICES_DIR/.runtime.json" <<'PY'
import json, sys
rt = json.load(open(sys.argv[1]))
print(rt["websites"]["public_host_suffix"], rt["gitlab"]["url"])
PY
)
export WEBSITE_HOST_SUFFIX GITLAB_URL
export GITLAB_PRIVATE_TOKEN="$(cat "$SERVICES_DIR/.gitlab-token")"
export OSWORLD_FILE_BASE_URL="$TASKS_DIR/assets"
export HOSTMAP_PROXY_SCRIPT="$SERVICES_DIR/hostmap_proxy.py"
export OSWORLD_FLEET_RULES="$SERVICES_DIR/.runtime.json"

echo "[task ${TASK_ID}/${DOMAIN}] port_base=${PORT_BASE} control=${CONTROL_PORT} agent=${AGENT_KIND} model=${MODEL}"

relay_pid=""
agent_pid=""
cleanup_started=0

# Make the background command the leader of a new session and process group.
# Python's setsid is available on every supported Unix host, unlike the
# util-linux `setsid` executable (which is absent on macOS development hosts).
start_in_new_session() {
    local working_directory="$1"
    shift
    exec python3 -c '
import os
import sys

os.chdir(sys.argv[1])
os.setsid()
os.execvp(sys.argv[2], sys.argv[2:])
' "$working_directory" "$@"
}

process_group_alive() {
    kill -0 -- "-$1" 2>/dev/null
}

wait_for_process_group() {
    local process_group="$1"
    local timeout_seconds="$2"
    local deadline=$((SECONDS + timeout_seconds))
    while process_group_alive "$process_group" && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 1
    done
    ! process_group_alive "$process_group"
}

terminate_process_group() {
    local process_group="$1"
    if process_group_alive "$process_group"; then
        kill -TERM -- "-$process_group" 2>/dev/null || true
        if ! wait_for_process_group "$process_group" "$PROCESS_TERMINATION_GRACE_SECONDS"; then
            kill -KILL -- "-$process_group" 2>/dev/null || true
            wait_for_process_group "$process_group" "$PROCESS_TERMINATION_GRACE_SECONDS" || true
        fi
    fi
    if ! process_group_alive "$process_group"; then
        wait "$process_group" 2>/dev/null || true
    fi
}

# shellcheck disable=SC2329  # Called from EXIT/signal cleanup.
shutdown_relay() {
    local process_group="$relay_pid"
    if [ -z "$process_group" ]; then
        return
    fi
    relay_pid=""
    curl -fsS \
        --connect-timeout "$RELAY_STOP_REQUEST_TIMEOUT_SECONDS" \
        --max-time "$RELAY_STOP_REQUEST_TIMEOUT_SECONDS" \
        -X POST "http://127.0.0.1:${CONTROL_PORT}/stop" >/dev/null 2>&1 || true
    if ! wait_for_process_group "$process_group" "$PROCESS_TERMINATION_GRACE_SECONDS"; then
        terminate_process_group "$process_group"
    else
        wait "$process_group" 2>/dev/null || true
    fi
}

# shellcheck disable=SC2329  # Registered as an EXIT trap below.
cleanup() {
    if [ "$cleanup_started" -eq 1 ]; then
        return
    fi
    cleanup_started=1
    # Once teardown starts it is bounded; do not let a second signal interrupt
    # it between recording a process group and terminating that whole group.
    trap '' INT TERM
    if [ -n "$agent_pid" ]; then
        local process_group="$agent_pid"
        terminate_process_group "$process_group"
        agent_pid=""
    fi
    shutdown_relay
}

# shellcheck disable=SC2329  # Registered as INT/TERM traps below.
handle_signal() {
    local exit_code="$1"
    cleanup
    exit "$exit_code"
}

trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

start_in_new_session "$OSWORLD_ROOT" "${UV[@]}" python e2b_relay.py 2>"$RELAY_LOG" &
relay_pid=$!
ready=0
for _ in $(seq 1 150); do
    if curl -fsS "http://127.0.0.1:${CONTROL_PORT}/health" >/dev/null 2>&1; then ready=1; break; fi
    if ! kill -0 "$relay_pid" 2>/dev/null; then break; fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    echo "[task ${TASK_ID}] relay did not become ready" >&2
    tail -40 "$RELAY_LOG" >&2
    exit 1
fi

start_in_new_session "$OSWORLD_ROOT" "${UV[@]}" python "$HERE/agent_runner.py" \
    --task-id "$TASK_ID" \
    --domain "$DOMAIN" \
    --tasks-dir "$TASKS_DIR" \
    --result-dir "$RESULT_DIR" \
    --output "$OUTPUT" \
    --agent-kind "$AGENT_KIND" \
    --model "$MODEL" \
    --max-steps "$MAX_STEPS" \
    --client-password "osworld-public-evaluation" &
agent_pid=$!
timed_out=0
started_at=$SECONDS
watchdog_poll="${AGENT_WATCHDOG_POLL_SECONDS:-5}"
while kill -0 "$agent_pid" 2>/dev/null; do
    if [ $((SECONDS - started_at)) -ge "$AGENT_TASK_TIMEOUT_SECONDS" ]; then
        timed_out=1
        echo "[task ${TASK_ID}] exceeded ${AGENT_TASK_TIMEOUT_SECONDS}s deadline" >&2
        process_group="$agent_pid"
        terminate_process_group "$process_group"
        agent_pid=""
        break
    fi
    sleep "$watchdog_poll"
done
if [ "$timed_out" -eq 1 ]; then
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
    status=124
else
    process_group="$agent_pid"
    wait "$agent_pid"
    status=$?
    # A successful leader exit must not leave helpers running in its session.
    if process_group_alive "$process_group"; then
        terminate_process_group "$process_group"
    fi
    agent_pid=""
fi

echo "[task ${TASK_ID}] agent_runner exit=${status}"
exit "$status"
