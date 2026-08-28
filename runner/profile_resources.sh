#!/usr/bin/env bash
# Verification ladder rung 7 -- resource profiling.
#
# Publish the real CPU / RAM / DISK a run's workload used on E2B. This is a thin
# invocation of the *portable, standalone* profiler at repo-root
# `tools/profile_e2b_resources.py`: all profiling logic lives there, keyed only
# on the E2B sandbox-metrics REST API, so nothing is embedded in the vended
# guest/provider/relay. This wrapper just points that tool at this bench's
# evidence directory.
#
# It reads the sandbox ids the earlier rungs already recorded in their evidence
# JSON (`sandbox_id` / nested `sandbox.id`) and queries E2B for each. E2B keeps
# metrics for a killed sandbox for a retention window, so run this SOON after the
# ladder's sandbox-creating rungs (smoke, validate, snapshot, no-agent, agent).
#
# Non-billable: querying sandbox metrics creates no sandboxes and starts no work.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"            # repo rootREPO_ROOT="$V2ROOT"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/out/osworld-v2-evidence}"
OUT="${RESOURCE_REPORT:-$EVIDENCE_DIR/resource-requirements.json}"
TOOL="$REPO_ROOT/tools/profile_e2b_resources.py"
# e2b>=2.37 reports disk metrics (older builds warn and omit disk). Pin to the
# same major the repo targets, independent of validate.sh's runtime pin.
UV="uv run --python 3.12 --with e2b>=2.37,<2.38"

if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

if [ ! -d "$EVIDENCE_DIR" ]; then
    echo "evidence dir not found: $EVIDENCE_DIR (run the sandbox-creating rungs first)" >&2
    exit 2
fi

echo "profiling E2B resource use from evidence in: $EVIDENCE_DIR"
$UV python "$TOOL" --from-evidence "$EVIDENCE_DIR" --out "$OUT"
status=$?

# Exit non-zero (from the tool) if any recorded sandbox yielded no metrics or a
# saturation flag tripped, so this rung gates like the others. Pass
# ALLOW_INCOMPLETE=1 to downgrade that to a warning (e.g. some sandboxes aged out
# of the retention window on a late profiling pass).
if [ "$status" -ne 0 ] && [ "${ALLOW_INCOMPLETE:-0}" = "1" ]; then
    echo "resource profiling incomplete (ALLOW_INCOMPLETE=1): treating as warning" >&2
    exit 0
fi
exit "$status"
