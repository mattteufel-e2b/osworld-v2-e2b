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
# the service launchers wrote; preflight has already required it, including
# the `tls` section's campaign CA/leaf material.
#
# Read complete lines so certificate paths may contain whitespace.
export_fleet_wiring() {
    {
        IFS= read -r WEBSITE_HOST_SUFFIX
        IFS= read -r GITLAB_URL
        IFS= read -r OSWORLD_CA_CERT
        IFS= read -r OSWORLD_CA_BUNDLE
        IFS= read -r HOSTMAP_TLS_CERT
        IFS= read -r HOSTMAP_TLS_KEY
    } < <(python3 - "$SERVICES_DIR/.runtime.json" <<'PY'
import json, sys
rt = json.load(open(sys.argv[1]))
tls = rt["tls"]
print(rt["websites"]["public_host_suffix"], rt["gitlab"]["url"], tls["ca_cert"], tls["bundle"], tls["leaf_cert"], tls["leaf_key"], sep="\n")
PY
)
    export WEBSITE_HOST_SUFFIX GITLAB_URL OSWORLD_CA_CERT HOSTMAP_TLS_CERT HOSTMAP_TLS_KEY
    # Upstream's website scheme probe and python-gitlab use requests; the SDKs
    # use httpx. Both get certifi's roots plus the campaign CA via these two
    # standard library env vars, so public HTTPS keeps working alongside trust
    # for the campaign-signed fleet origins.
    REQUESTS_CA_BUNDLE="$OSWORLD_CA_BUNDLE"; SSL_CERT_FILE="$OSWORLD_CA_BUNDLE"
    export REQUESTS_CA_BUNDLE SSL_CERT_FILE
    # :8090 is TLS-only; a transient scheme-probe timeout must not select HTTP.
    export OSWORLD_WEBSITE_SCHEME=https
    export GITLAB_PRIVATE_TOKEN="$(cat "$SERVICES_DIR/.gitlab-token")"
    export OSWORLD_FILE_BASE_URL="$TASKS_DIR/assets"
    export HOSTMAP_PROXY_SCRIPT="$SERVICES_DIR/hostmap_proxy.py"
    export OSWORLD_FLEET_RULES="$SERVICES_DIR/.runtime.json"
}

# Poll the host-side hostmap proxy until it answers or the owning process
# dies, bounded by a small connect/read timeout per attempt so a hung proxy
# cannot stall the caller past ~30 iterations. Prints the log tail on failure.
wait_for_hostmap_proxy() {
    local pid="$1" cookie="$2" logfile="$3" _
    for _ in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then break; fi
        # --resolve makes curl present the site name as SNI and Host so the
        # leaf certificate's SAN matches; connecting to the bare IP would fail
        # certificate verification even though the socket still reaches 8090.
        if curl -fsS --connect-timeout 2 --max-time 5 --cacert "$OSWORLD_CA_CERT" \
            --resolve 'mailhub.127.0.0.1.nip.io:8090:127.0.0.1' \
            "https://mailhub.127.0.0.1.nip.io:8090/api/state?cookie=$cookie" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "hostmap proxy did not become ready; last log lines:" >&2
    tail -n 40 "$logfile" >&2 2>/dev/null
    return 1
}
