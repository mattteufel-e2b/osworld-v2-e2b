#!/usr/bin/env bash
# Run the complete OSWorld-V2 agent benchmark with bounded E2B concurrency.
# Strict reset can briefly own two guests per worker, so 80 workers peak near
# 160 guest sandboxes and leave room for the two fleet guests and retries under
# a 200-concurrent-sandbox account ceiling. Task 082's host-side service dial is
# mapped from a unique high relay port to guest port 3000.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$V2ROOT"
SERVICES_DIR="$V2ROOT/services"
MANIFEST="${AGENT_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"
AGENT_RETRY_ATTEMPTS="${AGENT_RETRY_ATTEMPTS:-2}"
AGENT_RETRY_CONCURRENCY="${AGENT_RETRY_CONCURRENCY:-4}"
AGENT_START_STAGGER_SECONDS="${AGENT_START_STAGGER_SECONDS:-0.25}"
RUN_TASK_082_CONCURRENT="${RUN_TASK_082_CONCURRENT:-1}"
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
: "${EVAL_MODEL_BASE_URL:?EVAL_MODEL_BASE_URL required for release model judges}"
: "${EVAL_MODEL:?EVAL_MODEL required for release model judges}"
EVAL_MODEL_API_KEY="${EVAL_MODEL_API_KEY:-$MODEL_API_KEY}"
export GUEST_TEMPLATE MODEL_API_KEY MODEL_BASE_URL MODEL AGENT_KIND MAX_STEPS
export M3_THINKING_MODE M3_THINKING_BUDGET
export EVAL_MODEL_BASE_URL EVAL_MODEL_API_KEY EVAL_MODEL

if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

mkdir -p "$RAW_DIR/workers" "$(dirname "$OUTPUT")"

proxy_pid=""
cleanup_proxy() {
    if [ -n "$proxy_pid" ] && kill -0 "$proxy_pid" 2>/dev/null; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
}
trap cleanup_proxy EXIT INT TERM

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
    if curl -fsS -H 'Host: mailhub.127.0.0.1.nip.io' \
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
    local -a pids=()
    local row task_id domain slot port_base task_service_ports task_082_host_port receipt result_dir log pid
    local batch_failed=0
    slot=0
    for row in "${batch[@]}"; do
        read -r task_id domain <<<"$row"
        slot=$((slot + 1))
        port_base=$((slot * 500))
        task_service_ports=""
        task_082_host_port=""
        if [ "$task_id" = "082" ]; then
            # Host-side task code reaches this worker's high relay port, which
            # maps to the unchanged guest-side AWS mock service on :3000.
            task_082_host_port=60082
            task_service_ports="$task_082_host_port:3000"
        fi
        receipt="$RAW_DIR/workers/task_${task_id}.json"
        result_dir="$RAW_DIR/workers/task_${task_id}${ATTEMPT_SUFFIX:-}"
        log="$RAW_DIR/workers/task_${task_id}${ATTEMPT_SUFFIX:-}.log"
        OSWORLD_TASK_SERVICE_PORTS="$task_service_ports" \
            OSWORLD_TASK_082_HOST_PORT="$task_082_host_port" \
            TASK_ID="$task_id" DOMAIN="$domain" \
            PORT_BASE="$port_base" OUTPUT="$receipt" RESULT_DIR="$result_dir" \
            RAW_DIR="$RAW_DIR" "$HERE/run_agent.sh" >"$log" 2>&1 &
        pid=$!
        pids+=("$pid")
        echo "launched agent task $task_id port_base=$port_base pid=$pid"
        sleep "$AGENT_START_STAGGER_SECONDS"
    done
    for pid in "${pids[@]}"; do
        wait "$pid" || batch_failed=1
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
    read -r task_id domain <<<"$task_082_row"
    echo "running agent task 082 solo with namespaced task-service port"
    OSWORLD_TASK_SERVICE_PORTS="60082:3000" OSWORLD_TASK_082_HOST_PORT="60082" \
        TASK_ID="$task_id" DOMAIN="$domain" \
        PORT_BASE="0" OUTPUT="$RAW_DIR/workers/task_082.json" \
        RESULT_DIR="$RAW_DIR/workers/task_082" RAW_DIR="$RAW_DIR" \
        "$HERE/run_agent.sh" >"$RAW_DIR/workers/task_082.log" 2>&1 || overall=1
fi

# Retry infrastructure/path errors in a deliberately small wave. This is not a
# score retry: completed low-scoring tasks are never resampled. The first wave
# can transiently overload a shared stateful service even though each guest is
# isolated, so a recovered setup must replace the failed receipt before gating.
for ((attempt=1; attempt <= AGENT_RETRY_ATTEMPTS; attempt++)); do
    failed_rows=()
    while IFS= read -r row; do failed_rows+=("$row"); done < <(
        python3 - "$MANIFEST" "$RAW_DIR/workers" <<'PY'
import json, sys
from pathlib import Path

manifest, worker_dir = sys.argv[1:]
for item in json.load(open(manifest))["tasks"]:
    task_id = item["id"]
    path = Path(worker_dir) / f"task_{task_id}.json"
    try:
        record = json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        record = {}
    if record.get("path_status") != "OK":
        print(task_id, item.get("domain", "release"))
PY
    )
    if [ "${#failed_rows[@]}" -eq 0 ]; then break; fi
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
        read -r task_id domain <<<"$retry_082_row"
        OSWORLD_TASK_SERVICE_PORTS="60082:3000" OSWORLD_TASK_082_HOST_PORT="60082" \
            TASK_ID="$task_id" DOMAIN="$domain" \
            PORT_BASE="0" OUTPUT="$RAW_DIR/workers/task_082.json" \
            RESULT_DIR="$RAW_DIR/workers/task_082_retry_${attempt}" RAW_DIR="$RAW_DIR" \
            "$HERE/run_agent.sh" >"$RAW_DIR/workers/task_082_retry_${attempt}.log" 2>&1 \
            || overall=1
    fi
done
unset ATTEMPT_SUFFIX

python3 - "$MANIFEST" "$RAW_DIR/workers" "$OUTPUT" "$PARALLEL_CONCURRENCY" \
    "$MODEL" "$MAX_STEPS" "$RUN_TASK_082_CONCURRENT" "$AGENT_KIND" "$MODEL_BASE_URL" \
    "${M3_THINKING_MODE:-}" "${M3_THINKING_BUDGET:-}" \
    "openai_compatible" "$EVAL_MODEL" "$EVAL_MODEL_BASE_URL" <<'PY' || overall=1
import json, sys, uuid
from datetime import UTC, datetime
from pathlib import Path

(
    manifest_path,
    worker_dir,
    output_path,
    concurrency,
    model,
    max_steps,
    task_082_concurrent_raw,
    agent_kind,
    model_base_url,
    thinking_mode,
    thinking_budget,
    eval_provider,
    eval_model,
    eval_base_url,
) = sys.argv[1:]
task_082_concurrent = task_082_concurrent_raw == "1"
manifest = json.load(open(manifest_path))
expected_ids = [item["id"] for item in manifest["tasks"]]
records, missing = [], []
for task_id in expected_ids:
    path = Path(worker_dir) / f"task_{task_id}.json"
    try:
        record = json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        missing.append(task_id)
        continue
    if record.get("id") != task_id:
        missing.append(task_id)
        continue
    records.append(record)

scores = [record["score"] for record in records if isinstance(record.get("score"), (int, float))]
sandbox_ids = [record["sandbox_id"] for record in records if record.get("sandbox_id")]
summary = {
    "tasks": len(records),
    "expected_tasks": len(expected_ids),
    "missing_or_invalid_task_ids": missing,
    "path_ok": sum(record.get("path_status") == "OK" for record in records),
    "evaluator_ran_count": sum(bool(record.get("evaluator_ran")) for record in records),
    "scored_tasks": len(scores),
    "mean_score": (sum(scores) / len(scores)) if scores else None,
    "partial_score": (sum(scores) / len(scores)) if scores else None,
    "binary_successes": sum(score == 1.0 for score in scores),
    "binary_accuracy": (
        sum(score == 1.0 for score in scores) / len(scores) if scores else None
    ),
    "unique_sandboxes": len(set(sandbox_ids)),
    "all_recorded_sandboxes_unique": len(set(sandbox_ids)) == len(sandbox_ids),
}
run = {
    "schema_version": 1,
    "run_id": str(uuid.uuid4()),
    "finished_at": datetime.now(UTC).isoformat(),
    "purpose": "OSWorld-V2 agent benchmark on E2B",
    "release": manifest.get("release"),
    "template": manifest["template"],
    "osworld_commit": manifest["osworld_commit"],
    "model": model,
    "agent_kind": agent_kind,
    "model_transport": model_base_url,
    "reasoning": {
        "mode": thinking_mode or None,
        "budget_tokens": int(thinking_budget) if thinking_budget else None,
    },
    "evaluator": {
        "provider": eval_provider,
        "model": eval_model,
        "transport": eval_base_url,
    },
    "max_steps": int(max_steps),
    "execution": {
        "mode": (
            "bounded-parallel-with-namespaced-task-service-port"
            if task_082_concurrent
            else "bounded-parallel-with-namespaced-task-service-port-solo"
        ),
        "max_parallel_workers": int(concurrency),
        "task_082_solo": not task_082_concurrent,
        "task_082_concurrent": task_082_concurrent,
        "host_proxy_owned_for_campaign": True,
    },
    "records": records,
    "summary": summary,
}
Path(output_path).write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
print(json.dumps(summary, sort_keys=True))
ok = (
    not missing
    and len(records) == len(expected_ids)
    and summary["path_ok"] == len(expected_ids)
    and summary["evaluator_ran_count"] == len(expected_ids)
    and summary["scored_tasks"] == len(expected_ids)
    and len(sandbox_ids) == len(set(sandbox_ids)) == len(expected_ids)
)
print("AGENT RECEIPT GATE:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
PY

exit "$overall"
