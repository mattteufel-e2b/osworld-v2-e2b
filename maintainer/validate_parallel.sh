#!/usr/bin/env bash
# Maintainer-only release validation: the full 108-task OSWorld-V2
# environment-path run with no model calls, bounded E2B concurrency. Not needed
# to run the benchmark (see README "Quick start"); it qualifies a template build.
# A worker can briefly own two sandboxes during strict reset, so 80 workers peak
# near 160 guest sandboxes; the two service sandboxes and retry headroom remain
# below the account's 200-concurrent-sandbox ceiling.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$V2ROOT"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
MANIFEST="${VALIDATION_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/out/osworld-v2-evidence/full-suite}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw/full-suite}"
OUTPUT="${OUTPUT:-$EVIDENCE_DIR/validate-run1.json}"
PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"
UV="uv run --python 3.12 --with e2b==2.34.0"

if [[ ! "$PARALLEL_CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
    echo "PARALLEL_CONCURRENCY must be a positive integer" >&2
    exit 2
fi
if [ "$PARALLEL_CONCURRENCY" -gt 80 ]; then
    echo "PARALLEL_CONCURRENCY must not exceed 80 (strict reset can double guest use)" >&2
    exit 2
fi
if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9-]+:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "GUEST_TEMPLATE must be an immutable name:build_id reference" >&2
    exit 2
fi
export GUEST_TEMPLATE
if [ -z "${OSWORLD_CAMPAIGN_ID:-}" ]; then echo "OSWORLD_CAMPAIGN_ID is required" >&2; exit 2; fi
export OSWORLD_CAMPAIGN_ID
export OSWORLD_EVAL_MODEL_MODE=stub
unset OPENAI_API_KEY OPENAI_API_KEY_CUA ANTHROPIC_API_KEY GEMINI_API_KEY MODEL_API_KEY
unset OSWORLD_EVAL_MODEL_API_KEY OSWORLD_USER_SIM_API_KEY

if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

mkdir -p "$EVIDENCE_DIR" "$RAW_DIR/workers"
proxy_pid=""
cleanup_proxy() {
    local status=$?
    trap - EXIT INT TERM
    if [ -n "$proxy_pid" ] && kill -0 "$proxy_pid" 2>/dev/null; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup_proxy EXIT INT TERM

if ! python3 "$V2ROOT/runner/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "$MANIFEST"; then
    exit 2
fi

# Own the host proxy for the entire campaign. The launchers' best-effort proxy
# child does not survive every calling shell/PTY lifecycle, which previously
# produced mid-run connection-refused failures despite healthy service guests.
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
        'http://127.0.0.1:8090/api/state?cookie=parallel-validation' >/dev/null 2>&1; then
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

task_ids=()
while IFS= read -r task_id; do task_ids+=("$task_id"); done < <(
    python3 - "$MANIFEST" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["tasks"]:
    print(item["id"])
PY
)

run_batch() {
    local -a batch=("$@")
    local -a pids=()
    local task_id slot port_base task_service_ports output log pid
    local batch_failed=0
    slot=0
    for task_id in "${batch[@]}"; do
        slot=$((slot + 1))
        port_base=$((slot * 500))
        task_service_ports=""
        if [ "$task_id" = "082" ]; then
            task_service_ports="3000:3000"
        fi
        output="$RAW_DIR/workers/task_${task_id}.json"
        log="$RAW_DIR/workers/task_${task_id}.log"
        OSWORLD_TASK_SERVICE_PORTS="$task_service_ports" \
            TASK_ID="$task_id" PORT_BASE="$port_base" \
            OUTPUT="$output" RAW_DIR="$RAW_DIR/workers" \
            "$HERE/run_path_task.sh" >"$log" 2>&1 &
        pid=$!
        pids+=("$pid")
        echo "launched task $task_id port_base=$port_base pid=$pid"
    done
    for pid in "${pids[@]}"; do
        wait "$pid" || batch_failed=1
    done
    return "$batch_failed"
}

overall=0
batch=()
for task_id in "${task_ids[@]}"; do
    batch+=("$task_id")
    if [ "${#batch[@]}" -eq "$PARALLEL_CONCURRENCY" ]; then
        run_batch "${batch[@]}" || overall=1
        batch=()
    fi
done
if [ "${#batch[@]}" -gt 0 ]; then run_batch "${batch[@]}" || overall=1; fi

python3 - "$MANIFEST" "$RAW_DIR/workers" "$OUTPUT" "$PARALLEL_CONCURRENCY" <<'PY' || overall=1
import json, platform, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

manifest_path, worker_dir, output_path, concurrency = sys.argv[1:]
manifest = json.load(open(manifest_path))
expected_ids = [item["id"] for item in manifest["tasks"]]
records = []
missing = []
for task_id in expected_ids:
    path = Path(worker_dir) / f"task_{task_id}.json"
    try:
        worker = json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        missing.append(task_id)
        continue
    if len(worker.get("records", [])) != 1 or worker["records"][0].get("id") != task_id:
        missing.append(task_id)
        continue
    records.append(worker["records"][0])

sandbox_ids = [r.get("sandbox", {}).get("id") for r in records if r.get("sandbox", {}).get("id")]
unique = set(sandbox_ids)
path_passes = sum(r.get("path_status") == "PATH_PASS" for r in records)
model_boundary_passes = sum(
    r.get("path_status") == "MODEL_BOUNDARY_PASS" for r in records
)
validated_tasks = path_passes + model_boundary_passes
evaluator_ran = sum(bool(r.get("evaluator_ran")) for r in records)
# A malformed counter is an invalid record, never a default: r.get(..., -1)
# could cancel a genuine positive count and pass the ==0 gate.
for r in records:
    if type(r.get("external_model_calls")) is not int or type(r.get("eval_model_call_attempts")) is not int:
        missing.append(r.get("id"))
valid = [
    r for r in records
    if type(r.get("external_model_calls")) is int and type(r.get("eval_model_call_attempts")) is int
]
external_model_calls = sum(r["external_model_calls"] for r in valid)
eval_model_call_attempts = sum(r["eval_model_call_attempts"] for r in valid)
summary = {
    "tasks": len(records),
    "expected_tasks": len(expected_ids),
    "missing_or_invalid_task_ids": missing,
    "path_passes": path_passes,
    "model_boundary_passes": model_boundary_passes,
    "validated_tasks": validated_tasks,
    "path_failures": sum(r.get("path_status") == "PATH_FAIL" for r in records),
    "evaluator_ran_count": evaluator_ran,
    "evaluation_mode": "no-model-stub",
    "eval_model_call_attempts": eval_model_call_attempts,
    "external_model_calls": external_model_calls,
    "unique_sandboxes": len(unique),
    "all_recorded_sandboxes_unique": len(unique) == len(sandbox_ids),
}
run = {
    "schema_version": 1,
    "run_id": str(uuid.uuid4()),
    "started_at": min((r["started_at"] for r in records), default=None),
    "finished_at": max((r["finished_at"] for r in records), default=datetime.now(timezone.utc).isoformat()),
    "purpose": "environment-path validation; not an agent benchmark score",
    "release": manifest.get("release"),
    "template": manifest["template"],
    "osworld_commit": manifest["osworld_commit"],
    "manifest": manifest,
    "host": {"python": platform.python_version(), "platform": platform.platform()},
    "execution": {
        "mode": "bounded-parallel-with-namespaced-task-service-ports",
        "max_parallel_workers": int(concurrency),
        "task_082_solo": False,
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
    and validated_tasks == evaluator_ran == len(expected_ids)
    and len(sandbox_ids) == len(unique) == len(expected_ids)
    and external_model_calls == 0
)
print("VALIDATION GATE:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
PY

exit "$overall"
