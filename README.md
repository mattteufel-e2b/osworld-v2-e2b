# OSWorld 2.0 on E2B

Run the OSWorld 2.0 benchmark (`xlang-ai/OSWorld-V2`) on E2B sandboxes instead of
QEMU/VMware/AWS virtual machines: an Ubuntu 22.04 GNOME guest as an immutable E2B Template,
OSWorld's provider contract implemented at the sandbox boundary, a localhost relay that owns
all E2B credentials, and container-lane service fleets (mocked websites, GitLab) in their own
sandboxes. The pinned release is `examples/osworld-v2/upstream.lock.json`.

Two things are never committed here: the upstream checkout (`OSWorld-V2/`) and the gated task
data (`tasks/`) — OSWorld 2.0's datasets are gated upstream, so each consumer accepts the
gate and downloads them with their own credentials. The upstream guest server
(`xlang-ai/osworld-server`) publishes no license, so it is fetched at a pinned commit and
patched locally (`template/fetch_server.sh` + `patches/`), never redistributed. Run the fetch
script before template typechecking or building; it replaces the ignored generated payload with
a fresh copy of the pin and applies every committed patch in lexical order.

`FIDELITY.md` is the verification ledger: what was verified against a reference, what was
only recorded, and what is excluded (no VNC, no ALSA kernel modules, pause/resume unused).
Receipts live in `out/osworld-v2-evidence/`.

## Quick start

Prerequisites: `E2B_API_KEY=...` in `.env.local` at the repo root, `uv`, Node >=20.18.1,
`npm`, `git`, and Hugging Face access to both gated OSWorld V2 datasets. Authenticate once
with `uvx --from huggingface_hub hf auth login` after accepting their access gates.

```bash
template/fetch_server.sh                    # fetch + patch the pinned guest server
npm --prefix template ci --ignore-scripts
uv run --env-file .env.local npm --prefix template run typecheck
uv run --env-file .env.local npm --prefix template run build   # guest template
uv run --env-file .env.local --locked \
    python services/build_fleet_template.py                      # fleet template
export GUEST_TEMPLATE=<name:build_id>       # from template/results/template-build.json
export FLEET_TEMPLATE=<name:build_id>       # from out/osworld-v2-raw/builds/fleet-template-build.json
runner/setup.sh                             # clone pinned OSWorld-V2 + apply e2b patches
uv run --with huggingface-hub python runner/gated_data.py       # exact gated revisions + hashes

export OSWORLD_CAMPAIGN_ID="osworld-v2-$(date -u +%Y%m%dT%H%M%SZ)"
uv run --env-file .env.local --locked python services/websites/launch.py
uv run --env-file .env.local --locked python services/gitlab/launch.py

RUN_ROOT="$(mktemp -d)"
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/full-manifest.json"
VALIDATION_MANIFEST="$RUN_ROOT/full-manifest.json" VALIDATION_RUNS=1 \
    PARALLEL_CONCURRENCY=24 RAW_DIR="$RUN_ROOT/no-model-raw" \
    EVIDENCE_DIR="$RUN_ROOT/no-model-evidence" OUTPUT="$RUN_ROOT/no-model.json" \
    runner/validate_parallel.sh                # all 108 tasks, zero external model calls
```

The validation coordinator stops both service fleets on exit. Set
`TEARDOWN_FLEETS_ON_EXIT=0` only when deliberately retaining a campaign, and stop it later with
`uv run --env-file .env.local --locked python services/stop.py --campaign-id "$OSWORLD_CAMPAIGN_ID"`.
Run-scoped raw trajectories, service receipts, and secrets stay in ignored paths; committed
evidence is published only after allowlist sanitization.

## Full-model validation sample

After the all-task no-model run passes, use one fresh fleet campaign for a three-step canary and
another for the 24-task representative sample. The sample covers every application family, major
evaluator paths, multiphase tasks, task 082's local service, and a spread of task complexity. The
runner does not retry completed model rollouts.

```bash
export MODEL_API_KEY="$FIREWORKS_API_KEY"
export MODEL_BASE_URL="https://api.fireworks.ai/inference"
export MODEL="accounts/fireworks/models/minimax-m3"
export AGENT_KIND=m3 M3_THINKING_BUDGET=2048
export EVAL_MODEL_API_KEY="$FIREWORKS_API_KEY"
export EVAL_MODEL_BASE_URL="https://api.fireworks.ai/inference/v1"
export EVAL_MODEL="accounts/fireworks/models/minimax-m3"

export OSWORLD_CAMPAIGN_ID="osworld-v2-canary-$(date -u +%Y%m%dT%H%M%SZ)"
uv run --env-file .env.local --locked python services/websites/launch.py
uv run --env-file .env.local --locked python services/gitlab/launch.py
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/canary-manifest.json" --task-id 003
AGENT_MANIFEST="$RUN_ROOT/canary-manifest.json" PARALLEL_CONCURRENCY=1 MAX_STEPS=3 \
    AGENT_RETRY_ATTEMPTS=0 RAW_DIR="$RUN_ROOT/canary-raw" \
    OUTPUT="$RUN_ROOT/canary.json" runner/run_agent_parallel.sh

export OSWORLD_CAMPAIGN_ID="osworld-v2-sample24-$(date -u +%Y%m%dT%H%M%SZ)"
uv run --env-file .env.local --locked python services/websites/launch.py
uv run --env-file .env.local --locked python services/gitlab/launch.py
sample_args=()
for task_id in 003 008 011 015 019 026 035 038 046 048 050 053 057 059 067 069 079 082 083 092 093 103 105 107; do
    sample_args+=(--task-id "$task_id")
done
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/sample24-manifest.json" "${sample_args[@]}"
AGENT_MANIFEST="$RUN_ROOT/sample24-manifest.json" PARALLEL_CONCURRENCY=12 MAX_STEPS=500 \
    AGENT_TASK_TIMEOUT_SECONDS=14400 AGENT_RETRY_ATTEMPTS=0 AGENT_START_STAGGER_SECONDS=1 \
    RUN_TASK_082_CONCURRENT=1 RAW_DIR="$RUN_ROOT/sample24-raw" \
    OUTPUT="$RUN_ROOT/sample24.json" runner/run_agent_parallel.sh
```

Only immutable `name:build_id` references are accepted — launchers reject mutable aliases
and never build templates at runtime. The guest build is promotable only if its exact
immutable build restores with ≥100 GB usable root capacity. `runner/setup.sh` is idempotent
(grep-guarded patches); `runner/setup.sh --restore` reverts its patch footprint for pin
verification.

## Resource requirements

What the validated builds allocate, and what real runs actually used
(`out/osworld-v2-evidence/sample-24/resource-live-*.json` — live agent-run profile via E2B's
sandbox-metrics API; 15 sandboxes, no CPU/memory/disk saturation flags):

| Sandbox | vCPU | RAM | Disk (root) | Measured peaks |
| --- | --- | --- | --- | --- |
| Guest (one per task/worker) | 4 | 8 GB | ≥100 GB usable | 1.5 cores, 0.8 GiB RAM, ~7 GiB disk |
| Fleet ×2 (websites, GitLab) | 4 | 8 GB (+8 GB swap at launch) | same entitlement | first compose build is the heavy phase (~524 s) |

Don't trim below these even though measured peaks look low:

- **8 GB guest RAM is the validated floor** — at 4 GB, `chrome_open_tabs` tasks (3 heavy
  sites at once) thrash and leave CDP unresponsive for minutes (upstream's reference VM has
  16 GB). The ~0.8 GiB measured peak is the desktop baseline between browser-heavy phases.
- **100 GB root is a release contract, not observed usage** — 18 tasks declare
  `volume_size` of 32–100 GB (`validation/volume-requirements.json`), and the relay fails
  task setup if the live root is undersized. The build gate enforces ≥100 GB on restore.
- **4 vCPU matches upstream's t3.xlarge reference**; measured peak was 1.5 cores with no
  saturation, so this has headroom rather than slack to cut.
- **Fleet swap is required once**: the websites fleet's first-boot compose build (23 images)
  OOM-wedges an 8 GB sandbox without it; launchers add it idempotently.

Account level: the full-suite parallel drivers (80 workers) peak near 160 guest sandboxes
(strict reset briefly holds two per worker) plus the two fleet sandboxes — sized for a
200-concurrent-sandbox ceiling. Host needs are negligible (localhost relay + harness).

## Layout

- **`template/`** — guest Template source: GNOME under Xvfb + software GL, pinned/held
  application installs, baked first-run-modal suppression, the guest server payload.
- **`provider/`** — OSWorld provider ABC against E2B; `volume_size` maps to minimum root
  filesystem capacity (18 tasks request 32–100 GB, `validation/volume-requirements.json`).
- **`relay/`** — localhost bridge: token-authenticated proxying (HTTP + WebSocket), CDP
  Host fixups, snapshot save/revert, timeout heartbeat. Port map: `server 5000`,
  `CDP 9222`, `VLC 8080`, plus `OSWORLD_TASK_SERVICE_PORTS` entries (`port` or
  `local:guest` for collision-free parallel workers).
- **`runner/`** — pinned-checkout setup, the verification-ladder scripts, and parallel
  drivers (`validate_parallel.sh`, `run_agent_parallel.sh`; default 80 workers stays under a
  200-concurrent-sandbox ceiling since strict reset can briefly hold two guests per worker).
  `profile_resources.sh` wraps the standalone profiler in `tools/`.
- **`services/`** — website/GitLab fleet launchers; each fleet runs docker-compose inside
  one sandbox, launched per campaign for per-run isolation.
- **`validation/`** — task manifests (10-task sample, full 108-task release) referencing
  gated tasks by id only.

## QEMU → E2B mapping

The relay owns all sandbox objects; the vendored provider only dials the relay on localhost.

| OSWorld provider hook | QEMU semantics | E2B implementation |
| --- | --- | --- |
| `get_vm_path` | path to a `.qcow2` | immutable `GUEST_TEMPLATE` ref, validated fail-closed |
| `start_emulator` | boot the VM | relay creates the guest; provider gates on `/health` |
| `get_ip_address` | IP + `server:cdp:vnc:vlc` ports | `127.0.0.1:15000:19222:0:18080` (+`OSWORLD_RELAY_PORT_BASE`); VNC slot 0 (headless) |
| `save_state` | named `savevm` | `create_snapshot()` (memory + filesystem); `/save` returns after the resumed guest answers |
| `revert_to_snapshot` | in-place `loadvm` | destroy-and-recreate: saved name → new sandbox from its snapshot id; unsaved name (`init_state`) → fresh sandbox from the template |
| `prepare/finalize_volume` | size the root disk | requested GB asserted against the live root (`df`), fail-closed; not an E2B persistent Volume |
| `stop_emulator` | power off | relay `/stop` kills the guest |

Key decisions: a fresh template sandbox *is* `init_state` (the start command boots the
desktop fresh, and upstream never explicitly saves one); `setup.sh` patches in **strict
reset** so every task and setup retry gets a unique sandbox; **pause/resume is deliberately
unused** (multi-hour tasks with scheduled events and live CDP sockets don't tolerate clock
jumps) — liveness comes from the timeout heartbeat instead.

## Networking

Every sandbox (guest and fleet) is created through the shared, unit-tested `e2b_policy.py`:
secure envd, authenticated ingress (`allow_public_traffic: false`; the relay injects the
per-sandbox traffic token, strips it from responses, and binds only `127.0.0.1`), and public
egress with protected ranges denied (RFC1918, link-local/cloud-metadata, CGNAT, benchmark,
multicast, reserved, IPv6 equivalents). `template/build.ts` verifies the applied policy on
the freshly restored build. E2B ingress rejects overridden Host headers, so fleet sites get
one sandbox port each, with Host-mapping proxies routing `<site>.127.0.0.1.nip.io` to the
right ingress port + token (guest port 8080 stays reserved for VLC). `SANDBOX_TIMEOUT_S`
(default 1 h) is an *idle* ceiling: the heartbeat re-arms it while guest traffic flows, so
active multi-hour tasks survive and abandoned guests expire.

## Snapshots

An E2B snapshot captures memory + filesystem, persists independently of its sandbox, and
can seed any number of new sandboxes — the same contract as QEMU's named `savevm` states.
The name→id map lives in relay memory for the relay's lifetime; both revert paths are
live-probed (`out/osworld-v2-evidence/snapshot-probe.json`, 12/12). Snapshots are
deleted when the relay stops. Set `OSWORLD_RETAIN_SNAPSHOTS=1` only for an intentional debugging
session, then delete the recorded ids from `/save` or `/state` when finished.
The capture briefly pauses the guest (dropping CDP sockets), so `/save` re-gates on
readiness and the relay retries WebSocket connects during the resume window.

## Verification ladder

Rungs run in order; PATH_PASS is never reported as task success:

1. Static checks (typecheck, compile, relay unit tests).
2. Live desktop smoke (windows present, first-run modals absent, non-empty a11y tree).
3. Two-pass environment-path validation with fresh-sandbox-per-task proof.
4. Snapshot save/revert probe.
5. No-agent evaluator run (expected zeros).
6. Small full-agent run with audited trajectories.
7. Resource profiling (`runner/profile_resources.sh`) → `resource-requirements.json`;
   run soon after the ladder while metrics are within E2B's retention window.

For a full single-pass run: `VALIDATION_MANIFEST=validation/full-manifest.json`,
`VALIDATION_RUNS=1`, a distinct `EVIDENCE_DIR`, then `runner/validate.sh`.
