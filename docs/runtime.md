# Runtime and resource reference

For setup and first-run commands, see the [README](../README.md). This reference describes
the pinned guest, service fleets, provider mapping, networking, and snapshot behavior.
The release pin is `examples/osworld-v2/upstream.lock.json`.

The upstream checkout (`OSWorld-V2/`) and gated task data (`tasks/`) are downloaded locally
and never committed. Consumers accept the upstream access gates and use their own credentials.
The upstream guest server (`xlang-ai/osworld-server`) publishes no license; it is fetched at
a pinned commit and patched locally by `template/fetch_server.sh`, never redistributed.
Fetching replaces the ignored generated payload and applies committed patches in lexical order.

Only immutable `name:build_id` template references are accepted. Launchers never build templates
at runtime. `runner/setup.sh --verify OSWorld-V2` checks the checkout pin and adapter patches;
`runner/setup.sh --restore` returns the checkout to the bare pin before patches are reapplied.

## Host tools

Install FFmpeg and ImageMagick on the runner host: `brew install ffmpeg imagemagick`
on macOS or `sudo apt-get install ffmpeg imagemagick` on Ubuntu/Debian. Preflight requires
`ffprobe` and `identify` because upstream evaluators use them for media metadata and XCF
layer extraction. Use Bash for the documented command examples, and export model credentials
in that shell; `.env.local` does not populate shell variables by itself.

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

Both parallel drivers default to and cap `PARALLEL_CONCURRENCY` at 80. Strict reset can
briefly hold two guests per worker, so 80 workers peak near 160 guest sandboxes plus the two
fleet sandboxes under a 200-concurrent-sandbox account ceiling. Independently, each worker
slot offsets its relay listeners by 500 ports, and the CDP listener crosses 65535 at slot 93,
so the port layout itself allows at most 92 workers. Optional retry waves use
`AGENT_RETRY_CONCURRENCY` (default and cap of 4). Actual concurrency also depends on the
selected tasks, available host resources, and your E2B account capacity. Each host worker
runs a relay and upstream's evaluator stack (~270 MB RSS at
startup; easyocr/torch load only if an OCR metric runs).

## Layout

- **`template/`** — guest Template source: GNOME under Xvfb + software GL, pinned/held
  application installs, baked first-run-modal suppression, the guest server payload.
- **`provider/`** — OSWorld provider ABC against E2B; `volume_size` maps to minimum root
  filesystem capacity (18 tasks request 32–100 GB, `validation/volume-requirements.json`).
- **`relay/`** — localhost bridge: token-authenticated proxying (HTTP + WebSocket), CDP
  Host fixups, snapshot save/revert, timeout heartbeat. Port map: `server 5000`,
  `CDP 9222`, `VLC 8080`, plus `OSWORLD_TASK_SERVICE_PORTS` entries (`port` or
  `local:guest` for collision-free parallel workers).
- **`runner/`** — pinned-checkout setup and the benchmark driver `run_agent_parallel.sh`
  (default and cap of 80 concurrent workers).
- **`maintainer/`** — verification-ladder scripts, `validate_parallel.sh`, and
  `profile_resources.sh`, which wraps the standalone profiler in `tools/`.
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
