#!/usr/bin/env bash
# Self-contained OSWorld-V2-on-E2B setup: clone OSWorld-V2 at the validated pin
# (examples/osworld-v2/upstream.lock.json) and wire in the E2B provider
# (provider/provider.py + provider/manager.py) so OSWorld-V2's own run.py works
# with --provider_name e2b. Idempotent: re-running is safe and each string patch
# is grep-guarded, reporting "already applied" on the second run.
#
# Checkout states (the checkout is gitignored, so its state lives on disk only):
#   * "applied"  — setup.sh's patches present (register+classify e2b provider,
#                  strict reset, vendored provider/manager + relay/policy). This
#                  is the NORMAL operating state between runs and the state
#                  checked by `setup.sh --verify`. Re-running setup.sh with no
#                  flag restores it idempotently.
#   * "pristine" — the upstream pin with no tracked edits. Reach it with
#                  `setup.sh --restore`: it reverts every tracked edit (setup.sh's
#                  own patches and any drift --verify would reject, e.g. a
#                  regenerated uv.lock) and removes only the vendored files;
#                  .venv and other untracked working files are left intact.
#
# Usage:  ./setup.sh [dest-dir]            apply patches (default state)
#         ./setup.sh --restore [dest-dir]  revert to pristine, then exit
#         ./setup.sh --verify [dest-dir]   verify exact patched state, then exit
#         ./setup.sh --preflight           validate local inputs and pin, then exit
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"            # repo root
RELAY_DIR="$V2ROOT/relay"
PROVIDER_DIR="$V2ROOT/provider"
POLICY_FILE="$V2ROOT/e2b_policy.py"
LOCKFILE="$V2ROOT/examples/osworld-v2/upstream.lock.json"

RESTORE=0
VERIFY=0
PREFLIGHT=0
if [ "${1:-}" = "--restore" ]; then
    RESTORE=1
    shift
elif [ "${1:-}" = "--verify" ]; then
    VERIFY=1
    shift
elif [ "${1:-}" = "--preflight" ]; then
    PREFLIGHT=1
    shift
fi
DEST="${1:-$PWD/OSWorld-V2}"

# setup.sh's footprint in the checkout, declared once: the tracked files it
# patches in place and the local files it vendors in. --verify checks exactly
# this set; --restore reverts it. Any --verify failure prints REPAIR_HINT.
PATCHED_TRACKED=(desktop_env/desktop_env.py desktop_env/providers/__init__.py)
VENDORED=(
    "desktop_env/providers/e2b/provider.py:$PROVIDER_DIR/provider.py"
    "desktop_env/providers/e2b/manager.py:$PROVIDER_DIR/manager.py"
    "e2b_relay.py:$RELAY_DIR/relay.py"
    "e2b_policy.py:$POLICY_FILE"
)
REPAIR_HINT="repair with: runner/setup.sh --restore $DEST && runner/setup.sh $DEST"

if [ "$RESTORE" -eq 1 ]; then
    if [ ! -d "$DEST/.git" ]; then
        echo "ERROR: --restore: no OSWorld-V2 checkout at $DEST" >&2
        exit 1
    fi
    git -C "$DEST" checkout --quiet -- .
    git -C "$DEST" clean -fdq desktop_env/providers/e2b "${VENDORED[@]%%:*}"
    echo "restored pristine: $DEST (tracked edits reverted, vendored files removed)"
    git -C "$DEST" status --porcelain
    exit 0
fi

# Assert our hardcoded pin still matches the lock file so a lockfile bump can
# never silently diverge from what setup.sh checks out. A missing lockfile is a
# hard error too: skipping the check would silently disarm the guard (e.g. after
# a rename), which is exactly the divergence it exists to prevent.
if [ ! -f "$LOCKFILE" ]; then
    echo "ERROR: lock file not found at $LOCKFILE (moved/renamed? update setup.sh)" >&2
    exit 1
fi
python3 "$V2ROOT/services/release_lock.py" "$LOCKFILE"
PIN="$(python3 - "$LOCKFILE" <<'EOF'
import json, sys
print(json.load(open(sys.argv[1]))["code"]["commit"])
EOF
)"

for required_file in \
    "$RELAY_DIR/relay.py" \
    "$PROVIDER_DIR/provider.py" \
    "$PROVIDER_DIR/manager.py" \
    "$POLICY_FILE" \
    "$HERE/requirements-e2b.txt"
do
    if [ ! -f "$required_file" ]; then
        echo "ERROR: required local file not found at $required_file" >&2
        exit 1
    fi
done

if [ "$PREFLIGHT" -eq 1 ]; then
    echo "preflight ok: local inputs present; OSWorld-V2 pin $PIN"
    exit 0
fi

apply_adapter_patches() {
python3 - "$1" <<'EOF'
import sys, pathlib

dest = pathlib.Path(sys.argv[1])

# (a) register the e2b provider in OSWorld-V2's factory ---------------------
factory = dest / "desktop_env/providers/__init__.py"
src = factory.read_text()
if 'provider_name == "e2b"' in src:
    print("(a) providers/__init__.py: already applied")
else:
    branch = (
        '    elif provider_name == "e2b":\n'
        "        from desktop_env.providers.e2b.manager import E2BVMManager\n"
        "        from desktop_env.providers.e2b.provider import E2BProvider\n"
        "        return E2BVMManager(), E2BProvider(region)\n"
    )
    anchor = '    else:\n        raise NotImplementedError(f"{provider_name} not implemented!")'
    count = src.count(anchor)
    assert count == 1, f"providers/__init__.py factory anchor found {count}x, need exactly 1 (OSWorld-V2 moved?)"
    factory.write_text(src.replace(anchor, branch + anchor, 1))
    print("(a) providers/__init__.py: patched (registered e2b provider)")

# (b) classify e2b as a clean-start (cloud) provider -----------------------
# V2's DesktopEnv raises ValueError for any provider_name not in one of these
# two sets, so e2b must be listed. It belongs with the clean-start providers
# (a fresh sandbox each reset), so is_environment_used starts False like docker/aws.
denv = dest / "desktop_env/desktop_env.py"
src = denv.read_text()
clean_anchor = 'if self.provider_name in {"docker", "aws", "gcp", "azure", "aliyun", "volcengine"}:'
clean_patched = 'if self.provider_name in {"docker", "aws", "gcp", "azure", "aliyun", "volcengine", "e2b"}:'
if clean_patched in src:
    print("(b) desktop_env.py provider classification: already applied")
else:
    count = src.count(clean_anchor)
    assert count == 1, f"desktop_env.py provider-classification set found {count}x, need exactly 1 (OSWorld-V2 moved?)"
    src = src.replace(clean_anchor, clean_patched, 1)
    denv.write_text(src)
    print('(b) desktop_env.py: patched (classified e2b as clean-start provider)')

# (c) strict reset: fresh sandbox on every e2b reset -----------------------
# OSWorld normally skips revert when it believes the environment is unused. For
# E2B, reset on every task and every setup retry so even a partial/failed setup
# cannot leak state into the next attempt.
src = denv.read_text()
reset_anchor = 'if self.is_environment_used:'
reset_patched = 'if self.is_environment_used or self.provider_name == "e2b":'
if reset_patched in src:
    print("(c) desktop_env.py strict reset: already applied")
else:
    # `if self.is_environment_used:` is a generic guard; replacing "the first
    # occurrence" is only correct while it is also the ONLY occurrence, so a
    # pin bump that introduces a second one must fail here, not mispatch.
    count = src.count(reset_anchor)
    assert count == 1, f"desktop_env.py reset condition found {count}x, need exactly 1 (OSWorld-V2 moved?)"
    denv.write_text(src.replace(reset_anchor, reset_patched, 1))
    print('(c) desktop_env.py: patched (strict fresh sandbox for e2b resets)')

EOF
}

if [ "$VERIFY" -eq 1 ]; then
    if [ ! -d "$DEST/.git" ]; then
        echo "ERROR: --verify: no OSWorld-V2 checkout at $DEST" >&2
        exit 1
    fi
    ACTUAL_HEAD="$(git -C "$DEST" rev-parse HEAD)"
    if [ "$ACTUAL_HEAD" != "$PIN" ]; then
        echo "ERROR: OSWorld-V2 HEAD $ACTUAL_HEAD does not match pin $PIN; $REPAIR_HINT" >&2
        exit 1
    fi

    # Exactly the patched files may differ from the pin: this also rejects any
    # other tracked drift (a regenerated uv.lock, an edited evaluator backend).
    EXPECTED_TRACKED="$(printf '%s\n' "${PATCHED_TRACKED[@]}" | sort)"
    ACTUAL_TRACKED="$(git -C "$DEST" diff --name-only HEAD -- | sort)"
    if [ "$ACTUAL_TRACKED" != "$EXPECTED_TRACKED" ]; then
        echo "ERROR: OSWorld-V2 tracked edits do not match setup patch footprint; $REPAIR_HINT" >&2
        git -C "$DEST" status --short --untracked-files=no >&2
        exit 1
    fi

    EXPECTED_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/osworld-v2-verify.XXXXXX")"
    trap 'rm -rf "$EXPECTED_ROOT"' EXIT
    for relative in "${PATCHED_TRACKED[@]}"; do
        mkdir -p "$EXPECTED_ROOT/$(dirname "$relative")"
        git -C "$DEST" show "$PIN:$relative" > "$EXPECTED_ROOT/$relative"
    done
    apply_adapter_patches "$EXPECTED_ROOT" >/dev/null

    for relative in "${PATCHED_TRACKED[@]}"; do
        if ! cmp -s "$DEST/$relative" "$EXPECTED_ROOT/$relative"; then
            echo "ERROR: OSWorld-V2 checkout differs from expected: $relative; $REPAIR_HINT" >&2
            exit 1
        fi
    done
    for pair in "${VENDORED[@]}"; do
        if ! cmp -s "$DEST/${pair%%:*}" "${pair#*:}"; then
            echo "ERROR: OSWorld-V2 checkout differs from expected: ${pair%%:*}; $REPAIR_HINT" >&2
            exit 1
        fi
    done
    if [ ! -f "$DEST/desktop_env/providers/e2b/__init__.py" ] || [ -s "$DEST/desktop_env/providers/e2b/__init__.py" ]; then
        echo "ERROR: OSWorld-V2 checkout differs from expected: desktop_env/providers/e2b/__init__.py; $REPAIR_HINT" >&2
        exit 1
    fi
    echo "verified OSWorld-V2 checkout: $DEST (pin $PIN)"
    exit 0
fi

if [ ! -d "$DEST/.git" ]; then
    git clone https://github.com/xlang-ai/OSWorld-V2 "$DEST"
fi
git -C "$DEST" fetch --quiet origin "$PIN" 2>/dev/null || true
git -C "$DEST" checkout --quiet "$PIN"
echo "OSWorld-V2 at $DEST (pin $PIN)"

# Keep the task loop identical to upstream. Repair only the exact legacy E2B
# screenshot patch; never discard customer edits to agent/task execution.
python3 - "$DEST" "$PIN" <<'EOF'
import hashlib
import pathlib
import subprocess
import sys

dest = pathlib.Path(sys.argv[1])
single = dest / "lib_run_single.py"
pristine = subprocess.check_output(
    ["git", "-C", str(dest), "show", f"{sys.argv[2]}:lib_run_single.py"]
)
current = single.read_bytes()
if current != pristine:
    # SHA256 of the legacy patched file at upstream d578d2d (fixture in tests).
    legacy_sha256 = "68b441de17b8d73e48c9599381dca98a7ac74f3451d8d5567bf4ba794aeacb68"
    if hashlib.sha256(current).hexdigest() != legacy_sha256:
        sys.exit("ERROR: lib_run_single.py has unrecognized edits; preserve your changes "
                 "and restore this file to the upstream pin before running setup again")
    single.write_bytes(pristine)
    print("lib_run_single.py: restored upstream terminal observation handling")
EOF

# ---- provider package ----------------------------------------------------
mkdir -p "$DEST/desktop_env/providers/e2b"
cp "$PROVIDER_DIR/provider.py" "$PROVIDER_DIR/manager.py" "$DEST/desktop_env/providers/e2b/"
touch "$DEST/desktop_env/providers/e2b/__init__.py"

# ---- relay (runs on the host next to run.py) ------------------------------
cp "$RELAY_DIR/relay.py" "$DEST/e2b_relay.py"
cp "$POLICY_FILE" "$DEST/e2b_policy.py"

# ---- string patches: register + classify the provider, force strict reset --
apply_adapter_patches "$DEST"

echo
echo "Done. Follow the Quick start in $V2ROOT/README.md to run the upstream benchmark."
