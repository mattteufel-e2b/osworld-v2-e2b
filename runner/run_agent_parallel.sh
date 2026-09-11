#!/usr/bin/env bash
# Run the complete OSWorld-V2 agent benchmark with bounded E2B concurrency.
# Strict reset can briefly own two guests per worker, so 80 workers peak near
# 160 guest sandboxes and leave room for the two fleet guests and retries under
# a 200-concurrent-sandbox account ceiling. Task 082 alone owns host port 3000,
# matching the canonical gated task without rewriting its task module.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$V2ROOT"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
MANIFEST="${AGENT_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"
AGENT_RETRY_ATTEMPTS="${AGENT_RETRY_ATTEMPTS:-0}"
AGENT_RETRY_CONCURRENCY="${AGENT_RETRY_CONCURRENCY:-4}"
AGENT_START_STAGGER_SECONDS="${AGENT_START_STAGGER_SECONDS:-0.25}"
RUN_TASK_082_CONCURRENT="${RUN_TASK_082_CONCURRENT:-1}"
REQUIRE_NO_MODEL_COVERAGE="${REQUIRE_NO_MODEL_COVERAGE:-0}"
MAX_STEPS="${MAX_STEPS:-500}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw/agent-full/$RUN_ID}"
OUTPUT="${OUTPUT:-$REPO_ROOT/out/osworld-v2-evidence/full-suite/agent-$RUN_ID.json}"
UV="uv run --python 3.12 --with e2b==2.34.0"

if [[ ! "$PARALLEL_CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
    echo "PARALLEL_CONCURRENCY must be a positive integer" >&2
    exit 2
fi
if [ "$PARALLEL_CONCURRENCY" -gt 80 ]; then
    echo "PARALLEL_CONCURRENCY must not exceed 80 (strict reset can double guest use)" >&2
    exit 2
fi
if [[ ! "$AGENT_RETRY_ATTEMPTS" =~ ^[0-9]+$ ]]; then
    echo "AGENT_RETRY_ATTEMPTS must be a non-negative integer" >&2
    exit 2
fi
if [[ ! "$AGENT_RETRY_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || [ "$AGENT_RETRY_CONCURRENCY" -gt 4 ]; then
    echo "AGENT_RETRY_CONCURRENCY must be between 1 and 4" >&2
    exit 2
fi
if [[ ! "$AGENT_START_STAGGER_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "AGENT_START_STAGGER_SECONDS must be a non-negative number" >&2
    exit 2
fi
if [ "$RUN_TASK_082_CONCURRENT" != "0" ] && [ "$RUN_TASK_082_CONCURRENT" != "1" ]; then
    echo "RUN_TASK_082_CONCURRENT must be 0 or 1" >&2
    exit 2
fi
if [ "$REQUIRE_NO_MODEL_COVERAGE" != "0" ] && [ "$REQUIRE_NO_MODEL_COVERAGE" != "1" ]; then
    echo "REQUIRE_NO_MODEL_COVERAGE must be 0 or 1" >&2
    exit 2
fi
source "$HERE/worker_env.sh"
if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9-]+:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "GUEST_TEMPLATE must be an immutable name:build_id reference" >&2
    exit 2
fi
: "${MODEL_API_KEY:?MODEL_API_KEY required}"
: "${MODEL_BASE_URL:?MODEL_BASE_URL required}"
: "${MODEL:?MODEL required}"
AGENT_KIND="${AGENT_KIND:-prompt}"
if [ "$AGENT_KIND" != "prompt" ] && [ "$AGENT_KIND" != "m3" ]; then
    echo "AGENT_KIND must be prompt or m3" >&2
    exit 2
fi
if [ "$AGENT_KIND" = "m3" ] && [[ ! "${M3_THINKING_BUDGET:-}" =~ ^[1-9][0-9]*$ ]]; then
    echo "M3_THINKING_BUDGET must be explicit and positive for an M3 benchmark" >&2
    exit 2
fi
if [ "$AGENT_KIND" = "m3" ] && [[ ! "${M3_MAX_LLM_RETRIES:-}" =~ ^[0-9]+$ ]]; then
    echo "M3_MAX_LLM_RETRIES must be explicit and non-negative for an M3 benchmark" >&2
    exit 2
fi
if [ -z "${OSWORLD_CAMPAIGN_ID:-}" ]; then echo "OSWORLD_CAMPAIGN_ID is required" >&2; exit 2; fi
export GUEST_TEMPLATE OSWORLD_CAMPAIGN_ID MODEL_API_KEY MODEL_BASE_URL MODEL AGENT_KIND MAX_STEPS
export M3_THINKING_MODE M3_THINKING_BUDGET M3_MAX_LLM_RETRIES

if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

mkdir -p "$RAW_DIR"
proxy_pid=""
worker_pids=()
fleets_admitted=0
cleanup_proxy() {
    local status=$?
    trap - EXIT
    trap '' INT TERM
    local pid
    for pid in ${worker_pids[@]+"${worker_pids[@]}"}; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in ${worker_pids[@]+"${worker_pids[@]}"}; do
        wait "$pid" 2>/dev/null || true
    done
    if [ -n "$proxy_pid" ] && kill -0 "$proxy_pid" 2>/dev/null; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    # Fleets are torn down only for a run that was admitted. A rejection before
    # that (preflight, lifetime gate) is something the operator acts on with the
    # same fleets, so leave them running for that.
    if [ "$fleets_admitted" -ne 1 ]; then
        echo "service fleets for campaign $OSWORLD_CAMPAIGN_ID left running (run not admitted);" \
            "stop them with services/stop.py --campaign-id $OSWORLD_CAMPAIGN_ID" >&2
    elif [ "${TEARDOWN_FLEETS_ON_EXIT:-1}" = "1" ]; then
        if ! $UV python "$SERVICES_DIR/stop.py" --campaign-id "$OSWORLD_CAMPAIGN_ID" \
            >>"$RAW_DIR/service-teardown.log" 2>&1; then
            echo "service fleet cleanup failed; recovery state was preserved" >&2
            status=1
        fi
    fi
    exit "$status"
}
trap cleanup_proxy EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$REQUIRE_NO_MODEL_COVERAGE" = "1" ]; then
    : "${NO_MODEL_RECEIPT:?NO_MODEL_RECEIPT is required for full-agent coverage}"
    if ! python3 "$HERE/model_coverage.py" \
        --no-model-receipt "$NO_MODEL_RECEIPT" --agent-manifest "$MANIFEST"; then
        exit 2
    fi
fi

if ! python3 "$HERE/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "$MANIFEST"; then
    exit 2
fi
export OSWORLD_PREFLIGHT_VERIFIED=1  # workers skip the checks just made for them

export PARALLEL_CONCURRENCY RUN_TASK_082_CONCURRENT AGENT_START_STAGGER_SECONDS
if ! $UV python "$V2ROOT/services/fleetlib.py" --check-lifetime "$MANIFEST" \
    --runtime "$SERVICES_DIR/.runtime.json"; then
    exit 2
fi
fleets_admitted=1

mkdir -p "$RAW_DIR/workers" "$(dirname "$OUTPUT")"
RUN_NONCE="$(python3 "$HERE/prepare_agent_run.py" \
    --manifest "$MANIFEST" --worker-dir "$RAW_DIR/workers")" || exit 2
export OSWORLD_RUN_NONCE="$RUN_NONCE"

if python3 - <<'PY'
import socket
s = socket.socket()
s.settimeout(0.2)
occupied = s.connect_ex(("127.0.0.1", 8090)) == 0
s.close()
raise SystemExit(1 if occupied else 0)
PY
then :; else
    echo "127.0.0.1:8090 is already occupied; refusing an ambiguous fleet proxy" >&2
    exit 2
fi

HOSTMAP_PORT="8090" FLEET_RUNTIME_FILE="$SERVICES_DIR/.runtime.json" \
    $UV python "$SERVICES_DIR/hostmap_proxy.py" >"$RAW_DIR/hostmap-proxy.log" 2>&1 &
proxy_pid=$!
proxy_ready=0
for _ in $(seq 1 30); do
    if ! kill -0 "$proxy_pid" 2>/dev/null; then break; fi
    if curl -fsS --connect-timeout 2 --max-time 5 -H 'Host: mailhub.127.0.0.1.nip.io' \
        'http://127.0.0.1:8090/api/state?cookie=agent-benchmark' >/dev/null 2>&1; then
        proxy_ready=1
        break
    fi
    sleep 2
done
if [ "$proxy_ready" -ne 1 ]; then
    echo "fleet proxy or website service failed readiness" >&2
    tail -40 "$RAW_DIR/hostmap-proxy.log" >&2
    exit 1
fi

task_rows=()
while IFS= read -r row; do task_rows+=("$row"); done < <(
    python3 - "$MANIFEST" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["tasks"]:
    print(item["id"], item.get("domain", "release"))
PY
)

run_batch() {
    local -a batch=("$@")
    worker_pids=()
    local row task_id domain slot port_base task_service_ports receipt result_dir log pid index
    local batch_failed=0
    slot=0
    for row in "${batch[@]}"; do
        read -r task_id domain <<<"$row"
        slot=$((slot + 1))
        port_base=$((slot * 500))
        task_service_ports=""
        if [ "$task_id" = "082" ]; then
            # Canonical task 082 dials localhost:3000 from the host; this is the
            # only task-service listener in the release, so it can stay literal.
            task_service_ports="3000:3000"
        fi
        receipt="$RAW_DIR/workers/task_${task_id}.json"
        result_dir="$RAW_DIR/workers/task_${task_id}${ATTEMPT_SUFFIX:-}"
        log="$RAW_DIR/workers/task_${task_id}${ATTEMPT_SUFFIX:-}.log"
        OSWORLD_TASK_SERVICE_PORTS="$task_service_ports" \
            TASK_ID="$task_id" DOMAIN="$domain" \
            PORT_BASE="$port_base" OUTPUT="$receipt" RESULT_DIR="$result_dir" \
            RAW_DIR="$RAW_DIR" "$HERE/run_agent.sh" >"$log" 2>&1 &
        pid=$!
        worker_pids+=("$pid")
        echo "launched agent task $task_id port_base=$port_base pid=$pid"
        sleep "$AGENT_START_STAGGER_SECONDS"
    done
    for index in "${!worker_pids[@]}"; do
        wait "${worker_pids[$index]}" || batch_failed=1
        unset 'worker_pids[index]'
    done
    return "$batch_failed"
}

overall=0
batch=()
task_082_row=""
for row in "${task_rows[@]}"; do
    read -r task_id _ <<<"$row"
    if [ "$task_id" = "082" ]; then
        if [ "$RUN_TASK_082_CONCURRENT" != "1" ]; then
            task_082_row="$row"
            continue
        fi
    fi
    batch+=("$row")
    if [ "${#batch[@]}" -eq "$PARALLEL_CONCURRENCY" ]; then
        run_batch "${batch[@]}" || overall=1
        batch=()
    fi
done
if [ "${#batch[@]}" -gt 0 ]; then run_batch "${batch[@]}" || overall=1; fi

if [ -n "$task_082_row" ]; then
    echo "running agent task 082 solo on canonical host port 3000"
    run_batch "$task_082_row" || overall=1
fi

# Retry infrastructure/path errors in a deliberately small wave. This is not a
# score retry: completed low-scoring tasks are never resampled. The first wave
# can transiently overload a shared stateful service even though each guest is
# isolated, so a recovered setup must replace the failed receipt before gating.
for ((attempt=1; attempt <= AGENT_RETRY_ATTEMPTS; attempt++)); do
    failed_rows=()
    while IFS= read -r row; do failed_rows+=("$row"); done < <(
        python3 "$HERE/retry_candidates.py" "$MANIFEST" "$RAW_DIR/workers"
    )
    if [ "${#failed_rows[@]}" -eq 0 ]; then break; fi
    # Budget the retry wave against the tasks that actually failed, at retry
    # concurrency with 082 solo; skipping it is not fatal, the failures stand.
    retry_ids=()
    for row in "${failed_rows[@]}"; do
        read -r task_id _ <<<"$row"
        retry_ids+=(--task-id "$task_id")
    done
    if ! PARALLEL_CONCURRENCY="$AGENT_RETRY_CONCURRENCY" RUN_TASK_082_CONCURRENT=0 \
        $UV python "$V2ROOT/services/fleetlib.py" --check-lifetime "$MANIFEST" \
        --runtime "$SERVICES_DIR/.runtime.json" "${retry_ids[@]}"; then
        echo "skipping retry attempt $attempt: fleets cannot outlast a ${#failed_rows[@]}-task retry wave" >&2
        break
    fi
    echo "retrying ${#failed_rows[@]} infrastructure/path failures (attempt $attempt)"
    overall=0
    retry_batch=()
    retry_082_row=""
    ATTEMPT_SUFFIX="_retry_${attempt}"
    export ATTEMPT_SUFFIX
    for row in "${failed_rows[@]}"; do
        read -r task_id _ <<<"$row"
        cp "$RAW_DIR/workers/task_${task_id}.json" \
            "$RAW_DIR/workers/task_${task_id}_before_retry_${attempt}.json" 2>/dev/null || true
        if [ "$task_id" = "082" ]; then
            retry_082_row="$row"
            continue
        fi
        retry_batch+=("$row")
        if [ "${#retry_batch[@]}" -eq "$AGENT_RETRY_CONCURRENCY" ]; then
            run_batch "${retry_batch[@]}" || overall=1
            retry_batch=()
        fi
    done
    if [ "${#retry_batch[@]}" -gt 0 ]; then run_batch "${retry_batch[@]}" || overall=1; fi
    if [ -n "$retry_082_row" ]; then
        run_batch "$retry_082_row" || overall=1
    fi
done
unset ATTEMPT_SUFFIX

aggregate_args=(
    --manifest "$MANIFEST"
    --worker-dir "$RAW_DIR/workers"
    --output "$OUTPUT"
    --model "$MODEL"
    --agent-kind "$AGENT_KIND"
    --model-transport "$MODEL_BASE_URL"
    --eval-model "${OSWORLD_EVAL_MODEL_NAME:-}"
    --eval-provider "${OSWORLD_EVAL_MODEL_PROVIDER:-}"
    --eval-transport "${OSWORLD_EVAL_MODEL_BASE_URL:-}"
    --user-sim-model "${OSWORLD_USER_SIM_MODEL:-}"
    --user-sim-provider "${OSWORLD_USER_SIM_PROVIDER:-}"
    --user-sim-transport "${OSWORLD_USER_SIM_BASE_URL:-}"
    --max-steps "$MAX_STEPS"
    --concurrency "$PARALLEL_CONCURRENCY"
    --thinking-budget "${M3_THINKING_BUDGET:-0}"
    --run-nonce "$RUN_NONCE"
    --campaign-id "$OSWORLD_CAMPAIGN_ID"
)
if [ -n "${M3_THINKING_MODE:-}" ]; then
    aggregate_args+=(--thinking-mode "$M3_THINKING_MODE")
fi
if [ "$AGENT_KIND" = "m3" ]; then
    aggregate_args+=(--m3-max-llm-retries "$M3_MAX_LLM_RETRIES")
fi
if [ "$RUN_TASK_082_CONCURRENT" = "1" ]; then
    aggregate_args+=(--task-082-concurrent)
fi
if [ "$REQUIRE_NO_MODEL_COVERAGE" = "1" ]; then
    aggregate_args+=(--no-model-receipt "$NO_MODEL_RECEIPT")
fi
python3 "$HERE/aggregate_agent.py" "${aggregate_args[@]}" || overall=1

exit "$overall"
