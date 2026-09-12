#!/usr/bin/env bash
# Per-task runtime knobs, sourced by worker_lib.sh (every relay-driving worker
# enforces them) and by run_agent_parallel.sh (which budgets fleet lifetime from
# them). One place for
# the defaults, so the admission gate can never budget with a value the worker
# no longer uses.
AGENT_TASK_TIMEOUT_SECONDS="${AGENT_TASK_TIMEOUT_SECONDS:-14400}"
PROCESS_TERMINATION_GRACE_SECONDS="${PROCESS_TERMINATION_GRACE_SECONDS:-10}"
RELAY_STOP_REQUEST_TIMEOUT_SECONDS="${RELAY_STOP_REQUEST_TIMEOUT_SECONDS:-10}"
RELAY_READY_TIMEOUT_SECONDS="${RELAY_READY_TIMEOUT_SECONDS:-1050}"
AGENT_WATCHDOG_POLL_SECONDS="${AGENT_WATCHDOG_POLL_SECONDS:-5}"
for worker_env_name in AGENT_TASK_TIMEOUT_SECONDS PROCESS_TERMINATION_GRACE_SECONDS \
    RELAY_STOP_REQUEST_TIMEOUT_SECONDS RELAY_READY_TIMEOUT_SECONDS AGENT_WATCHDOG_POLL_SECONDS; do
    if [[ ! "${!worker_env_name}" =~ ^[1-9][0-9]*$ ]]; then
        echo "$worker_env_name must be a positive integer" >&2
        exit 2
    fi
done
unset worker_env_name
export AGENT_TASK_TIMEOUT_SECONDS PROCESS_TERMINATION_GRACE_SECONDS \
    RELAY_STOP_REQUEST_TIMEOUT_SECONDS RELAY_READY_TIMEOUT_SECONDS AGENT_WATCHDOG_POLL_SECONDS
