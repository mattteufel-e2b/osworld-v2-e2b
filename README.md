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
patched locally (`template/fetch_server.sh` + `patches/`), never redistributed.

`FIDELITY.md` is the verification ledger: what was verified against a reference, what was
only recorded, and what is excluded (no VNC, no ALSA kernel modules, pause/resume unused).
Receipts live in `out/osworld-v2-evidence/`.

## Quick start

Prerequisites: `E2B_API_KEY=...` in `.env.local` at the repo root, `uv`, Node 20+, `git`.

```bash
template/fetch_server.sh                    # fetch + patch the pinned guest server
cd template && uv run --env-file ../.env.local npm run build   # guest template
cd ../services && uv run --env-file ../.env.local --python 3.12 --with e2b==2.34.0 \
    python build_fleet_template.py          # fleet template
export GUEST_TEMPLATE=<name:build_id>       # from template/results/template-build.json
export FLEET_TEMPLATE=<name:build_id>       # from out/osworld-v2-evidence/fleet-template-build.json
runner/setup.sh                             # clone pinned OSWorld-V2 + apply e2b patches
runner/validate.sh                          # environment-path validation
```

Only immutable `name:build_id` references are accepted — launchers reject mutable aliases
and never build templates at runtime. The guest build is promotable only if its exact
immutable build restores with ≥100 GB usable root capacity. `runner/setup.sh` is idempotent
(grep-guarded patches); `runner/setup.sh --restore` reverts its patch footprint for pin
verification.

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
deliberately never deleted — record ids from `/save` or `/state` to clean up after a run.
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
