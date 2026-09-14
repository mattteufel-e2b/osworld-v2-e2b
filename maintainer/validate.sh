#!/usr/bin/env bash
# Maintainer-only release validation (sequential): run the
# selected manifest VALIDATION_RUNS times against the immutable guest template,
# preserving per-run receipts and raw relay logs. The final gate derives its
# expected task and unique-sandbox counts from the manifest and VALIDATION_RUNS.
#
# Relay + harness run under the pinned checkout's project env via worker_lib.sh;
# this script additionally owns the host-side fleet proxy for the whole run.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../runner/worker_lib.sh"  # shared with the benchmark path
MANIFEST="${VALIDATION_MANIFEST:-$V2ROOT/validation/manifest.json}"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/out/osworld-v2-evidence}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw}"
RUNS="${VALIDATION_RUNS:-2}"
# Host-side helper (fleet proxy) runs from the repo env, not the checkout's.
UV="uv run --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1"

require_immutable_guest_template
require_campaign_id
export OSWORLD_EVAL_MODEL_MODE=stub
unset OPENAI_API_KEY OPENAI_API_KEY_CUA ANTHROPIC_API_KEY GEMINI_API_KEY MODEL_API_KEY
unset OSWORLD_EVAL_MODEL_API_KEY OSWORLD_USER_SIM_API_KEY
resolve_e2b_api_key

mkdir -p "$EVIDENCE_DIR" "$RAW_DIR"
overall=0
proxy_pid=""
outputs=()
cleanup_all() {
    local status=$?
    trap - EXIT INT TERM
    worker_cleanup
    if [ -n "$proxy_pid" ] && kill -0 "$proxy_pid" 2>/dev/null; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup_all EXIT INT TERM

if ! python3 "$RUNNER_DIR/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "$MANIFEST"; then
    exit 2
fi

namespace_relay 0
export_fleet_wiring

echo "template=$GUEST_TEMPLATE"
echo "WEBSITE_HOST_SUFFIX=$WEBSITE_HOST_SUFFIX"
echo "GITLAB_URL=$GITLAB_URL"
echo "OSWORLD_FILE_BASE_URL=$OSWORLD_FILE_BASE_URL"

# Own the host-side fleet proxy for the full validation campaign. A proxy
# spawned by a service launcher is tied to that launcher's process/session and
# is not a durable dependency for a later validation command.
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
    $UV python "$SERVICES_DIR/hostmap_proxy.py" >"$RAW_DIR/validate-hostmap-proxy.log" 2>&1 &
proxy_pid=$!
proxy_ready=0
for _ in $(seq 1 30); do
    if ! kill -0 "$proxy_pid" 2>/dev/null; then break; fi
    if curl -fsS -H 'Host: mailhub.127.0.0.1.nip.io' \
        'http://127.0.0.1:8090/api/state?cookie=validation' >/dev/null 2>&1; then
        proxy_ready=1
        break
    fi
    sleep 2
done
if [ "$proxy_ready" -ne 1 ]; then
    echo "fleet proxy or website service failed readiness" >&2
    tail -40 "$RAW_DIR/validate-hostmap-proxy.log" >&2
    exit 1
fi

for run_number in $(seq 1 "$RUNS"); do
    output="$EVIDENCE_DIR/validate-run${run_number}.json"
    outputs+=("$output")
    relay_log="$RAW_DIR/validate-run${run_number}-relay.log"

    if ! start_relay "$relay_log"; then
        echo "relay did not become ready for run $run_number" >&2
        shutdown_relay
        overall=1
        continue
    fi

    start_in_new_session "$OSWORLD_ROOT" "${WORKER_UV[@]}" python "$HERE/harness.py" \
        --osworld-root "$OSWORLD_ROOT" \
        --tasks-dir "$TASKS_DIR" \
        --manifest "$MANIFEST" \
        --raw-dir "$RAW_DIR" \
        --output "$output" &
    rollout_pid=$!
    # One harness process runs every manifest task and bounds each with its own
    # TASK_TIMEOUT_SECONDS, so no per-task deadline applies here.
    wait_for_rollout 0
    status=$?
    [ "$status" -eq 0 ] || overall=1
    shutdown_relay
done

# ---- aggregate gate: every task passes and every sandbox id is unique ------
python3 - "$MANIFEST" "$RUNS" "${outputs[@]}" <<'PY' || overall=1
import json, sys
manifest_path, runs_text, *receipt_paths = sys.argv[1:]
manifest = json.load(open(manifest_path))
expected = len(manifest["tasks"]) * int(runs_text)
ids, passes, model_boundaries, tasks = [], 0, 0, 0
external_model_calls = 0
invalid_counter_records = 0
if len(receipt_paths) != int(runs_text):
    print(f"receipt count mismatch: expected {runs_text}, got {len(receipt_paths)}")
    sys.exit(1)
for path in receipt_paths:
    try:
        run = json.load(open(path))
    except FileNotFoundError:
        print(f"missing receipt: {path}"); sys.exit(1)
    for r in run["records"]:
        tasks += 1
        if r.get("path_status") == "PATH_PASS":
            passes += 1
        if r.get("path_status") == "MODEL_BOUNDARY_PASS":
            model_boundaries += 1
        sb = r.get("sandbox") or {}
        if sb.get("id"):
            ids.append(sb["id"])
        calls = r.get("external_model_calls")
        if type(calls) is not int:
            invalid_counter_records += 1
        else:
            external_model_calls += calls
uniq = set(ids)
validated_tasks = passes + model_boundaries
print(
    f"tasks={tasks} path_passes={passes} model_boundary_passes={model_boundaries} "
    f"validated_tasks={validated_tasks} sandbox_ids={len(ids)} unique={len(uniq)}"
)
print(f"external_model_calls={external_model_calls} invalid_counter_records={invalid_counter_records}")
ok = (
    (len(ids) == len(uniq))
    and (len(uniq) == expected)
    and (validated_tasks == tasks == expected)
    and external_model_calls == 0
    and invalid_counter_records == 0
)
print("VALIDATION GATE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY

exit "$overall"
