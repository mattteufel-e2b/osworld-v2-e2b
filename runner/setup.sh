#!/usr/bin/env bash
# Self-contained OSWorld-V2-on-E2B setup: clone OSWorld-V2 at the validated pin
# (examples/osworld-v2/upstream.lock.json) and wire in the E2B provider
# (provider/provider.py + provider/manager.py + provider/bridge.py, the
# in-process bridge) so OSWorld-V2's own run.py works with --provider_name e2b.
# Idempotent: re-running is safe and each string patch (a)-(n) is guarded,
# reporting "already applied" on the second run.
#
# Checkout states (the checkout is gitignored, so its state lives on disk only):
#   * "applied"  — setup.sh's patches present (register+classify e2b provider,
#                  strict reset, --provider_name e2b accepted by the M3 runner,
#                  vendored provider/manager/bridge + policy). This is the
#                  NORMAL operating state between runs and the state checked
#                  by `setup.sh --verify`. Re-running setup.sh with no flag
#                  restores it idempotently.
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
PATCHED_TRACKED=(
    desktop_env/desktop_env.py
    desktop_env/providers/__init__.py
    scripts/python/run_multienv_m3.py
    mm_agents/m3/parser.py
    mm_agents/m3/agent.py
    mm_agents/anthropic/main.py
    desktop_env/controllers/python.py
    desktop_env/controllers/website.py
)
VENDORED=(
    "desktop_env/providers/e2b/provider.py:$PROVIDER_DIR/provider.py"
    "desktop_env/providers/e2b/manager.py:$PROVIDER_DIR/manager.py"
    "desktop_env/providers/e2b/bridge.py:$PROVIDER_DIR/bridge.py"
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
    "$PROVIDER_DIR/provider.py" \
    "$PROVIDER_DIR/manager.py" \
    "$PROVIDER_DIR/bridge.py" \
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

# (d) let upstream's M3 multi-env runner accept --provider_name e2b ---------
m3 = dest / "scripts/python/run_multienv_m3.py"
if m3.exists():
    src = m3.read_text()
    m3_anchor = 'choices=["aws", "virtualbox", "vmware", "docker", "azure"]'
    m3_patched = 'choices=["aws", "virtualbox", "vmware", "docker", "azure", "e2b"]'
    if m3_patched in src:
        print("(d) run_multienv_m3.py: already applied")
    else:
        count = src.count(m3_anchor)
        assert count == 1, f"run_multienv_m3.py provider choices found {count}x, need exactly 1 (OSWorld-V2 moved?)"
        m3.write_text(src.replace(m3_anchor, m3_patched, 1))
        print("(d) run_multienv_m3.py: patched (accepts --provider_name e2b)")

# (e) M3 parser: `super` is the X11 Super key ("win"), not macOS "command" ----
# The active `key` branch shadows the module's own _NORMALIZE_KEY table; on
# Linux PyAutoGUI has no "command" key and silently drops the press (sample
# 2026-09-15, task 103). Narrow: one entry, neighbours unchanged.
parser = dest / "mm_agents/m3/parser.py"
if parser.exists():
    src = parser.read_text()
    key_anchor = '                "super_l": "win",\n                "super": "command",\n'
    key_patched = '                "super_l": "win",\n                "super": "win",\n'
    if key_patched in src:
        print("(e) m3/parser.py super key: already applied")
    else:
        count = src.count(key_anchor)
        assert count == 1, f"m3/parser.py key_conversion super entry found {count}x, need exactly 1 (OSWorld-V2 moved?)"
        parser.write_text(src.replace(key_anchor, key_patched, 1))
        print("(e) m3/parser.py: patched (super -> win on Linux)")

# (f) M3 parser: the [INFEASIBLE] terminal marker counts only outside the
# model's thinking block. M3Agent._call_llm prepends thinking as
# <mm:think>...</mm:think>; a marker mentioned while reasoning overrode an
# actual tool call in the same response (sample 2026-09-15, task 067).
if parser.exists():
    src = parser.read_text()
    inf_anchor = '    if "[INFEASIBLE]" in response:\n        return "[INFEASIBLE]", ["FAIL"]\n'
    inf_patched = (
        '    if "[INFEASIBLE]" in _M3_THINK_BLOCK.sub("", response):\n'
        '        return "[INFEASIBLE]", ["FAIL"]\n'
    )
    if inf_patched in src:
        print("(f) m3/parser.py infeasible marker: already applied")
    else:
        count = src.count(inf_anchor)
        assert count == 1, f"m3/parser.py [INFEASIBLE] check found {count}x, need exactly 1 (OSWorld-V2 moved?)"
        src = src.replace(inf_anchor, inf_patched, 1)
        # Module constant next to the existing `import re` (unique line).
        import_anchor = "import re\n"
        count = src.count(import_anchor)
        assert count == 1, f"m3/parser.py `import re` found {count}x, need exactly 1"
        src = src.replace(import_anchor, import_anchor + '_M3_THINK_BLOCK = re.compile(r"<mm:think>.*?</mm:think>", re.S)\n', 1)
        parser.write_text(src)
        print("(f) m3/parser.py: patched ([INFEASIBLE] ignored inside <mm:think>)")

# (g) controller: wait for the guest's verdict on an agent action -----------
# The guest kills an action at its own 120 s deadline and answers 500 with
# subprocess's TimeoutExpired text. Upstream's 90 s client timeout returned None
# before that verdict arrived, so typing continued for up to 30 s under the next
# action (sample 2026-09-15, tasks 093/059/079/082). Only the /execute action
# path changes; setup and script timeouts are untouched.
controller = dest / "desktop_env/controllers/python.py"
if controller.exists():
    src = controller.read_text()
    deadline_anchor = "data=payload, timeout=90)"
    deadline_patched = "data=payload, timeout=130)"
    if deadline_patched in src:
        print("(g) controllers/python.py action deadline: already applied")
    else:
        count = src.count(deadline_anchor)
        assert count == 1, f"controllers/python.py execute_python_command timeout found {count}x, need exactly 1 (OSWorld-V2 moved?)"
        controller.write_text(src.replace(deadline_anchor, deadline_patched, 1))
        print("(g) controllers/python.py: patched (action deadline 130 s covers the guest's 120 s kill)")

# (h) controller: the guest's own timeout verdict is final, never retried ----
# With (g) the client waits 130 s, so the guest's 500 for a 120 s kill now
# reaches the client instead of a client-side ReadTimeout arriving first.
# Retrying would replay a partially applied action, so `break` falls through to
# upstream's own `return None` -- the same outcome upstream produces on a
# client-side ReadTimeout, with no replay. Evaluator code paths are unchanged:
# callers still see None, never a 200 carrying empty output.
if controller.exists():
    src = controller.read_text()
    retry_anchor = (
        "                else:\n"
        '                    logger.error("Failed to execute command. Status code: %d", response.status_code)\n'
        '                    logger.info("Retrying to execute command.")\n'
    )
    retry_patched = (
        "                else:\n"
        '                    logger.error("Failed to execute command. Status code: %d", response.status_code)\n'
        '                    if response.status_code == 500 and "timed out after" in response.text:\n'
        "                        # The guest killed the action at its own deadline (upstream's\n"
        "                        # TimeoutExpired message). Retrying would replay a partially\n"
        "                        # applied action, so give up exactly as a client timeout does.\n"
        "                        break\n"
        '                    logger.info("Retrying to execute command.")\n'
    )
    if retry_patched in src:
        print("(h) controllers/python.py guest-timeout retry: already applied")
    else:
        count = src.count(retry_anchor)
        assert count == 1, f"controllers/python.py execute_python_command non-200 branch found {count}x, need exactly 1 (OSWorld-V2 moved?)"
        controller.write_text(src.replace(retry_anchor, retry_patched, 1))
        print("(h) controllers/python.py: patched (no retry after the guest's own timeout)")

# (i)-(k) Inference-run contracts: failures propagate, fleet HTTPS is explicit,
# and one text action incurs one PyAutoGUI pause instead of one per character.
def replace_once(relative, before, after, label):
    path = dest / relative
    if not path.exists():
        return
    src = path.read_text()
    if after in src:
        print(f"{label}: already applied")
        return
    assert src.count(before) == 1, f"{label}: expected one anchor (OSWorld-V2 moved?)"
    path.write_text(src.replace(before, after, 1))
    print(f"{label}: patched")

replace_once(
    "mm_agents/m3/agent.py",
    '                raw_response = {"error": str(e)}\n'
    '                if attempt < self.max_llm_retries:\n'
    '                    continue\n'
    '                break\n',
    '                raw_response = {"error": str(e)}\n'
    '                if attempt < self.max_llm_retries:\n'
    '                    continue\n'
    '                self._save_api_log(request_body, raw_response, retry_attempts=retry_attempts)\n'
    '                raise\n',
    "(i) M3 exhausted transport errors propagate",
)
replace_once(
    "desktop_env/controllers/website.py",
    '    https_url = f"https://{host}"\n',
    '    if os.environ.get("OSWORLD_WEBSITE_SCHEME") == "https":\n'
    '        return "https://"\n'
    '    https_url = f"https://{host}"\n',
    "(j) campaign fleet scheme",
)
if parser.exists():
    src = parser.read_text()
    typed = '            code.append(f"pyautogui.write({text!r}, interval=0.001)")\n'
    if typed not in src:
        start = '            for char in text:\n'
        end = '    elif action == "scroll":\n'
        assert src.count(start) == src.count(end) == 1, "(k) M3 typing anchors moved"
        before = src[src.index(start):src.index(end)]
        replace_once("mm_agents/m3/parser.py", before, typed, "(k) M3 typing")

replace_once(
    "mm_agents/m3/agent.py",
    '            if not pyautogui_code and response_text.strip():\n',
    '            if not pyautogui_code:\n',
    "(l) M3 empty responses use the configured retries",
)
replace_once(
    "mm_agents/m3/agent.py",
    'out of retries, no-op',
    'out of retries, failing',
    "(l) M3 parse-failure diagnostic",
)
replace_once(
    "mm_agents/m3/agent.py",
    '        if pyautogui_code == ["CALL_USER"]:\n',
    '        if not pyautogui_code:\n'
    '            self._save_api_log(request_body, raw_response, retry_attempts=retry_attempts or None)\n'
    '            raise ValueError("M3 response contained no executable action")\n'
    '\n'
    '        if pyautogui_code == ["CALL_USER"]:\n',
    "(l) M3 empty or malformed actions fail closed",
)

replace_once(
    "mm_agents/m3/agent.py",
    '            body["thinking"] = {"type": "adaptive"}\n'
    '            if self.thinking_budget:\n'
    '                body["thinking"]["budget_tokens"] = self.thinking_budget\n'
    '        elif self.thinking_budget:\n',
    '            body["thinking"] = {"type": "adaptive"}\n'
    '        elif self.thinking_budget:\n',
    "(m) adaptive thinking omits unsupported fixed budget",
)

replace_once(
    "mm_agents/anthropic/main.py",
    '            betas.append(PROMPT_CACHING_BETA_FLAG)\n',
    '            # Prompt caching is generally available; keep cache_control without the obsolete beta.\n',
    "(n) Claude prompt caching omits obsolete beta header",
)

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

# ---- provider package (provider + manager + in-process bridge) -------------
mkdir -p "$DEST/desktop_env/providers/e2b"
cp "$PROVIDER_DIR/provider.py" "$PROVIDER_DIR/manager.py" "$PROVIDER_DIR/bridge.py" \
    "$DEST/desktop_env/providers/e2b/"
touch "$DEST/desktop_env/providers/e2b/__init__.py"
# The bridge imports e2b_policy from the checkout root (run.py's cwd).
cp "$POLICY_FILE" "$DEST/e2b_policy.py"

# ---- string patches (a)-(n): register + classify the provider, force strict
#      reset, accept --provider_name e2b in the M3 multi-env runner, and the
#      disclosed agent transport / parser / controller execution patches ----
apply_adapter_patches "$DEST"

echo
echo "Done. Follow the Quick start in $V2ROOT/README.md to run the upstream benchmark."
