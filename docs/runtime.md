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
| Guest (one per task/worker) | 4 | 8 GB | ≥100 GB usable | 1.5 cores, 0.8 GiB RAM, 16.2 GiB disk at boot |
| Fleet ×2 (websites, GitLab) | 4 | 8 GB (+8 GB swap at launch) | same entitlement | first compose build is the heavy phase (~524 s) |

The guest disk entry is the image footprint, not a live-run peak: the build's own smoke sandbox
on `osworld-v2-gnome:cd4a1a63-eb56-418f-9ac3-60fbbf2b838e` reported 17,384,730,624 bytes used of
114,839,875,584 usable on `/` before any task wrote anything (`rootUsedBytes` in that build's
receipt, committed at
`out/osworld-v2-evidence/template/template-build-cd4a1a63-eb56-418f-9ac3-60fbbf2b838e.json`),
so a running task only adds to it. That footprint covers the preinstalled application set recorded in the
receipt's `applicationInventory` — Google Chrome 153.0.8010.47-1 and KiCad 10.0.6~ubuntu22.04.1
(apt, `apt-mark hold`, version frozen by the build id rather than by a source pin), WPS Office
11.1.0.11723.XA, Blender 4.5.14 LTS, x11vnc 0.9.16-8, novnc 1:1.0.0-5, websockify
0.10.0+dfsg1-2build1, libnss3-tools 2:3.98-0ubuntu0.22.04.4, and poppler-utils
22.02.0-2ubuntu0.13 — plus the sha256-pinned MuseScore
Studio 4.6.5 and FreeCAD 1.1.3 AppImages under `/opt`, whose versions are pinned in
`template/template.ts` and whose extracted AppImage payloads the same smoke listed.

This recipe has a shelf life. Kingsoft serves only its current WPS build (older build numbers
return 403) and the KiCad PPA serves whatever is current, so when either rotates, the pinned
`curl -f` download or the held apt version stops being available and the template build hard-fails
instead of silently installing something else. That fail-closed behaviour is intended — an already
built image is immutable and unaffected — but a rebuild after a rotation needs the artifact
re-pinned in `template/template.ts` and its sha256 recomputed from the new download.

Don't trim below these even though measured peaks look low:

- **8 GB guest RAM is the validated floor** — at 4 GB, `chrome_open_tabs` tasks (3 heavy
  sites at once) thrash and leave CDP unresponsive for minutes (upstream's reference VM has
  16 GB). The ~0.8 GiB measured peak is the desktop baseline between browser-heavy phases.
- **100 GB root is a release contract, not observed usage** — 18 tasks declare
  `volume_size` of 32–100 GB (`validation/volume-requirements.json`), and the bridge fails
  task setup if the live root is undersized. The build gate enforces ≥100 GB on restore.
- **4 vCPU matches upstream's t3.xlarge reference**; measured peak was 1.5 cores with no
  saturation, so this has headroom rather than slack to cut.
- **Fleet swap is required once**: the websites fleet's first-boot compose build (23 images)
  OOM-wedges an 8 GB sandbox without it; launchers add it idempotently.

Both parallel drivers default to and cap `PARALLEL_CONCURRENCY` at 80. This is a
coordinator policy, not the measured E2B account limit. Strict reset creates the replacement
before deleting the old guest, so budget up to `2 × workers + 2` sandboxes with both fleets:
162 at 80 workers, 198 at 98. A September 20 admission probe held **201 simultaneous
sandboxes** successfully; it did not find the account's hard limit. The former 200-sandbox
planning assumption is therefore not an enforced account ceiling.

On `osworld-v2-gnome:be5ffc32-390c-4b67-99c7-c14b42861019`, 80 independent task-030 setup
workers passed, and a separate 98-worker bridge probe passed three strict-reset cycles
(392 guests). Final build `cd4a1a63…` passed task-030 setup and observations in 98/98
processes with no 502s. Eighty-five workers needed retries after accessibility HTTP 500s;
maximum accessibility readiness was 79 seconds. These are environment tests, not 80- or
98-way full-inference benchmarks.
The guest serializes Linux accessibility/terminal reads because concurrent AT-SPI traversal
crashed the previous control server inside GLib. See the [evidence](../out/osworld-v2-evidence/concurrency/verification-20260920.json).

Each worker binds OS-assigned loopback ports; no fixed port range limits worker count.
Optional retry waves use `AGENT_RETRY_CONCURRENCY` (default and cap of four). Host memory,
task-specific resource use, shared fleet load, and inference quotas also constrain useful
concurrency. AWS publishes Opus 5 Mantle defaults of 20M input and 2M output tokens/minute;
Mantle has no requests/minute quota, but capacity throttling can still occur. These are
[published defaults](https://docs.aws.amazon.com/general/latest/gr/bedrock.html), not this
account's verified quotas. [Quota accounting](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-mantle.html)
reserves input plus maximum output on admission and excludes cached input reads. The bearer
key's model-list response exposed no quota values; its free token-counting requests returned
403. An 80-request simultaneous Opus 5 burst completed with 80 HTTP 200 responses
in 2.58 seconds, using tiny text prompts, 32 output tokens, and disabled thinking. This
does not validate 80 long-context agent sessions. No fixed model-concurrency limit was established.

## Layout

- **`template/`** — guest Template source: GNOME under Xvfb + software GL, pinned/held
  application installs, baked first-run-modal suppression, the guest server payload.
- **`provider/`** — `provider.py` (OSWorld provider contract), `bridge.py` (in-process
  sandbox lifecycle + token-authenticated loopback proxy for HTTP, WebSocket and CDP on
  OS-assigned ports), `manager.py`.
- **`runner/`** — pinned-checkout setup and the benchmark driver `run_agent_parallel.sh`
  (default and cap of 80 concurrent workers).
- **`maintainer/`** — verification-ladder scripts, `validate_parallel.sh`, and
  `profile_resources.sh`, which wraps the standalone profiler in `tools/`.
- **`services/`** — website/GitLab fleet launchers; each fleet runs docker-compose inside
  one sandbox, launched per campaign for per-run isolation.
- **`validation/`** — task manifests (10-task sample, full 108-task release) referencing
  gated tasks by id only.

## GitLab CI and Pages

Fleet startup registers one instance runner for untagged Docker jobs, reuses its persistent
configuration on repeat launches, and requires it to be online. Runner API and clone URLs
use the internal fanout on the Compose network so job containers do not resolve the
loopback development hostname to themselves.

Pages requests preserve a validated project hostname beneath the configured GitLab host.
The campaign certificate covers that wildcard; the proxy keeps returned Pages URLs on
HTTPS and the caller's listener port, including the host evaluator's port 8090. API and Git
push checks alone do not verify CI or Pages: a deployment control must also complete a job,
upload its artifact, and retrieve the published page.

## QEMU → E2B mapping

The provider owns an in-process bridge; DesktopEnv sees a VM at 127.0.0.1 on whatever ports
the OS assigned.

| OSWorld provider hook | QEMU semantics | E2B implementation |
| --- | --- | --- |
| `get_vm_path` | path to a `.qcow2` | immutable `GUEST_TEMPLATE` ref, validated fail-closed |
| `start_emulator` | boot the VM | bridge binds loopback listeners, creates the guest, gates on `/screen_size` |
| `get_ip_address` | IP + `server:cdp:vnc:vlc` ports | `127.0.0.1:<server>:<cdp>:0:<vlc>`, ports assigned at bind; VNC slot 0 (headless) |
| `save_state` | named `savevm` | `create_snapshot()` (memory + filesystem); the call returns only after the resumed guest answers |
| `revert_to_snapshot` | in-place `loadvm` | destroy-and-recreate: saved name → new sandbox from its snapshot id; unsaved name (`init_state`) → fresh sandbox from the template |
| `prepare/finalize_volume` | size the root disk | requested GB asserted against the live root (`df`), fail-closed; not an E2B persistent Volume |
| `stop_emulator` | power off | `bridge.stop()` kills the guest |

Key decisions: a fresh template sandbox *is* `init_state` (the start command boots the
desktop fresh, and upstream never explicitly saves one); `setup.sh` patches in **strict
reset** so every task and setup retry gets a unique sandbox; **pause/resume is deliberately
unused** (multi-hour tasks with scheduled events and live CDP sockets don't tolerate clock
jumps) — liveness comes from the timeout heartbeat instead.

## Networking

Every sandbox (guest and fleet) is created through the shared, unit-tested `e2b_policy.py`:
secure envd, authenticated ingress (`allow_public_traffic: false`; the bridge injects the
per-sandbox traffic token, strips it from responses, and binds only `127.0.0.1`), and public
egress with protected ranges denied (RFC1918, link-local/cloud-metadata, CGNAT, benchmark,
multicast, reserved, IPv6 equivalents). `template/build.ts` verifies the applied policy on
the freshly restored build. E2B ingress rejects overridden Host headers, so fleet sites get
one sandbox port each, with Host-mapping proxies routing `<site>.127.0.0.1.nip.io` to the
right ingress port + token (guest port 8080 stays reserved for VLC). `SANDBOX_TIMEOUT_S`
(default 1 h) is an *idle* ceiling: the heartbeat re-arms it while guest traffic flows, so
active multi-hour tasks survive and abandoned guests expire.

## Fleet origins and trust

Fleet origins terminate TLS under a per-campaign CA the coordinator generates in
`services/.campaign-tls/` (`campaign_tls.py`'s `ensure_campaign_tls`): a self-signed CA
(`ca.key`/`ca.crt`) signs a leaf (`leaf.key`/`leaf.crt`) whose SAN list is every fleet
hostname. The CA's private key never leaves the coordinator; the leaf key does, because the
guest's root-owned Host-mapping proxy is the only thing that terminates TLS and needs it to
present the certificate. `ensure_campaign_tls` unions each launcher's own requested hosts with
whatever the persisted `tls` section already covers for the same `campaign_id`, so whichever
launcher (websites or GitLab) runs second only grows the leaf's SAN set and never drops a host
the first launcher already added; a different `campaign_id`, or missing CA material, resets
the whole directory instead.

The host-side proxy (macOS, best-effort — binding `:80` needs elevation there) listens on
whatever `HOSTMAP_PORT` lists (default `8090`) and, once a `tls` runtime section with leaf
material exists, terminates TLS on every one of those ports: `fleetlib.tls_env()` sets
`HOSTMAP_TLS_PORTS` to the same port list as `HOSTMAP_PORT` and `HOSTMAP_TLS_CERT`/
`HOSTMAP_TLS_KEY` to the campaign leaf, because the coordinator never runs a plain listener
once TLS material is available. In practice every fleet URL the coordinator hands the harness
is `https://<site>.127.0.0.1.nip.io:8090`.

The guest-side proxy (installed as root by `provider/bridge.py`'s `_install_guest_proxy` at
session start) binds `80,443,8090`, with TLS on `443` and `8090`; plain `:80` stops proxying
and instead answers every request with a `301` to a portless `https://<host><path>`, landing
the client on the guest's own `:443` with no port carried over. Before a `tls` runtime section
with leaf material exists, the guest falls back to serving `80,8090` in plain HTTP, matching
pre-TLS behavior exactly.

Trust is installed into the guest before Chrome ever starts: the CA cert is copied to
`/usr/local/share/ca-certificates/osworld-campaign.crt` and picked up by
`update-ca-certificates` (system trust store), and separately imported into Chrome's own NSS
database with `certutil -d sql:/home/user/.pki/nssdb -A -t "C,," -n osworld-campaign -i
/usr/local/share/ca-certificates/osworld-campaign.crt` — Chrome on Linux consults NSS, not just
the system store, so both installs are required for `isSecureContext` to be true. That `-i`
reads the installed copy, not `/opt/hostmap-tls/ca.crt`, because `certutil` runs as `user` (it
writes the user-owned NSS database) and `/opt/hostmap-tls` is root-owned mode `0700`, so the
root-only path fails with EACCES and aborts the session — do not "tidy" it back. Neither
install command backgrounds or swallows its exit code, so a missing `certutil`/nssdb or a
failed `update-ca-certificates`
raises rather than silently leaving Chrome untrusting. The guest template ships
`libnss3-tools` (for `certutil`) and a `/home/user/.pki/nssdb` seeded by `certutil -N`. Guest TLS
material (`leaf.crt`, `leaf.key`, `ca.crt`) lives at `/opt/hostmap-tls/`, root-owned,
`leaf.key` additionally mode `0600`, so the agent-controlled `user` account driving Chrome can
never read the private key even though its browser trusts the CA that issued it.

`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`, both pointed at `campaign_tls.py`'s `bundle.crt`
(certifi's public root bundle with the campaign CA appended), keep Python's own HTTPS clients
working against both public endpoints and the campaign-signed fleet — `requests` (upstream's
website-scheme probe, python-gitlab) and any stdlib `ssl` consumer trust both without separate
configuration. `runner/common.sh`'s `export_fleet_wiring` reads `tls.ca_cert`/`tls.bundle` out
of `.runtime.json` into `OSWORLD_CA_CERT`/`OSWORLD_CA_BUNDLE` for the coordinator's own trust
plumbing, then derives `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` from the latter. The only
environment variables `hostmap_proxy.py` itself reads for TLS are `HOSTMAP_TLS_PORTS`,
`HOSTMAP_TLS_CERT`, and `HOSTMAP_TLS_KEY`.

`export_fleet_wiring` also sets `OSWORLD_WEBSITE_SCHEME=https`. The patched upstream
URL builder uses that explicit scheme instead of caching an HTTP fallback when its
HTTPS readiness probe times out. Port 8090 serves TLS only. The host and guest proxies
decode chunked request bodies before forwarding, including large Git pushes.

Task 026 depends on a Hugging Face-hosted zip whose upstream URL is dead. The websites
launcher verifies the pinned local copy against a recorded sha256, uploads it into a
`files.<suffix>` static nginx site (read-only bind mount, `Content-Disposition: attachment`)
added to the fanout alongside the real control-plane sites, and records `{dead_url:
served_url}` in the `websites.asset_url_map` runtime field. `hostmap_proxy.py` rewrites any
occurrence of the dead URL inside `/api/state` request bodies to the fleet-served replacement
before forwarding, so task setup that seeds state with the original Hugging Face link
transparently gets the working one instead.

Task 041 (`tasks/task_041.py:87`) hardcodes a public GitLab host, `54.174.16.65.sslip.io`,
that the harness cannot control. `campaign_tls.py`'s `TASK_041_GITLAB_ALIAS` folds that
hostname unconditionally into every campaign leaf's SAN set, and both the host and guest
hostmap proxies route it as an alias of the real GitLab host (`gitlab.aliases` in
`.runtime.json`) carrying a `canonical_host`, so GitLab's own canonical absolute URLs in
responses get rewritten back to the alias authority the client actually used instead of
leaking the real `gitlab.<suffix>` host.

The `tls` section of `.runtime.json` — required by `preflight.py` before a run can proceed —
carries `campaign_id`, `hosts` (the leaf's full SAN list), `ca_cert`, `leaf_cert`, `leaf_key`,
and `bundle`, all absolute paths. There is no `ca_key` field: `ensure_campaign_tls`
deliberately omits it when writing the section, and `preflight.py` derives the CA key's path
itself (next to `ca_cert`, as `ca.key`) rather than trusting an extra published field, so
nothing downstream of the coordinator can be handed the CA's private key even by mistake.
`preflight.py` also requires both the leaf key and the derived CA key path to be private files
(no group/world access) before a run starts.

`services/stop.py` removes a `websites` or `gitlab` runtime entry with no `campaign_id` (a
legacy entry, from before this plan) only when its recorded sandbox is confirmed gone; if the
sandbox is still running or the liveness check is inconclusive, `stop_campaign` raises rather
than guessing, naming the sandbox to kill manually. On a clean teardown, `remove_campaign_tls`
deletes `services/.campaign-tls/` and the `tls` runtime section along with the rest of the
campaign's state.

Status: everything above is implemented and covered by unit tests against faked sandboxes. The
guest-side half has since been exercised against a live guest on build
`osworld-v2-gnome:00124a57-267c-45e7-93d1-ca0ff196e4b8`. The guest-browser probe
([summary](../out/osworld-v2-evidence/fleet/browser-probe-00124a57-267c-45e7-93d1-ca0ff196e4b8.summary.json))
read `isSecureContext: true`, a `secure` visible security state over TLS 1.3, and no
certificate interstitial from inside the guest's own Chrome on all six fleet origins, which
means the trust install and the guest proxy's TLS termination both worked; priced inference ran
on top of the same build and fleet campaign
([summary](../out/osworld-v2-evidence/live/agent-nine-20260918.summary.json)), nine tasks with
the upstream M3 agent.

That is one build, one fleet campaign and one probe run. The probe drove only the portless
`:443` origins, not the `:8090` origins upstream's URL builder hands tasks, and it loaded only
landing pages; the nine-task run returned a **FAIL** gate and scores at or near zero.
`FIDELITY.md` states what those two runs do and do not establish, and it is the authority on
that, not this file.

## Snapshots

An E2B snapshot captures memory + filesystem, persists independently of its sandbox, and
can seed any number of new sandboxes — the same contract as QEMU's named `savevm` states.
The name→id map lives in the bridge for the DesktopEnv's lifetime; both revert paths are
live-probed (`out/osworld-v2-evidence/snapshot-probe.json`, 12/12). Snapshots are
deleted when the environment closes. Set `OSWORLD_RETAIN_SNAPSHOTS=1` only for an intentional debugging
session, then delete the recorded ids reported by `save_state` or `bridge.state()` when finished.
The capture briefly pauses the guest (dropping CDP sockets), so `save_state` re-gates on
readiness and the bridge retries WebSocket connects during the resume window.
