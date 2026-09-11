#!/usr/bin/env bash
# OSWorld-V2 -> E2B environment-path validation: run the selected manifest
# against the immutable guest template, preserving per-run receipts and raw
# relay logs. The final gate derives its expected task and unique-sandbox counts
# from the manifest and VALIDATION_RUNS rather than assuming the 10-task sample.
#
# The relay + harness both run under `uv run --python 3.12` using the pinned
# OSWorld-V2 checkout's project env, with e2b + aiohttp layered on top.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"            # repo root
REPO_ROOT="$V2ROOT"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
MANIFEST="${VALIDATION_MANIFEST:-$V2ROOT/validation/manifest.json}"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/out/osworld-v2-evidence}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw}"
RUNS="${VALIDATION_RUNS:-2}"
UV="uv run --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1"

# ---- immutable-template gate ----------------------------------------------
# GUEST_TEMPLATE must be an immutable `name:build_id` reference (a UUID build id
# from `npm run build`), never a mutable alias like `osworld-v2-gnome:latest`.
if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9-]+:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "GUEST_TEMPLATE must be an immutable name:build_id reference (got: '${GUEST_TEMPLATE:-<unset>}')" >&2
    exit 2
fi
export GUEST_TEMPLATE
if [ -z "${OSWORLD_CAMPAIGN_ID:-}" ]; then echo "OSWORLD_CAMPAIGN_ID is required" >&2; exit 2; fi
export OSWORLD_CAMPAIGN_ID
export OSWORLD_EVAL_MODEL_MODE=stub
unset OPENAI_API_KEY OPENAI_API_KEY_CUA ANTHROPIC_API_KEY GEMINI_API_KEY MODEL_API_KEY
unset OSWORLD_EVAL_MODEL_API_KEY OSWORLD_USER_SIM_API_KEY

# ---- E2B key ---------------------------------------------------------------
if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

mkdir -p "$EVIDENCE_DIR" "$RAW_DIR"
overall=0
relay_pid=""
proxy_pid=""
outputs=()
cleanup_current() {
    curl -fsS -X POST http://127.0.0.1:14999/stop >/dev/null 2>&1 || true
    if [ -n "$relay_pid" ]; then wait "$relay_pid" 2>/dev/null || true; fi
    relay_pid=""
}
cleanup_all() {
    local status=$?
    trap - EXIT INT TERM
    cleanup_current
    if [ -n "$proxy_pid" ] && kill -0 "$proxy_pid" 2>/dev/null; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup_all EXIT INT TERM

if ! python3 "$HERE/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "$MANIFEST"; then
    exit 2
fi

# ---- fleet + asset wiring (consumed by relay and harness) ------------------
# Read the fleet interface from the gitignored runtime file the launchers wrote.
read -r WEBSITE_HOST_SUFFIX GITLAB_URL < <(python3 - "$SERVICES_DIR/.runtime.json" <<'PY'
import json, sys
rt = json.load(open(sys.argv[1]))
print(rt["websites"]["public_host_suffix"], rt["gitlab"]["url"])
PY
)
export WEBSITE_HOST_SUFFIX GITLAB_URL
export GITLAB_PRIVATE_TOKEN="$(cat "$SERVICES_DIR/.gitlab-token")"
export OSWORLD_FILE_BASE_URL="$TASKS_DIR/assets"
# Relay installs the in-guest Host-mapping proxy only when both of these point at
# the proxy script and the fleet runtime file.
export HOSTMAP_PROXY_SCRIPT="$SERVICES_DIR/hostmap_proxy.py"
export OSWORLD_FLEET_RULES="$SERVICES_DIR/.runtime.json"

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

    ( cd "$OSWORLD_ROOT" && $UV --locked --extra full python e2b_relay.py ) 2>"$relay_log" &
    relay_pid=$!
    ready=0
    for _ in $(seq 1 150); do
        if curl -fsS http://127.0.0.1:14999/health >/dev/null 2>&1; then ready=1; break; fi
        if ! kill -0 "$relay_pid" 2>/dev/null; then break; fi
        sleep 2
    done
    if [ "$ready" -ne 1 ]; then
        echo "relay did not become ready for run $run_number" >&2
        tail -60 "$relay_log" >&2
        cleanup_current
        overall=1
        continue
    fi

    ( cd "$OSWORLD_ROOT" && $UV --locked --extra full python "$HERE/harness.py" \
        --osworld-root "$OSWORLD_ROOT" \
        --tasks-dir "$TASKS_DIR" \
        --manifest "$MANIFEST" \
        --raw-dir "$RAW_DIR" \
        --output "$output" )
    status=$?
    [ "$status" -eq 0 ] || overall=1
    cleanup_current
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
