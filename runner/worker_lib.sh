#!/usr/bin/env bash
# Shared worker lifecycle for every script that drives one OSWorld-V2 rollout
# through its own namespaced E2B relay: runner/run_agent.sh (the benchmark path)
# and the maintainer validation wrappers. Source it after setting HERE; it
# defines functions and a few exported variables and runs nothing on its own.
#
# What lives here, once:
#   * repo/checkout/task/service path resolution and the worker `uv run` command,
#   * the immutable GUEST_TEMPLATE, OSWORLD_CAMPAIGN_ID and E2B_API_KEY gates,
#   * fleet + asset wiring exported to the relay and to the rollout process,
#   * relay start with bounded readiness, and relay stop,
#   * process-group start/probe/terminate so a timeout or signal never leaves an
#     agent, harness or relay helper behind.
# Every wrapper keeps only what differs: its arguments, what it launches, and
# what it writes when the launch ends.

WORKER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$WORKER_LIB_DIR/.." && pwd)"
REPO_ROOT="$V2ROOT"
RUNNER_DIR="$WORKER_LIB_DIR"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
# Relay and rollout run in the pinned checkout's own project env (cwd is
# OSWORLD_ROOT), with the e2b SDK and aiohttp layered on top.
WORKER_UV=(uv run --locked --extra full --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1)
source "$WORKER_LIB_DIR/worker_env.sh"

abspath() {
    python3 - "$1" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
}

require_immutable_guest_template() {
    if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9-]+:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
        echo "GUEST_TEMPLATE must be an immutable name:build_id reference (got: '${GUEST_TEMPLATE:-<unset>}')" >&2
        exit 2
    fi
    export GUEST_TEMPLATE
}

require_campaign_id() {
    if [ -z "${OSWORLD_CAMPAIGN_ID:-}" ]; then echo "OSWORLD_CAMPAIGN_ID is required" >&2; exit 2; fi
    export OSWORLD_CAMPAIGN_ID
}

resolve_e2b_api_key() {
    if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
        export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
    fi
    if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi
}

# Namespace this worker's relay on a disjoint loopback port block.
namespace_relay() {
    local port_base="$1"
    export OSWORLD_RELAY_PORT_BASE="$port_base"
    CONTROL_PORT=$((14999 + port_base))
    export E2B_RELAY_CONTROL_URL="http://127.0.0.1:${CONTROL_PORT}"
}

# Fleet + asset wiring consumed by the relay (in-guest Host-mapping proxy) and
# by the rollout (website suffix, GitLab, gated assets). Reads the runtime file
# the service launchers wrote; preflight has already required it.
export_fleet_wiring() {
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
}

# ---- process groups ---------------------------------------------------------
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

# A child launched by start_in_new_session shares OUR process group until its
# python reaches os.setsid(), so a group-only probe or kill can miss it and a
# bare `wait` would then block forever. Probe and signal the group first, then
# the pid itself; a zombie (exited, not yet reaped) counts as gone.
process_alive() {
    local state
    state="$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')"
    [ -n "$state" ] && [ "${state#Z}" = "$state" ]
}

signal_process() {
    kill "-$1" -- "-$2" 2>/dev/null || true
    kill "-$1" "$2" 2>/dev/null || true
}

wait_for_exit() {
    local process="$1"
    local timeout_seconds="$2"
    local deadline=$((SECONDS + timeout_seconds))
    while process_alive "$process" && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 1
    done
    ! process_alive "$process"
}

terminate_process_group() {
    local process_group="$1"
    if process_alive "$process_group"; then
        signal_process TERM "$process_group"
        if ! wait_for_exit "$process_group" "$PROCESS_TERMINATION_GRACE_SECONDS"; then
            signal_process KILL "$process_group"
            wait_for_exit "$process_group" "$PROCESS_TERMINATION_GRACE_SECONDS" || true
        fi
    fi
    wait "$process_group" 2>/dev/null || true
}

# ---- relay -------------------------------------------------------------------
relay_pid=""
rollout_pid=""

# start_relay LOG: launch e2b_relay.py in its own session (creating this
# worker's guest sandbox) and wait for its control port to answer /health.
# Sets relay_pid; returns 1 (with the log tail on stderr) if it never came up.
start_relay() {
    local relay_log="$1"
    start_in_new_session "$OSWORLD_ROOT" "${WORKER_UV[@]}" python e2b_relay.py 2>"$relay_log" &
    relay_pid=$!
    local deadline=$((SECONDS + RELAY_READY_TIMEOUT_SECONDS))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:${CONTROL_PORT}/health" >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "$relay_pid" 2>/dev/null; then break; fi
        sleep 2
    done
    echo "relay did not become ready on control port ${CONTROL_PORT}" >&2
    tail -40 "$relay_log" >&2
    return 1
}

# Ask the relay to stop (it tears its guest sandbox down), then make sure the
# process group is gone.
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
    if ! wait_for_exit "$process_group" "$PROCESS_TERMINATION_GRACE_SECONDS"; then
        terminate_process_group "$process_group"
    else
        wait "$process_group" 2>/dev/null || true
    fi
}

# ---- worker teardown ---------------------------------------------------------
# Terminate the rollout's process group first, then the relay. Once teardown
# starts it is bounded; a second signal must not interrupt it between recording
# a process group and terminating that whole group.
cleanup_started=0
worker_cleanup() {
    if [ "$cleanup_started" -eq 1 ]; then
        return
    fi
    cleanup_started=1
    trap '' INT TERM
    if [ -n "$rollout_pid" ]; then
        local process_group="$rollout_pid"
        terminate_process_group "$process_group"
        rollout_pid=""
    fi
    shutdown_relay
}

handle_worker_signal() {
    local exit_code="$1"
    worker_cleanup
    exit "$exit_code"
}

install_worker_traps() {
    trap worker_cleanup EXIT
    trap 'handle_worker_signal 130' INT
    trap 'handle_worker_signal 143' TERM
}

# wait_for_rollout TIMEOUT_SECONDS: wait for rollout_pid, polling every
# AGENT_WATCHDOG_POLL_SECONDS. Returns the rollout's exit status, or 124 after
# terminating its process group at the deadline. A leader that exits cleanly
# must not leave helpers running in its session either.
wait_for_rollout() {
    local timeout_seconds="$1"
    local started_at=$SECONDS
    local process_group="$rollout_pid"
    while kill -0 "$rollout_pid" 2>/dev/null; do
        if [ $((SECONDS - started_at)) -ge "$timeout_seconds" ]; then
            echo "rollout exceeded ${timeout_seconds}s deadline" >&2
            terminate_process_group "$process_group"
            rollout_pid=""
            return 124
        fi
        sleep "$AGENT_WATCHDOG_POLL_SECONDS"
    done
    wait "$rollout_pid"
    local status=$?
    if process_alive "$process_group"; then
        terminate_process_group "$process_group"
    fi
    rollout_pid=""
    return "$status"
}
