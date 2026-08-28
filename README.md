# OSWorld 2.0 on E2B

Run the OSWorld 2.0 benchmark (`xlang-ai/OSWorld-V2`) against E2B sandboxes instead of
QEMU/VMware/AWS virtual machines: an Ubuntu 22.04 GNOME guest as an immutable E2B Template,
an implementation of OSWorld's provider contract at the sandbox boundary, a localhost relay
that owns all E2B credentials, and container-lane service fleets (mocked websites, GitLab)
in their own sandboxes. The unit of conversion is the suite substrate (one guest template,
one provider/relay pair), not the task — there are no per-task artifacts here. The pinned
release lives at `examples/osworld-v2/upstream.lock.json`; the pinned upstream checkout
(`OSWorld-V2/`) and any downloaded gated task data (`tasks/`) are gitignored, never
committed — OSWorld 2.0's task and asset datasets are gated upstream, so each consumer
accepts the upstream gate and downloads them with their own credentials.

Prerequisites: an E2B API key in `.env.local` at the repo root (`E2B_API_KEY=...`), `uv`,
Node 20+, and `git` (upstream checkouts are plain HTTPS clones at pinned commits). Before the first template
build, run `template/fetch_server.sh` — the upstream guest server
(`xlang-ai/osworld-server`) publishes no license, so this repo does not redistribute it;
the script downloads the exact pinned commit and applies the local AT-SPI null-guard patch
from `patches/`.

## Checkout state

`OSWorld-V2/` is gitignored, so its state lives on disk between runs. `runner/setup.sh`
leaves it in the **applied** state — the e2b provider registered/classified, strict-reset
patch present, and `provider/manager` + `e2b_relay.py` vendored in. That is the intended
operating state: the harness, relay, and downstream (Task 12) all require it, and re-running
`setup.sh` re-applies the patches idempotently (each is grep-guarded, reporting "already
applied"). To return the checkout to **pristine** for pin verification, run
`runner/setup.sh --restore`, which reverts only setup.sh's own patch footprint (leaving
`.venv` and other untracked working files intact); re-run `setup.sh` to get back to applied.

## Subdirectories

- **`template/`** — the guest Template source and baked files: desktop bring-up (logind
  session, hand-rolled GNOME, Xvfb, software GL), application installs pinned and held, the
  bench's guest agent, and first-run modal suppression baked in before the snapshot. Run
  `./fetch_server.sh` once (see prerequisites above), then build with
  `uv run --env-file ../.env.local npm run build` from this directory. The build is
  promotable only when its exact immutable build restores with at least 100 GB of usable root
  capacity; the resulting `name:build_id` and capacity are written to
  `template/results/template-build.json`.
- **`provider/`** — the boundary-layer implementation of the bench's provider ABC against E2B
  sandboxes: `start_emulator`, `get_ip_address`, `save_state`/`revert_to_snapshot`, and
  `stop_emulator`. `prepare_volume` and `finalize_volume` map OSWorld's `volume_size` to a minimum
  sandbox root-filesystem capacity. The relay measures `/` on the current E2B sandbox and fails
  task setup if the requested decimal-GB capacity is unavailable. This is the VM's root disk,
  matching QEMU/AWS/Docker provider semantics; it is not an E2B persistent Volume mount. The
  pinned release has 18 such tasks and a 100 GB maximum, recorded in
  `validation/volume-requirements.json`.
- **`relay/`** — the host relay: sandboxes created with authenticated ingress, secure envd,
  public egress retained for task setup, and explicit denial of RFC1918, link-local/metadata,
  carrier-grade NAT, benchmark, multicast, and reserved destination ranges; per-sandbox traffic
  token injection; an activity-based timeout heartbeat (guest-directed traffic re-arms
  `set_timeout(SANDBOX_TIMEOUT_S)` every `SANDBOX_HEARTBEAT_INTERVAL_S`, so a long-running task
  — V2 medians ~1.6 h — outlives the idle ceiling while an abandoned guest still expires);
  HTTP+WebSocket proxying; protocol fixups (CDP Host-header
  normalization), and the port map (`server 5000`, `CDP 9222`, `VLC 8080`, plus task-service
  mappings selected with `OSWORLD_TASK_SERVICE_PORTS`; entries may be `port` or
  `local:guest`, so concurrent workers can preserve guest-visible ports behind unique host
  listeners). Snapshot name→id scope and retention are covered in "Snapshots" below.
- **`runner/`** — pinned-checkout wiring against the locked upstream commit, the surgical
  idempotent source patches (strict reset on every task/setup retry), and the smoke/probe
  scripts used by the verification ladder. `validate_parallel.sh` and `run_agent_parallel.sh`
  are the bounded-concurrency full-suite drivers (default 80 workers; strict reset can briefly
  hold two guests per worker, so the peak stays under a 200-concurrent-sandbox account
  ceiling with headroom for the two fleet sandboxes); `run_path_task.sh` runs one task's
  environment path solo; `wave.sh` fires namespaced parallel real-agent rollouts (one relay
  per worker via disjoint `OSWORLD_RELAY_PORT_BASE`). `profile_resources.sh` is the rung-7 resource-profiling
  wrapper: a thin invocation of the portable, standalone profiler at repo-root
  `tools/profile_e2b_resources.py`, which reads the sandbox ids the earlier rungs recorded and
  queries E2B's sandbox-metrics API to publish the run's real CPU/RAM/DISK. No profiling logic
  lives in the vended guest/provider/relay.
- **`validation/`** — the task manifests used for environment-path validation, including the
  10-task cross-domain sample and the complete 108-task release manifest; no gated task or
  evaluator content is stored here, only manifest references by id. Runtime guest and fleet
  references must always be immutable `name:build_id` values.

## QEMU → E2B mapping

Upstream OSWorld runs each guest as a full VM — QEMU locally, VMware/AWS/Docker variants in
the other providers — behind `desktop_env`'s `Provider`/`VMManager` contract. This conversion
implements that contract against E2B sandboxes, with the localhost relay owning all E2B
credentials and sandbox objects (the vendored provider never talks to E2B directly):

| OSWorld provider hook | VM semantics (QEMU) | E2B implementation |
| --- | --- | --- |
| `get_vm_path` | path to a `.qcow2` image | the immutable `GUEST_TEMPLATE` `name:build_id` reference, regex-validated fail-closed |
| `start_emulator` | boot the VM | the relay creates the guest sandbox at startup; the provider just gates on relay `/health` |
| `get_ip_address` | VM IP + `server:cdp:vnc:vlc` port tuple | loopback tuple `127.0.0.1:15000:19222:0:18080` (each shifted by `OSWORLD_RELAY_PORT_BASE`); DesktopEnv always dials the local relay, never E2B ingress directly |
| `save_state` | named `savevm` (memory + disk) | `sandbox.create_snapshot()` — a memory+filesystem capture; the guest pauses briefly and resumes, and `/save` returns only after the resumed guest server answers again |
| `revert_to_snapshot` | in-place `loadvm` | destroy-and-recreate: a saved name seeds a **new** sandbox from its E2B snapshot id; any unsaved name (upstream's default `init_state`, which is never explicitly saved) means a fresh sandbox from the immutable template |
| `prepare_volume` / `finalize_volume` | size the VM's root disk | the requested decimal GB is asserted against the live root filesystem (`df`) before task setup and fails closed if undersized — the E2B root disk plays the VM root disk's role; this is **not** an E2B persistent Volume mount |
| `stop_emulator` | power off | relay `/stop`; the current guest sandbox is killed |

Decisions behind the mapping:

- **`init_state` by construction.** The template's start command boots the desktop fresh in
  every new sandbox, so "fresh sandbox from the immutable template" *is* the post-boot init
  state — no explicit init snapshot needs to exist, matching upstream, which never saves one.
- **Strict reset.** Upstream skips the revert when it believes the environment is unused.
  `runner/setup.sh` patches `desktop_env` so the e2b provider reverts on *every* task and
  every setup retry: even a partial or failed setup cannot leak state into the next attempt,
  and every task provably runs on a unique sandbox (asserted in the validation receipts).
- **Headless, no VNC.** The provider tuple reports `0` for the VNC slot. Observation is
  screenshots + the AT-SPI accessibility tree via the guest server; nothing in the pinned
  task set requires VNC.
- **No pause/resume in the task loop.** E2B pause/resume exists but is deliberately unused:
  V2 tasks median ~1.6 h with scheduled dynamic events and live CDP websockets, and a guest
  clock jump under pause risks breaking both. Liveness is handled by the timeout heartbeat
  instead (below). The only pause the guest ever experiences is the brief internal one during
  `create_snapshot`, which is explicitly re-gated on readiness.
- **Hardware boundary.** The Firecracker guest has no KVM and no ALSA kernel modules
  (`snd-dummy`/`snd-aloop` absent); audio runs through a userspace PulseAudio null sink with
  REAPER/MuseScore baked to non-ALSA backends, and rendering uses Xvfb + software GL. See
  `FIDELITY.md` for what this boundary does and does not affect.
- **Two-lane composition.** The guest desktop is the VM lane; the mocked-website and GitLab
  service fleets run as separate container-lane sandboxes from their own fleet template,
  launched per campaign (per-run isolation instead of a shared hosted deployment).

## Networking

Every sandbox this suite creates — guest and fleet alike — is created the same way, through
the shared, unit-tested policy module `e2b_policy.py`:

- **Secure envd** (`secure=True`): the in-sandbox control daemon requires its access token.
- **Authenticated ingress** (`allow_public_traffic: false`): every E2B ingress request must
  carry the per-sandbox traffic access token. The relay injects the token on every proxied
  request and strips it from response headers; the relay itself binds only `127.0.0.1`, so
  the token never reaches OSWorld code or the agent. Sandbox creation fails closed if E2B
  returns no token. Verified live: unauthenticated ingress 403s, authenticated 200s.
- **Egress: public internet allowed, protected ranges denied.** Task setup legitimately
  downloads from package repos, source checkouts, and OCI registries (upstream VMs have NAT
  internet, so this is parity). `deny_out` blocks what a benchmark guest must never reach:
  RFC1918 (`10/8`, `172.16/12`, `192.168/16`), link-local/cloud-metadata (`169.254/16`),
  carrier-grade NAT (`100.64/10`), IETF protocol assignments (`192.0.0/24`), benchmark
  (`198.18/15`), multicast (`224/4`), reserved (`240/4`), and the IPv6 loopback/ULA/
  link-local/multicast equivalents as defense in depth. `template/build.ts` re-reads the
  applied policy from the freshly restored build and fails the build on any mismatch.
- **Ingress addressing is per-port, not per-host.** E2B ingress rejects a foreign SNI label
  and an overridden Host header (both probed directly — see `FIDELITY.md`), so site names
  cannot ride the Host header across ingress. Fleet services publish one sandbox port per
  site, and Host-mapping proxies (host-side for the harness, guest-side installed by the
  relay at session start) map `<site>.127.0.0.1.nip.io` onto the right fleet ingress port +
  token. Guest port 8080 stays reserved for VLC's baked Lua HTTP interface.
- **Lifetime.** `SANDBOX_TIMEOUT_S` (default 1 h) is an *idle* ceiling, not a task ceiling:
  the relay's heartbeat calls `set_timeout(SANDBOX_TIMEOUT_S)` whenever guest-directed
  traffic flowed since the last refresh, so an active multi-hour task stays alive while an
  abandoned guest expires within one ceiling of its last activity. Every relay shutdown and
  error path reaps the sandboxes it created, including a create that completes after a
  client disconnect or a `/stop` that lands mid-reset.

## Snapshots

- An E2B snapshot is a memory + filesystem capture of the running sandbox. It persists
  independently of the sandbox that produced it and can seed any number of new sandboxes, so
  one `save_state` name stays revert-able for the whole run — the same contract as QEMU's
  named `savevm` states.
- The OSWorld-name → E2B-snapshot-id map lives in relay process memory for the relay's
  lifetime. Reverting to a saved name creates a new sandbox from the snapshot; reverting to
  an unknown name falls back to a fresh sandbox from the immutable template. Both paths were
  live-probed (marker survives a snapshot revert, marker absent after a template reset,
  restricted ingress preserved across both — `out/osworld-v2-evidence/snapshot-probe.json`,
  12/12 checks).
- **Retention is deliberate: the relay never deletes snapshots.** A run's saved states stay
  inspectable in the E2B account afterwards. Consumers who want cleanup record the ids from
  the `/save` response or the `snapshots` map in `/state`; persisting the name→id map across
  relay restarts is likewise production-integration work owned by the consumer.
- The capture pauses the guest and drops live CDP websockets. `/save` therefore re-gates on
  the resumed guest server answering before returning, and the relay's websocket handler
  retries the upstream connect (`RELAY_WS_CONNECT_RETRY_S`) so a CDP client re-dialing during
  the resume window connects as soon as the guest is back instead of failing instantly.

## Build and launch contract

Build the two templates as explicit, separate operations. From `template/`, run the guest build
command above. From `services/`, run
`uv run --env-file ../.env.local --python 3.12 --with e2b==2.34.0 python build_fleet_template.py`.
The fleet builder digest-pins its Debian base and Docker installer, restores the exact build,
checks Docker and root capacity, and emits `out/osworld-v2-evidence/fleet-template-build.json`.

Set `GUEST_TEMPLATE` and `FLEET_TEMPLATE` to the emitted immutable references before validation or
service launch. Launchers reject mutable aliases and never build templates at runtime. For a full
single-pass environment-path run, set `VALIDATION_MANIFEST=validation/full-manifest.json`,
`VALIDATION_RUNS=1`, and use a distinct `EVIDENCE_DIR` before invoking `runner/validate.sh`.

## Ladder run order

Verification runs as a ladder — each rung must pass before the next spends more, and
PATH_PASS (the environment path worked) is never reported as task success:

1. Static checks — typecheck, compile, unit tests of relay logic.
2. Live desktop smoke on a fresh sandbox — application window present in the accessibility tree
   and first-run modal absent, non-empty accessibility tree, display geometry, `kvm=absent`
   probe recorded.
3. Two-pass environment-path validation across the cross-domain task manifest, with
   fresh-sandbox-per-task proof.
4. Snapshot save/revert probe.
5. No-agent evaluator run (expected zeros).
6. Small full-agent run with audited trajectories confirming evaluators score agent-caused
   outcomes.
7. Resource profiling (`runner/profile_resources.sh`) — after the sandbox-creating rungs, query
   E2B's sandbox-metrics API for every sandbox id those rungs recorded and emit
   `out/osworld-v2-evidence/resource-requirements.json`: aggregate CPU/RAM/DISK peaks, saturation
   flags, and a recommended memory floor. This is the publishable resource-requirements evidence;
   run it soon after the ladder so metrics are still within E2B's retention window.
