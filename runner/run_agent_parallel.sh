#!/usr/bin/env bash
# Run the complete OSWorld-V2 agent benchmark with bounded E2B concurrency.
# Each task runs as one agent_runner.py process; its E2BProvider owns an
# in-process bridge on OS-assigned loopback ports, so workers never collide
# and need no port arithmetic. Concurrency defaults to 80 and is capped at 120:
# strict reset can briefly own two guests per worker, so 80 workers peak near
# 160 guest sandboxes plus the two fleet sandboxes. The account admitted 201 in
# the live capacity probe and the strict-reset overlap is per task and brief, so
# an operator who has confirmed the org quota may raise this up to 120.
# Task 082 alone dials host port 3000, so it gets a
# literal task-service listener, and runs solo when RUN_TASK_082_CONCURRENT=0.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/common.sh"
MANIFEST="${AGENT_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
PARALLEL_CONCURRENCY="${PARALLEL_CONCURRENCY:-80}"
AGENT_RETRY_ATTEMPTS="${AGENT_RETRY_ATTEMPTS:-0}"
AGENT_RETRY_CONCURRENCY="${AGENT_RETRY_CONCURRENCY:-4}"
AGENT_START_STAGGER_SECONDS="${AGENT_START_STAGGER_SECONDS:-0.25}"
RUN_TASK_082_CONCURRENT="${RUN_TASK_082_CONCURRENT:-1}"
REQUIRE_NO_MODEL_COVERAGE="${REQUIRE_NO_MODEL_COVERAGE:-0}"
MAX_STEPS="${MAX_STEPS:-500}"
# Resuming continues an earlier run in place: its id selects the same raw dir
# and output (unless the operator overrides them) and its nonce is recovered
# from the receipts that survive, so kept rollouts still gate.
RESUME_RUN_ID="${RESUME_RUN_ID:-}"
RUN_ID="${RESUME_RUN_ID:-${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}}"
# Seconds between reap passes of the rolling worker pool. Not an operator knob.
POOL_POLL_SECONDS="${POOL_POLL_SECONDS:-5}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw/agent-full/$RUN_ID}"
OUTPUT="${OUTPUT:-$REPO_ROOT/out/osworld-v2-evidence/full-suite/agent-$RUN_ID.json}"
UV="uv run --python 3.12 --with e2b==2.34.0"
# Workers `cd` into the pinned checkout, so every path they are handed and every
# path this script writes to afterwards must already be absolute.
RAW_DIR="$(abspath "$RAW_DIR")"
OUTPUT="$(abspath "$OUTPUT")"
MANIFEST="$(abspath "$MANIFEST")"

if [[ ! "$PARALLEL_CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
    echo "PARALLEL_CONCURRENCY must be a positive integer" >&2
    exit 2
fi
if [ "$PARALLEL_CONCURRENCY" -gt 120 ]; then
    echo "PARALLEL_CONCURRENCY must not exceed 120 (strict reset can briefly double guest use)" >&2
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
# Default per-task wall-clock deadline: 28800s (8h) budgets a full 500-step
# rollout with headroom; override for shorter canaries.
AGENT_TASK_TIMEOUT_SECONDS="${AGENT_TASK_TIMEOUT_SECONDS:-28800}"
GUEST_READY_TIMEOUT_S="${GUEST_READY_TIMEOUT_S:-180}"
for knob in AGENT_TASK_TIMEOUT_SECONDS GUEST_READY_TIMEOUT_S; do
    if [[ ! "${!knob}" =~ ^[1-9][0-9]*$ ]]; then
        echo "$knob must be a positive integer" >&2
        exit 2
    fi
done
export AGENT_TASK_TIMEOUT_SECONDS GUEST_READY_TIMEOUT_S
if [[ ! "${ENABLE_RECORDING:-0}" =~ ^[01]$ ]]; then
    echo "ENABLE_RECORDING must be 0 or 1 (got: '$ENABLE_RECORDING')" >&2
    exit 2
fi
require_immutable_guest_template
require_campaign_id
resolve_e2b_api_key
: "${MODEL_API_KEY:?MODEL_API_KEY required}"
: "${MODEL_BASE_URL:?MODEL_BASE_URL required}"
: "${MODEL:?MODEL required}"
# Judge (and user-simulator, which shares the same pair) retry budget: an
# operator override wins, otherwise budget for the full run.
: "${OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS:=8}"
: "${OSWORLD_EVAL_MODEL_RETRY_DELAY:=10}"
export OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS OSWORLD_EVAL_MODEL_RETRY_DELAY
: "${AGENT_KIND:?AGENT_KIND required (prompt|m3|claude; see runner/agents.py)}"
if [ "$AGENT_KIND" = "m3" ] && [[ ! "${M3_THINKING_BUDGET:-}" =~ ^[1-9][0-9]*$ ]]; then
    echo "M3_THINKING_BUDGET must be explicit and positive for an M3 benchmark" >&2
    exit 2
fi
if [ "$AGENT_KIND" = "m3" ] && [[ ! "${M3_MAX_LLM_RETRIES:-}" =~ ^[0-9]+$ ]]; then
    echo "M3_MAX_LLM_RETRIES must be explicit and non-negative for an M3 benchmark" >&2
    exit 2
fi
export MODEL_API_KEY MODEL_BASE_URL MODEL AGENT_KIND MAX_STEPS
export MAX_TOKENS TEMPERATURE TOP_P MAX_TRAJECTORY_LENGTH  # unset = upstream default
export ENABLE_RECORDING  # 1 = upstream --enable_recording (mp4 per task); unset = off
export M3_THINKING_MODE M3_THINKING_BUDGET M3_MAX_LLM_RETRIES

mkdir -p "$RAW_DIR"
proxy_pid=""
watchdog_pid=""
PROXY_LOG="$RAW_DIR/hostmap-proxy.log"
LIVENESS_LOG="$RAW_DIR/fleet-liveness.log"
# The watchdog restarts the proxy from a subshell, so the pid cleanup has to
# kill lives on disk rather than in this shell's copy of $proxy_pid. A resumed
# run reuses RAW_DIR, so the file is emptied now: whatever pid an earlier run
# left there belongs to some unrelated process by the time this one exits.
HOSTMAP_PROXY_PID_FILE="$RAW_DIR/hostmap-proxy.pid"
: >"$HOSTMAP_PROXY_PID_FILE"
# Watchdog cadences. Neither is an operator knob; the tests shorten them.
PROXY_WATCHDOG_SECONDS="${PROXY_WATCHDOG_SECONDS:-30}"
FLEET_LIVENESS_SECONDS="${FLEET_LIVENESS_SECONDS:-60}"
# Cycles the watchdog skips after a refused restart (liveness probes continue).
PROXY_RESTART_BACKOFF_CYCLES=5
worker_pids=()
worker_task_ids=()
# Campaign-wide progress: N is the task count this run set out to complete,
# k counts every worker that has finished (retries included, so it can pass N),
# and the clock starts with the first pool.
progress_total=0
progress_done=0
progress_started=""
fleets_admitted=0
aggregated=0
cleanup_proxy() {
    local status=$?
    trap - EXIT
    trap '' INT TERM
    local pid
    # Workers are direct children running agent_runner.py under uv; uv forwards
    # SIGTERM to python, whose handler writes an "interrupted" receipt, stops its
    # bridge (killing the guest) and exits 143.
    for pid in ${worker_pids[@]+"${worker_pids[@]}"}; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in ${worker_pids[@]+"${worker_pids[@]}"}; do
        wait "$pid" 2>/dev/null || true
    done
    # The watchdog goes first: it would otherwise answer the proxy's death by
    # starting a replacement nobody is left to kill.
    if [ -n "$watchdog_pid" ] && kill -0 "$watchdog_pid" 2>/dev/null; then
        kill "$watchdog_pid" 2>/dev/null || true
        wait "$watchdog_pid" 2>/dev/null || true
    fi
    # The file holds the pid of whichever proxy this run last started, the
    # watchdog's replacements included, and is emptied when a restart is
    # refused. It is consulted only when $proxy_pid is non-empty -- i.e. only
    # after this shell started a proxy of its own -- so a rejection before
    # start_host_proxy kills nothing at all.
    local live_proxy_pid="$proxy_pid"
    if [ -n "$proxy_pid" ] && [ -s "$HOSTMAP_PROXY_PID_FILE" ]; then
        live_proxy_pid="$(cat "$HOSTMAP_PROXY_PID_FILE")"
    fi
    # A restarted proxy is the watchdog subshell's child, not ours, so the
    # `wait` is best-effort; the kill is what actually reaps it.
    if [ -n "$live_proxy_pid" ] && kill -0 "$live_proxy_pid" 2>/dev/null; then
        kill "$live_proxy_pid" 2>/dev/null || true
        wait "$live_proxy_pid" 2>/dev/null || true
    fi
    # An interrupted campaign still gets its receipt: the workers above have
    # been reaped, so every receipt they wrote is final. Its gate will fail --
    # that is correct, the run is incomplete -- so the status here is ignored
    # and `aggregated` keeps the normal path and this one from both running.
    if [ "$fleets_admitted" -eq 1 ] && [ "$aggregated" -ne 1 ]; then
        aggregated=1
        echo "aggregating an interrupted run into $OUTPUT" >&2
        python3 "$HERE/aggregate_agent.py" \
            ${aggregate_args[@]+"${aggregate_args[@]}"} || true
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

# Reject an unknown AGENT_KIND now, in the worker's own environment, rather than
# letting every worker discover it after its guest sandbox has been created.
if ! (cd "$OSWORLD_ROOT" && PYTHONPATH="$OSWORLD_ROOT" uv run --locked --extra full --python 3.12 \
        python "$HERE/agents.py" --check "$AGENT_KIND"); then
    echo "AGENT_KIND='$AGENT_KIND' rejected by runner/agents.py" >&2
    exit 2
fi

if ! PYTHONPATH="$OSWORLD_ROOT" uv run --project "$OSWORLD_ROOT" --locked --extra full --python 3.12 \
        python "$HERE/check_models.py" --manifest "$MANIFEST" --tasks-dir "$TASKS_DIR"; then
    echo "Live judge/simulator check failed; verify OSWORLD_EVAL_MODEL_* and OSWORLD_USER_SIM_* settings" >&2
    exit 2
fi

export PARALLEL_CONCURRENCY RUN_TASK_082_CONCURRENT AGENT_START_STAGGER_SECONDS
export_fleet_wiring
if ! $UV python "$V2ROOT/services/fleetlib.py" --check-lifetime "$MANIFEST" \
    --runtime "$SERVICES_DIR/.runtime.json"; then
    exit 2
fi

mkdir -p "$RAW_DIR/workers" "$(dirname "$OUTPUT")"
prepare_args=(--manifest "$MANIFEST" --worker-dir "$RAW_DIR/workers")
if [ -n "$RESUME_RUN_ID" ]; then prepare_args+=(--resume); fi
RUN_NONCE="$(python3 "$HERE/prepare_agent_run.py" "${prepare_args[@]}")" || exit 2
export OSWORLD_RUN_NONCE="$RUN_NONCE"

start_host_proxy "agent-benchmark" "$PROXY_LOG" || exit $?

# Consecutive-miss bookkeeping for one fleet. Returns the updated counters on
# stdout (bash 3.2 has no associative arrays) and escalates to stderr once per
# outage, not once per probe.
escalate_liveness() {
    local service="$1" state="$2" failures="$3" reported="$4"
    if [ "$state" != "fail" ]; then
        echo "0 0"
        return 0
    fi
    failures=$((failures + 1))
    if [ "$failures" -ge 3 ] && [ "$reported" -eq 0 ]; then
        reported=1
        echo "FLEET LIVENESS: $service unreachable for 3 probes;" \
            "see fleet-liveness.log" >&2
    fi
    echo "$failures $reported"
}

# One curl per fleet, in the readiness probe's form. Appends a line to
# fleet-liveness.log so an operator can see when an outage started. A failing
# probe never aborts the run: the workers' own receipts decide the campaign.
probe_fleet_liveness() {
    local websites="fail" gitlab="skip"
    if curl -fsS --connect-timeout 2 --max-time 5 --cacert "$OSWORLD_CA_CERT" \
        --resolve 'mailhub.127.0.0.1.nip.io:8090:127.0.0.1' \
        "https://mailhub.127.0.0.1.nip.io:8090/api/state?cookie=liveness" \
        >/dev/null 2>&1; then
        websites="ok"
    fi
    # No GitLab in this campaign's runtime wiring: the line says so rather than
    # claiming a health nothing measured.
    if [ -n "${GITLAB_URL:-}" ]; then
        gitlab="fail"
        # /api/v4/version is authenticated: without the campaign's token GitLab
        # answers 401, `curl -f` fails, and a healthy fleet reports an outage on
        # every probe. export_fleet_wiring exported the token for this.
        if curl -fsS --connect-timeout 2 --max-time 5 --cacert "$OSWORLD_CA_CERT" \
            -H "PRIVATE-TOKEN: ${GITLAB_PRIVATE_TOKEN:-}" \
            "$GITLAB_URL/api/v4/version" >/dev/null 2>&1; then
            gitlab="ok"
        fi
    fi
    printf '%s websites=%s gitlab=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$websites" "$gitlab" >>"$LIVENESS_LOG"
    read -r websites_failures websites_reported <<<"$(escalate_liveness \
        websites "$websites" "$websites_failures" "$websites_reported")"
    read -r gitlab_failures gitlab_reported <<<"$(escalate_liveness \
        gitlab "$gitlab" "$gitlab_failures" "$gitlab_reported")"
}

# Every worker's setup and evaluate traffic crosses this one proxy, so its
# death mid-campaign fails every remaining task while the run keeps buying
# model tokens. Watch it from the background and put a replacement back on
# 8090; the counters are the callee's to update (bash scopes them dynamically).
proxy_watchdog() {
    local since_probe=0 restart_backoff=0 restart_status=0
    local websites_failures=0 websites_reported=0
    local gitlab_failures=0 gitlab_reported=0
    while :; do
        # The sleep must not inherit this script's stdout/stderr: it outlives
        # the kill below by up to one interval, and a reader of the campaign's
        # output would block on the pipe that long after the run ended.
        sleep "$PROXY_WATCHDOG_SECONDS" >/dev/null 2>&1 </dev/null
        if [ "$restart_backoff" -gt 0 ]; then
            # A refused restart stays refused for a while (something else holds
            # 8090), so retrying every cycle only reprints the proxy log tail.
            restart_backoff=$((restart_backoff - 1))
        elif ! kill -0 "$proxy_pid" 2>/dev/null; then
            echo "hostmap proxy died; restarting" >&2
            echo "hostmap proxy died; restarting" >>"$PROXY_LOG"
            restart_status=0
            start_host_proxy "agent-benchmark" "$PROXY_LOG" || restart_status=$?
            if [ "$restart_status" -ne 0 ]; then
                # The two failures need opposite handling. 2: the port was
                # occupied, nothing of ours started, so the file must stop
                # naming the dead proxy -- cleanup would kill that pid once it
                # had been recycled -- and clearing this subshell's copy makes
                # the next cycle retry rather than poll a corpse. 1: a
                # replacement DID start (common.sh wrote its pid) and never
                # answered; it is alive, it holds 8090, and it is this
                # subshell's child, so the file is the only way cleanup can
                # reach it. Keep the file and adopt the pid.
                if [ "$restart_status" -eq 2 ]; then
                    : >"$HOSTMAP_PROXY_PID_FILE"
                    proxy_pid=""
                else
                    proxy_pid="$(cat "$HOSTMAP_PROXY_PID_FILE" 2>/dev/null)"
                    echo "hostmap proxy restarted but never became ready;" \
                        "pid $proxy_pid kept for cleanup" >&2
                fi
                restart_backoff="$PROXY_RESTART_BACKOFF_CYCLES"
                echo "hostmap proxy restart failed; retrying in" \
                    "$((PROXY_RESTART_BACKOFF_CYCLES * PROXY_WATCHDOG_SECONDS)) s" >&2
            fi
        fi
        since_probe=$((since_probe + PROXY_WATCHDOG_SECONDS))
        if [ "$since_probe" -ge "$FLEET_LIVENESS_SECONDS" ]; then
            since_probe=0
            probe_fleet_liveness
        fi
    done
}
proxy_watchdog &
watchdog_pid=$!

task_rows=()
while IFS= read -r row; do task_rows+=("$row"); done < <(
    python3 - "$MANIFEST" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["tasks"]:
    print(item["id"], item.get("domain", "release"))
PY
)

# A resumed run re-runs only what is still unscored: a task whose rollout
# already wrote a result is never bought again. prepare_agent_run.py --resume
# kept those receipts, and retry_candidates.py skips scored tasks on its own.
if [ -n "$RESUME_RUN_ID" ]; then
    unscored_rows=()
    for row in ${task_rows[@]+"${task_rows[@]}"}; do
        read -r task_id _ <<<"$row"
        scored=0
        for result in "$RAW_DIR/workers/task_${task_id}/result.txt" \
            "$RAW_DIR/workers/task_${task_id}"_retry_*/result.txt; do
            if [ -e "$result" ]; then scored=1; fi
        done
        if [ "$scored" -eq 0 ]; then unscored_rows+=("$row"); fi
    done
    task_rows=(${unscored_rows[@]+"${unscored_rows[@]}"})
fi

# Claude pauses 3s (upstream's default) after every native action unless told
# otherwise; that is dead time during a 500-step budgeted run, so default it
# to 0 for this kind only. Other kinds keep upstream's default.
if [ "$AGENT_KIND" = "claude" ] && [ -z "${SLEEP_AFTER_EXECUTION:-}" ]; then
    SLEEP_AFTER_EXECUTION=0
fi

# Upstream generation flags, forwarded only when set so agents.py keeps the
# upstream default otherwise. (`${arr[@]+...}` keeps bash 3.2 happy under set -u.)
generation_args=()
for pair in MAX_TOKENS:--max-tokens TEMPERATURE:--temperature TOP_P:--top-p \
    MAX_TRAJECTORY_LENGTH:--max-trajectory-length SLEEP_AFTER_EXECUTION:--sleep-after-execution; do
    name="${pair%%:*}"
    if [ -n "${!name:-}" ]; then generation_args+=("${pair#*:}" "${!name}"); fi
done
if [ "${ENABLE_RECORDING:-0}" = "1" ]; then
    generation_args+=(--enable-recording)
fi

launched_pid=""
launch_worker() {
    local row="$1"
    local task_id domain task_service_ports receipt result_dir log
    read -r task_id domain <<<"$row"
    task_service_ports=""
    if [ "$task_id" = "082" ]; then
        # Canonical task 082 dials localhost:3000 from the host; this is the
        # only task-service listener in the release, so it stays literal.
        task_service_ports="3000:3000"
    fi
    receipt="$RAW_DIR/workers/task_${task_id}.json"
    result_dir="$RAW_DIR/workers/task_${task_id}${ATTEMPT_SUFFIX:-}"
    log="$RAW_DIR/workers/task_${task_id}${ATTEMPT_SUFFIX:-}.log"
    rm -f "$receipt"
    # `exec` makes $! the worker itself rather than an intermediate shell,
    # so cancellation's `kill -TERM "$pid"` reaches uv (and the python it
    # supervises) instead of orphaning a rollout that still holds a guest.
    (
        cd "$OSWORLD_ROOT" || exit 1
        export OSWORLD_TASK_SERVICE_PORTS="$task_service_ports"
        exec "${WORKER_UV[@]}" python "$HERE/agent_runner.py" \
            --task-id "$task_id" \
            --domain "$domain" \
            --tasks-dir "$TASKS_DIR" \
            --result-dir "$result_dir" \
            --output "$receipt" \
            --agent-kind "$AGENT_KIND" \
            --model "$MODEL" \
            --max-steps "$MAX_STEPS" \
            --deadline-seconds "$AGENT_TASK_TIMEOUT_SECONDS" \
            ${generation_args[@]+"${generation_args[@]}"}
    ) >"$log" 2>&1 &
    launched_pid=$!
    # Registered before anything else runs: a signal between the fork and this
    # append would leave cleanup_proxy blind to a worker that holds a guest.
    worker_pids+=("$launched_pid")
    worker_task_ids+=("$task_id")
    echo "launched agent task $task_id pid=$launched_pid"
}

# One line per finished worker so an operator watching a multi-hour run can see
# progress without reading receipts. A missing or unparseable receipt is normal
# here (a worker killed before it wrote one) and must not break the line.
report_worker_exit() {
    local task_id="$1" status="$2" running="$3"
    local elapsed="$((SECONDS - progress_started))"
    local score cause
    read -r score cause <<<"$(python3 - "$RAW_DIR/workers/task_${task_id}.json" <<'PY'
import json, sys
try:
    record = json.load(open(sys.argv[1]))
except Exception:
    record = {}
if not isinstance(record, dict):
    record = {}
score = record.get("score")
cause = record.get("error_cause")
ok = isinstance(score, (int, float)) and not isinstance(score, bool)
print(score if ok else "none", cause if isinstance(cause, str) and cause else "-")
PY
)"
    printf 'task %s exit=%s score=%s cause=%s done=%s/%s running=%s elapsed=%02d:%02d:%02d\n' \
        "$task_id" "$status" "$score" "$cause" "$progress_done" "$progress_total" \
        "$running" \
        "$((elapsed / 3600))" "$((elapsed % 3600 / 60))" "$((elapsed % 60))"
}

# Rolling pool: keep $1 workers in flight, refilling a slot as soon as one frees
# instead of waiting out a whole batch on its slowest task. bash 3.2 has no
# `wait -n`, so exits are found by polling `kill -0` and the status is then read
# with a `wait` that returns immediately.
run_pool() {
    local concurrency="$1"
    shift
    local -a queue=("$@")
    local total="${#queue[@]}"
    local pool_failed=0 next=0 running=0
    local count index pid task_id status
    local alive_pids alive_ids
    worker_pids=()
    worker_task_ids=()
    # One clock and one counter for the whole run: the solo-082 pool and every
    # retry wave continue the campaign's progress instead of restarting it.
    if [ -z "$progress_started" ]; then progress_started="$SECONDS"; fi
    while [ "$next" -lt "$total" ] || [ "$running" -gt 0 ]; do
        while [ "$next" -lt "$total" ] && [ "$running" -lt "$concurrency" ]; do
            launch_worker "${queue[$next]}"
            next=$((next + 1))
            running=$((running + 1))
            sleep "$AGENT_START_STAGGER_SECONDS"
        done
        sleep "$POOL_POLL_SECONDS"
        alive_pids=()
        alive_ids=()
        count="${#worker_pids[@]}"
        index=0
        while [ "$index" -lt "$count" ]; do
            pid="${worker_pids[$index]}"
            task_id="${worker_task_ids[$index]}"
            index=$((index + 1))
            if kill -0 "$pid" 2>/dev/null; then
                alive_pids+=("$pid")
                alive_ids+=("$task_id")
                continue
            fi
            wait "$pid"
            status=$?
            if [ "$status" -ne 0 ]; then pool_failed=1; fi
            progress_done=$((progress_done + 1))
            running=$((running - 1))
            report_worker_exit "$task_id" "$status" "$running"
        done
        # Assigned, not unset: cleanup_proxy reaps whatever is in flight now.
        worker_pids=(${alive_pids[@]+"${alive_pids[@]}"})
        worker_task_ids=(${alive_ids[@]+"${alive_ids[@]}"})
    done
    return "$pool_failed"
}

# Built before the first guest exists so cleanup_proxy can aggregate an
# interrupted run with exactly the arguments the normal path would use.
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

# Everything above is local; from here a guest may exist, so an exit must stop the fleets.
fleets_admitted=1

overall=0
pool_rows=()
task_082_row=""
for row in ${task_rows[@]+"${task_rows[@]}"}; do
    read -r task_id _ <<<"$row"
    if [ "$task_id" = "082" ]; then
        if [ "$RUN_TASK_082_CONCURRENT" != "1" ]; then
            task_082_row="$row"
            continue
        fi
    fi
    pool_rows+=("$row")
done
progress_total="${#pool_rows[@]}"
if [ -n "$task_082_row" ]; then progress_total=$((progress_total + 1)); fi
if [ "${#pool_rows[@]}" -gt 0 ]; then
    run_pool "$PARALLEL_CONCURRENCY" "${pool_rows[@]}" || overall=1
fi

if [ -n "$task_082_row" ]; then
    echo "running agent task 082 solo on canonical host port 3000"
    run_pool 1 "$task_082_row" || overall=1
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
    python3 - "$RAW_DIR/workers/retries.json" "$attempt" "${failed_rows[@]}" <<'PY'
import json, sys
path, attempt, rows = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
try:
    waves = json.load(open(path))
except (FileNotFoundError, json.JSONDecodeError):
    waves = []
waves.append({"attempt": attempt, "task_ids": [row.split()[0] for row in rows]})
json.dump(waves, open(path, "w"), indent=2)
PY

    echo "retrying ${#failed_rows[@]} infrastructure/path failures (attempt $attempt)"
    overall=0
    retry_rows=()
    retry_082_row=""
    ATTEMPT_SUFFIX="_retry_${attempt}"
    export ATTEMPT_SUFFIX
    for row in "${failed_rows[@]}"; do
        read -r task_id _ <<<"$row"
        # Audit copy for humans reading the raw dir; the aggregate reads
        # retries.json, never these _before_retry_ files.
        cp "$RAW_DIR/workers/task_${task_id}.json" \
            "$RAW_DIR/workers/task_${task_id}_before_retry_${attempt}.json" 2>/dev/null || true
        if [ "$task_id" = "082" ]; then
            retry_082_row="$row"
            continue
        fi
        retry_rows+=("$row")
    done
    if [ "${#retry_rows[@]}" -gt 0 ]; then
        run_pool "$AGENT_RETRY_CONCURRENCY" "${retry_rows[@]}" || overall=1
    fi
    if [ -n "$retry_082_row" ]; then
        run_pool 1 "$retry_082_row" || overall=1
    fi
done
unset ATTEMPT_SUFFIX

aggregated=1
python3 "$HERE/aggregate_agent.py" "${aggregate_args[@]}" || overall=1

exit "$overall"
