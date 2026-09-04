# Fidelity boundary — OSWorld 2.0 on E2B

Final immutable guest template: `osworld-v2-gnome:007fa754-617b-4ff0-957a-42ea4ecf5b8a`
(`out/osworld-v2-evidence/template-build.json`) — the hardening rebuild of `87d8a46b…`:
digest-pinned Ubuntu base image, sha256-pinned artifact downloads (VS Code .deb, Zotero,
REAPER, Compose v2 plugin), OpenBoard + snap-launcher compatibility, and the REAPER
startup-nag launcher. Earlier ladder receipts record the predecessor build each rung ran
against (`817519a3…`, `87d8a46b…`); each evidence file cites its own build id, and the
full-suite/focused receipts under `out/osworld-v2-evidence/full-suite/` cover the changed
surface on the final build.
Release pin: `osworld-v2-2026.08.08`. OSWorld-V2 checkout: `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`.

This ledger separates what was verified to match a stated reference, what was recorded
without a reference to match against, and what is excluded or unexercised outright.

## Matched

- **Guest OS**: Ubuntu 22.04, GNOME Shell 42.9 — ported unchanged from hark's proven
  OSWorld 1.0 desktop reconstruction (`out/osworld-v2-evidence/live-smoke.json`, `versions`).
- **Source-pinned applications** (deterministic, versioned artifact, not apt-latest):
  `code` 1.91.1-1720564633 (Microsoft's permanent versioned `.deb`), `zotero` 7.0.15
  (versioned tarball from zotero.org), `reaper` 7.79 (versioned tarball from reaper.fm).
  `out/osworld-v2-evidence/template-build.json` `pinnedVersions`.
- **Guest control server**: V2's FastAPI/uvicorn `osworld-server`, vendored at
  `xlang-ai/osworld-server@a3cc3f0c64e463f020d1a44780307e9b46cbcab1`, with hark's null-safe
  AT-SPI serializer guards re-applied, plus the exact-process-path open-file fallback and
  near-silent ffmpeg recording flags. The unlicensed source is generated only by
  `template/fetch_server.sh`; the repository carries two reviewable patches against the pin,
  not a copy of upstream source. V2 carries the identical AT-SPI defect — `role`, `name`,
  action description, key binding, stripped role-name — at the same call sites as V1.
  Corroborated live: accessibility observations returned substantial, non-empty content
  with no serialization crash across every one of the 20 validated sandboxes
  (`out/osworld-v2-evidence/validate-run1.json`, `validate-run2.json`,
  `accessibility_characters` present and nonzero on every record).
- **Provider contract**: `E2BVMManager`/`E2BProvider` registered via three idempotent,
  grep-guarded source patches (factory registration, cloud-provider classification, strict
  reset) — patch anchors and logic committed at `runner/setup.sh`.
  Verified working, not just applied: the environment-path validation ladder created 20
  unique sandboxes through this exact provider registration, all `PATH_PASS`
  (`out/osworld-v2-evidence/validate-run1.json`, `validate-run2.json`). Destroy-and-
  recreate revert semantics transfer from V1 unchanged.
- **Snapshot semantics**: live-probed, not inferred — a marker written before `save_state`
  survives a revert into a new sandbox; an unsaved name falls back to the template's neutral
  state (marker absent); restricted ingress is preserved across both paths. 12/12 checks,
  `out/osworld-v2-evidence/snapshot-probe.json`, `all_checks_passed: true`.
- **Restricted ingress + traffic-token auth**: unauthenticated requests to the guest port
  403, authenticated 200 — verified at the guest control port and again at the service
  fanout ports (`out/osworld-v2-evidence/live-smoke.json`, `out/osworld-v2-evidence/spike-ingress.json`).
- **Sandbox network boundary**: every guest and service sandbox uses secure envd and
  authenticated ingress. Public egress remains available for upstream setup downloads while
  RFC1918, link-local/metadata, carrier-grade NAT, benchmark, multicast, and reserved destination
  ranges are denied. The shared Python policy is unit-tested, and the exact guest build was
  restored successfully with the same policy during its post-build smoke.
- **Root-volume contract**: OSWorld `volume_size` is mapped to minimum usable root-filesystem
  capacity, not to an E2B persistent Volume. All 18 requesting tasks use 32–100 GB; the exact
  immutable guest restore reported 103,495,585,792 usable bytes and passed the 100 GB release
  maximum. `prepare_volume` records the request and `finalize_volume` fails before task setup if
  the live root is undersized. See `validation/volume-requirements.json` and
  `out/osworld-v2-evidence/template-build.json`.
- **Boot-time credential**: verified as PAM verifies it, not merely "chpasswd ran" —
  `crypt(candidate, stored_salt) == stored_hash` read from `/etc/shadow` via the guest's
  own NOPASSWD sudo channel; correct password matches, wrong password does not
  (`live-smoke.json` `su_auth`).
- **Audio path**: PulseAudio null sink `vsink` present, a rendered tone plays to the sink
  successfully (`out/osworld-v2-evidence/spike-audio.json`). REAPER and MuseScore are baked
  to their Dummy/PulseAudio backends, not a hardware ALSA path — see Recorded-only for the
  kernel-module boundary this implies.
- **First-run modal absence, showing-gated**: every V2-added first-run-prone app (MuseScore,
  FreeCAD, REAPER) plus the carried-over V1 set (Chrome, LibreOffice, VLC) shows its main
  window present AND its first-run modal absent, using an AT-SPI `showing`/`visible` flag
  check that is strictly stronger than V1's presence-only check (a suppressed-but-present
  dialog object no longer trips a false positive) — `live-smoke.json`, Task 10 report "Showing-
  gated modal detection".
- **Environment-path completion**: two independent 10-task passes, 10/10 `PATH_PASS` each,
  20/20 unique sandboxes, every evaluator ran without an infrastructure error
  (`out/osworld-v2-evidence/validate-run1.json`, `validate-run2.json`).
- **Full-agent transport**: 10/10 rollouts transport-clean (0 transport errors, 0 evaluator
  crashes) across parallel namespaced relays (`out/osworld-v2-evidence/agent-run-summary.json`
  `gate`).

## Recorded-only (no reference to certify against, or best-effort under load)

- **Unpinnable applications** — apt-mark held so the immutable build freezes whatever
  version was served at build time, but not matched to any V2 reference image (V2, like
  V1, has no published reference-image version inventory to check against):
  `google-chrome-stable` 152.0.7977.64-1 (Google's apt repo serves only latest stable, no
  archived `.deb`), `musescore3` 3.2.3+dfsg2-11, `shotcut` 22.01.30+ds-1, `freecad`
  0.19.2+dfsg1-3ubuntu1 (all single-version Ubuntu 22.04 universe packages, no archived
  `.deb` URL). Deterministic-per-release but not source-pinned: `libreoffice` 7.3.7.2,
  `gimp` 2.10.30, `vlc` 3.0.16, `thunderbird` 140.7.1esr, `evince` 42.3, `pulseaudio`
  15.99.1 (jammy pockets). Full table: `out/osworld-v2-evidence/template-build.json`
  `unpinnableVersions` + `unpinnableVersionsNote`.
- **Task-service port mapping**: `OSWORLD_TASK_SERVICE_PORTS` accepts either a literal `port`
  entry for backward compatibility or an explicit `local:guest` mapping. `task_082`, the sole
  task that host-dials a guest service, uses its canonical host port 3000 mapped to guest port
  3000. Preflight rejects a run containing `task_082` when host port 3000 is unavailable; because
  no other release task owns that listener, it can otherwise run in the concurrent wave without
  routing into another worker's guest.
- **Evaluator-model transport**: the pinned checkout's OpenAI evaluator backend is
  patched (`runner/setup.sh`, patch (e)) to a 180 s request timeout with zero SDK
  retries, replacing upstream's ~600 s timeout plus SDK retry layer. A judge
  response slower than 180 s or a transient transport error the SDK would have
  retried therefore fails the evaluator call instead of eventually scoring; this
  is a deliberate bound on external-call exposure and can differ from upstream
  scoring on a slow judge endpoint.
- **Per-task wall clock**: `AGENT_TASK_TIMEOUT_SECONDS` (default 14400 s) bounds a
  task rollout; upstream has no per-task deadline. A timed-out task produces a
  fail-closed `task-timeout` receipt (never a score) and is eligible for the
  infrastructure retry wave — unless a completed receipt already exists when the
  deadline fires, in which case that scored receipt is kept.
- **Setup upload paths**: the relay serves `POST /setup/upload` directly through
  the E2B files API and requires absolute guest paths; upstream's in-guest server
  also accepts `~`- and `$VAR`-relative paths. All 108 release tasks use absolute
  paths; custom configs with relative upload destinations must be rewritten.
- **Guest apt front-end**: the template installs a `/usr/local/sbin/apt-get`
  wrapper forcing `DEBIAN_FRONTEND=noninteractive` and conffile-keep defaults so
  task-driven package operations cannot hang a rollout; a stock VM would prompt.
- **Audio kernel path**: no `snd-dummy`/`snd-aloop` ALSA kernel module in the Firecracker
  guest kernel (`modprobe: FATAL: Module snd-dummy not found`, `spike-audio.json`). The
  PulseAudio null-sink path was sufficient for every app exercised (REAPER and MuseScore
  both configured to non-ALSA backends), so this did not block any verified check, but a
  task requiring a real ALSA device would hit this boundary.
- **Fonts, seeded profiles, application preferences/accounts**: not reproduced or certified
  against any reference image, same caveat as the V1 conversion — no file-by-file reference
  inventory exists to check against (V2's reference is a gated qcow2/AMI, never inspected).
- **Service fleet topology**: mocked websites and GitLab are self-hosted-in-sandbox per
  campaign (fresh fleet per campaign), not a team-hosted shared deployment — a deliberate
  choice (turns GitLab's shared-root-token problem into per-run isolation; deferred
  alternative noted, not implemented). Functionally equivalent for task purposes (each
  site's state is cookie-scoped via its own `/api/state`, so the deployment is stateless
  across tasks) but the topology itself differs from any official hosted deployment.
  `out/osworld-v2-evidence/services-websites.json`, `services-gitlab.json`.
- **Ingress addressing mode**: per-port fanout + host+guest Host-mapping proxies, not
  single-port Caddy Host-header passthrough. Directly probed and ruled out: E2B ingress
  rejects both an extra SNI label (`spike-ingress.json` probe `7_tls_handshake_mailhub`,
  TLS handshake fails before any HTTP layer) and an overridden `Host` header on the valid
  ingress hostname (`services-websites.json` `addressing_evidence.caddy_passthrough_probe`,
  `400 Invalid host`). `WEBSITE_HOST_SUFFIX=127.0.0.1.nip.io:8090` via the host proxy (binding
  127.0.0.1:80 was denied on the build host) plus a native-root guest-side proxy on :80/:8090.
  Port 8080 remains exclusively assigned to VLC.

## Not-certified / excluded

- **VNC: absent entirely.** Headless, same as the V1 conversion; the provider's IP/port
  tuple reports `0` for the VNC slot. Never built, never probed.
- **LLM-judge evaluator component: unexercised at any nonzero score.** All 10 real-agent
  rollouts scored 0.0 (`out/osworld-v2-evidence/agent-run-summary.json`
  `score_summary_reported_not_gated`), so no judge call was ever triggered by a genuinely
  agent-solved task. `judge_used` is recorded per task from what could be determined from
  the evaluator's own return shape: `false` for the 3 tasks (103, 026, 069) whose evaluator
  returned a dict with no LLM-judge key present; `null` — undeterminable — for the other 7
  (single-phase, no separate result artifact to inspect). Per recon, the judge helpers
  (`desktop_env/evaluators/metrics/llm_metrics.py`) touch the guest only through getters the
  harness already uses and then make a host-side LLM API call — no new guest endpoint — but
  that call path was never exercised end-to-end in this conversion.
- **Pause/resume: not used.** Deliberate no-pause-by-default stance (median task length
  ~1.6 h, tail to ~3 h — a guest clock jump under pause risks breaking scheduled dynamic
  events and drops CDP WebSockets). None of this conversion's rollouts (up to ~1064 s /
  ~17.7 min at a 75-step budget) invoked pause; the feature itself remains untested here.
- **Proxy-required tasks: not applicable to this release.** Unlike V1 (which excluded
  `proxy=true` tasks), recon found zero references to a proxy-requirement field anywhere in
  the pinned V2 task set — there is nothing to exclude on this axis.

## Parity promotion-gate stanza

The promotion gate: a same-model, same-config comparison against a published OSWorld 2.0
result, priced before authorization. **This gate is defined and costed here; it is not
executed by this conversion.**

| Field | Contents |
| --- | --- |
| Reference model / agent | Claude Opus 4.8, max-thinking configuration — published 20.6% binary task-completion on OSWorld 2.0's 108-task release. Secondary published reference: GPT-5.5, ~13–14%, same release. |
| Published band | 20.6% (Opus 4.8, primary); ~13–14% (GPT-5.5, secondary) — point estimates as published; no confidence interval is publicly stated for either, unlike the Terminal-Bench 2.0 comparison this repository already has (16.0–21.4%). Treat point-estimate comparison as weaker evidence until an interval is available. |
| Valid-trial count | Not fewer than 1 trial/task (108 rollouts) to reproduce a single comparable point; **recommended minimum for a defensible comparison is 3 trials/task (324 rollouts)** given no published interval to anchor against. Operator sets the final count before authorizing spend. |
| Release pins | `osworld-v2-2026.08.08`; OSWorld-V2 commit `d578d2d4e0dc82b43e270fdaa7fa89d9708cd154`; guest template `osworld-v2-gnome:007fa754-617b-4ff0-957a-42ea4ecf5b8a`; e2b SDK `2.34.0`; aiohttp `3.14.1`. All are fixed across every trial. |
| Cost estimate | See below. |

### Cost estimate (derived from this conversion's own measured numbers)

This conversion's real-agent rung ran gpt-4o at a **75-step** budget: 10 rollouts, wall
times 704.9–1064.2 s (mean 856.4 s ≈ 14.3 min/rollout,
`out/osworld-v2-evidence/agent-run-summary.json` `tasks[].wall_clock_s`). A genuine parity
attempt needs a far larger step budget — upstream's published rollouts average closer to
**~318 tool calls**, consistent with the reported **median ~1.6 h / tail ~3 h** per-task
duration. Linearly scaling this conversion's 75-step numbers by 318/75 ≈ 4.24× gives
**~50–75 min/rollout (mean ≈ 60 min)** — the same order of magnitude as the median figure
above, but almost certainly an *underestimate*: our scaling is linear in step count and
does not capture per-step context growth, retries, or harder-task step overruns that a
real long-horizon rollout accumulates. Treat the published median 1.6 h / tail 3 h as the
operative planning number, with this conversion's scaled ~1 h figure only as a
sanity-check floor.

- **1 trial/task, 108 tasks, at the median 1.6 h/rollout**: ≈ 172.8 sandbox-hours. Serial,
  that is ~7.2 sandbox-days; a concurrency ceiling of 200 sandboxes, approved by the
  operator during this conversion (recorded here as the authoritative statement — no
  separate committed artifact), covers all 108 in one wave, so wall clock collapses to
  approximately one task's duration — **~1.6 h median, ~3 h worst-case tail** — not 7 days.
- **3 trials/task (recommended), 324 rollouts**: ≈ 518.4 sandbox-hours at the median. At the
  same 200-concurrency ceiling that is 324/200 ≈ 2 waves, so **~2× one task's duration
  wall-clock, roughly 3–6 h**, plus the fixed cost of running two fleet lanes (websites,
  GitLab) live for the whole campaign window.
- **Sandbox compute cost and LLM API cost in dollars are deliberately not estimated here** —
  current E2B sandbox pricing and the chosen model's per-token pricing (Opus 4.8 with
  extended/max thinking is materially more expensive per rollout than the gpt-4o baseline
  this conversion used) were out of scope for this task and must be priced against
  current rate cards before authorization, not assumed from this conversion's numbers.
- This estimate excludes: fleet warm-up (websites fleet first-boot compose build measured
  524 s, `out/osworld-v2-evidence/services-websites.json`), any repair/retry overhead, and
  judge-model calls (unexercised in this conversion, so their cost contribution is unknown).

**NOT AUTHORIZED — separate decision.** This stanza defines and prices the gate; it does
not request or imply authorization to run it. A parity campaign requires an explicit
operator decision to spend against the estimate above — reserved for major
upstream revisions or explicit demand, never routine adoptions.
