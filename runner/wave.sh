#!/usr/bin/env bash
# Fire the 9 non-canary manifest tasks as parallel REAL-agent rollouts, each on
# its own namespaced relay (disjoint PORT_BASE). Detaches every worker and waits
# for all to finish. Receipts + trajectories land under the gitignored raw dir.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
RAW="$REPO_ROOT/out/osworld-v2-raw/agent"
mkdir -p "$RAW"

export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
: "${GUEST_TEMPLATE:?GUEST_TEMPLATE must be an immutable name:build_id reference}"
# Literal task-service ports (3000/8000) cannot be namespaced per worker (task
# code dials them on vm_ip=127.0.0.1 at their literal numbers), so under a
# parallel wave one relay would silently serve every worker's dials from its own
# guest. Disable them wave-wide: a task that needs them must run solo.
export OSWORLD_TASK_SERVICE_PORTS=""

# task_id domain port_base. 003 re-runs at base 0 for a clean receipt (its canary
# pass proved the loop but predated the post-eval bookkeeping fix).
WORK=(
  "003 gimp 0"
  "030 vscode 100"
  "004 libreoffice_impress 200"
  "027 excel 300"
  "097 zotero 400"
  "103 freecad 500"
  "067 musescore 600"
  "026 gitlab 700"
  "069 chrome 800"
  "005 chrome 900"
)

pids=""
for row in "${WORK[@]}"; do
  set -- $row
  tid="$1"; dom="$2"; pb="$3"
  TASK_ID="$tid" DOMAIN="$dom" PORT_BASE="$pb" \
    nohup "$HERE/run_agent.sh" > "$RAW/wave_${tid}.log" 2>&1 &
  worker_pid=$!
  pids="$pids $worker_pid"
  echo "launched task $tid ($dom) port_base=$pb pid=$worker_pid"
  sleep 3   # stagger relay/sandbox creation slightly
done

echo "waiting on workers:$pids"
fail=0
for pid in $pids; do
  wait "$pid" || fail=1
done
echo "wave complete (fail=$fail)"
exit "$fail"
