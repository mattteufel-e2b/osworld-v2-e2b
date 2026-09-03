#!/usr/bin/env bash
# Self-contained OSWorld-V2-on-E2B setup: clone OSWorld-V2 at the validated pin
# (examples/osworld-v2/upstream.lock.json) and wire in the E2B provider
# (provider/provider.py + provider/manager.py) so OSWorld-V2's own run.py works
# with --provider_name e2b. Idempotent: re-running is safe and each string patch
# is grep-guarded, reporting "already applied" on the second run.
#
# Checkout states (the checkout is gitignored, so its state lives on disk only):
#   * "applied"  — setup.sh's patches present (register+classify e2b provider,
#                  strict reset, vendored provider/manager + e2b_relay.py). This
#                  is the NORMAL operating state between runs; the harness, relay,
#                  and Task 12 all require it. Re-running setup.sh with no flag
#                  restores it idempotently.
#   * "pristine" — patch footprint reverted, matching the upstream pin exactly.
#                  Reach it with `setup.sh --restore` (for pin verification); it
#                  reverts only setup.sh's own files and leaves .venv/other
#                  untracked working files intact.
#
# Usage:  ./setup.sh [dest-dir]            apply patches (default state)
#         ./setup.sh --restore [dest-dir]  revert to pristine, then exit
#         ./setup.sh --preflight           validate local inputs and pin, then exit
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"            # repo root
RELAY_DIR="$V2ROOT/relay"
PROVIDER_DIR="$V2ROOT/provider"
POLICY_FILE="$V2ROOT/e2b_policy.py"
LOCKFILE="$V2ROOT/examples/osworld-v2/upstream.lock.json"

# Pin: xlang-ai/OSWorld-V2 @ upstream.lock.json code.commit.
PIN=d578d2d4e0dc82b43e270fdaa7fa89d9708cd154

RESTORE=0
PREFLIGHT=0
if [ "${1:-}" = "--restore" ]; then
    RESTORE=1
    shift
elif [ "${1:-}" = "--preflight" ]; then
    PREFLIGHT=1
    shift
fi
DEST="${1:-$PWD/OSWorld-V2}"

# --restore returns the checkout to pristine (pin-verification state) by reverting
# ONLY setup.sh's patch footprint: the two patched tracked files, plus the two
# vendored untracked paths. .venv and any other untracked working files are left
# untouched (clean is scoped, never a bare `git clean -fd`).
if [ "$RESTORE" -eq 1 ]; then
    if [ ! -d "$DEST/.git" ]; then
        echo "ERROR: --restore: no OSWorld-V2 checkout at $DEST" >&2
        exit 1
    fi
    git -C "$DEST" checkout -- desktop_env/desktop_env.py desktop_env/providers/__init__.py lib_run_single.py 2>/dev/null || true
    git -C "$DEST" clean -fdq desktop_env/providers/e2b e2b_relay.py e2b_policy.py 2>/dev/null || true
    echo "restored pristine: $DEST (setup.sh patch footprint reverted)"
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
LOCK_PIN="$(python3 - "$LOCKFILE" <<'EOF'
import json, sys
print(json.load(open(sys.argv[1]))["code"]["commit"])
EOF
)"
if [ "$LOCK_PIN" != "$PIN" ]; then
    echo "ERROR: setup.sh PIN ($PIN) != upstream.lock.json commit ($LOCK_PIN)" >&2
    exit 1
fi

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

if [ ! -d "$DEST/.git" ]; then
    git clone https://github.com/xlang-ai/OSWorld-V2 "$DEST"
fi
git -C "$DEST" fetch --quiet origin "$PIN" 2>/dev/null || true
git -C "$DEST" checkout --quiet "$PIN"
echo "OSWorld-V2 at $DEST (pin $PIN)"

# ---- provider package ----------------------------------------------------
mkdir -p "$DEST/desktop_env/providers/e2b"
cp "$PROVIDER_DIR/provider.py" "$PROVIDER_DIR/manager.py" "$DEST/desktop_env/providers/e2b/"
touch "$DEST/desktop_env/providers/e2b/__init__.py"

# ---- relay (runs on the host next to run.py) ------------------------------
cp "$RELAY_DIR/relay.py" "$DEST/e2b_relay.py"
cp "$POLICY_FILE" "$DEST/e2b_policy.py"

# ---- string patches: register + classify the provider, force strict reset --
python3 - "$DEST" <<'EOF'
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

# (d) preserve terminal actions whose final observation has no screenshot ---
# V2's controller returns screenshot=None for DONE/FAIL. The upstream runner
# tried to write that value as bytes before evaluating, invalidating every task
# that terminated normally. Record no screenshot for that terminal row and
# continue into env.evaluate(); a non-terminal None remains a hard error.
single = dest / "lib_run_single.py"
src = single.read_text()
terminal_marker = 'terminal observation returned screenshot=None; evaluating final state'
if terminal_marker in src:
    print("(d) lib_run_single.py terminal screenshot handling: already applied")
else:
    old = '''                with open(os.path.join(example_result_dir, f"step_{step_idx + 1}_{action_timestamp}.png"),
                        "wb") as _f:
                    _f.write(obs['screenshot'])
                with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:'''
    new = '''                screenshot_file = None
                screenshot_bytes = obs.get("screenshot")
                if screenshot_bytes is not None:
                    screenshot_file = f"step_{step_idx + 1}_{action_timestamp}.png"
                    with open(os.path.join(example_result_dir, screenshot_file), "wb") as _f:
                        _f.write(screenshot_bytes)
                elif not done:
                    raise RuntimeError("non-terminal observation returned screenshot=None")
                else:
                    logger.info("terminal observation returned screenshot=None; evaluating final state")
                with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:'''
    count = src.count(old)
    assert count == 1, f"lib_run_single.py terminal screenshot anchor found {count}x, need exactly 1 (OSWorld-V2 moved?)"
    src = src.replace(old, new, 1)
    old_name = '"screenshot_file": f"step_{step_idx + 1}_{action_timestamp}.png"'
    count = src.count(old_name)
    assert count >= 1, "lib_run_single.py screenshot_file anchor missing (OSWorld-V2 moved?)"
    single.write_text(src.replace(old_name, '"screenshot_file": screenshot_file', 1))
    print("(d) lib_run_single.py: patched (terminal screenshot None is evaluable)")
EOF

echo
echo "Done. Next:"
echo "  1. uv pip install -r $DEST/requirements.txt -r $HERE/requirements-e2b.txt   # python >=3.12"
echo "  2. export E2B_API_KEY=... ; export GUEST_TEMPLATE=osworld-v2-gnome:<build_id>"
echo "  3. python $DEST/e2b_relay.py   # host-side relay on 127.0.0.1:14999"
