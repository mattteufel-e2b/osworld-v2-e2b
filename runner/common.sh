#!/usr/bin/env bash
# Shared, lifecycle-free helpers for the benchmark coordinator and the
# maintainer validation scripts: path resolution, the checkout `uv run`
# command, and the fail-closed environment gates. Sourcing it starts no process.

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$COMMON_DIR/.." && pwd)"
REPO_ROOT="$V2ROOT"
RUNNER_DIR="$COMMON_DIR"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
# Rollouts run in the pinned checkout's own project env (cwd is OSWORLD_ROOT)
# with the e2b SDK and aiohttp layered on top for the in-process bridge.
WORKER_UV=(uv run --locked --extra full --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1)

abspath() {
    python3 - "$1" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
}

require_immutable_guest_template() {
    if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9_-]+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
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

# Fleet + asset wiring consumed by the bridge (in-guest Host-mapping proxy) and
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
