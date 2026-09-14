# Architecture: one OSWorld 2.0 task on E2B

OSWorld's shape is kept: the desktop VM is the thing under test, and the agent, task setup,
and evaluators run *outside* it as a client, seeing only screenshots and sending only
mouse/keyboard actions. This port swaps the VM for E2B sandboxes and adds a localhost relay
because E2B exposes sandbox ports as per-sandbox HTTPS hostnames rather than an IP with open
ports. Everything below is what actually runs for a real task, e.g. a task that opens a mocked
website in Chrome and edits a document in LibreOffice.

## What is hosted where

```mermaid
flowchart LR
    subgraph HOST["Runner host (any Linux box or a coordinator sandbox) — one process set per task worker"]
        direction TB
        COORD["run_agent_parallel.sh<br/>one worker per task, up to 80"]
        WORKER["run_agent.sh (worker for task N)"]
        RUNNER["agent_runner.py<br/>DesktopEnv(provider=e2b)<br/>lib_run_single agent loop (upstream)"]
        AGENT["Agent (upstream PromptAgent / M3)<br/>predict(screenshot) → pyautogui code"]
        EVAL["Upstream evaluators + setup controller<br/>ffprobe / identify / OCR on host"]
        RELAY["e2b_relay.py (one per worker)<br/>127.0.0.1:14999 control<br/>:15000 → guest 5000 (server)<br/>:19222 → guest 9222 (CDP)<br/>:18080 → guest 8080 (VLC)<br/>(+500 × slot)"]
        HPROXY["hostmap_proxy.py<br/>127.0.0.1:8090<br/>Host: site.127.0.0.1.nip.io → fleet port"]
        COORD --> WORKER --> RUNNER
        RUNNER --> AGENT
        RUNNER --> EVAL
        RUNNER -- "http://127.0.0.1:15000/screenshot, /execute, /file, /setup/upload ..." --> RELAY
        RUNNER -- "CDP ws://127.0.0.1:19222" --> RELAY
        EVAL -- "http://site.127.0.0.1.nip.io:8090/api/..." --> HPROXY
    end

    subgraph E2B["E2B (Firecracker sandboxes, all created with secure envd + egress policy + campaign metadata)"]
        direction TB
        subgraph GUEST["Guest desktop sandbox — one fresh sandbox per task attempt (osworld-v2-gnome template, 4 vCPU / 8 GB)"]
            SERVER["osworld-server :5000 (upstream, patched)<br/>screenshot, pyautogui execute, file I/O,<br/>a11y tree, ffmpeg recording"]
            DESK["GNOME on Xvfb 1920×1080<br/>Chrome (CDP :9222), LibreOffice, GIMP, VLC :8080, ..."]
            GPROXY["hostmap_proxy.py (installed by relay)<br/>:80 and :8090 inside guest"]
            SERVER --- DESK
            DESK -- "Chrome loads http://teamchat.127.0.0.1.nip.io:8090<br/>(nip.io → 127.0.0.1 inside guest)" --> GPROXY
        end
        subgraph WEB["Websites fleet sandbox — one per campaign (osworld-v2-fleet-base template + Docker)"]
            FANOUT["nginx fanout<br/>one sandbox port per site (13001, 13002, ...)"]
            CADDY["Caddy → Task-Web/OSWorld-web containers<br/>TeamChat, CloudCRM, MailHub, StreamView, ..."]
            FANOUT --> CADDY
        end
        subgraph GL["GitLab fleet sandbox — one per campaign"]
            GITLAB["gitlab-ce 18.7.0 + runner (Task-Web/gitlab)"]
        end
    end

    subgraph EXT["Third parties (reached from the host only)"]
        MODEL["Agent model endpoint<br/>(OpenAI-compatible or Anthropic Messages)"]
        JUDGE["Judge + user-simulator LLM"]
        HF["Hugging Face gated datasets<br/>108 tasks + assets (hash-verified)"]
        E2BAPI["E2B API (sandbox create/kill/snapshot)"]
    end

    RELAY -- "https://5000-{sbx}.e2b.app + e2b-traffic-access-token" --> SERVER
    RELAY -- "https://9222-{sbx}.e2b.app (WebSocket)" --> DESK
    RELAY -- "SDK: Sandbox.create / kill / create_snapshot / set_timeout" --> E2BAPI
    HPROXY -- "https://13001-{web-sbx}.e2b.app + token" --> FANOUT
    HPROXY -- "https://{port}-{gitlab-sbx}.e2b.app + token" --> GITLAB
    GPROXY -- "https://13001-{web-sbx}.e2b.app + token" --> FANOUT
    GPROXY -- "https://{port}-{gitlab-sbx}.e2b.app + token" --> GITLAB
    AGENT --> MODEL
    EVAL --> JUDGE
    EVAL -- "task assets at setup" --> HF
```

Reading the diagram:

- **Nothing on the host listens beyond 127.0.0.1.** The relay and the host proxy bind loopback
  only. The E2B API key and every sandbox traffic token live in the relay and proxies, never in
  the guest, so an agent with a terminal cannot read them.
- **The guest never sees the host.** It only sees inbound requests on its own server port and
  CDP port arriving via E2B ingress. Egress from every sandbox denies RFC1918, link-local and
  cloud-metadata ranges (`e2b_policy.py`).
- **Mocked websites are reached by name from two places.** Host-side setup and evaluator code
  goes through the host proxy on :8090. The browser inside the guest resolves
  `<site>.127.0.0.1.nip.io` to loopback and hits the guest-side copy of the same proxy. Both
  proxies map the site name to the fleet sandbox's per-site port because E2B ingress rejects an
  overridden `Host` header, which is why the fleet publishes one port per site.
- **One task attempt = one guest sandbox.** Strict reset (a one-line patch in `setup.sh`)
  means every `env.reset()` and every setup retry destroys the guest and creates a new one from
  the immutable template, so no state leaks between tasks or attempts.
- **Fleets live for the campaign, not the task.** The websites and GitLab sandboxes are
  launched once per `OSWORLD_CAMPAIGN_ID`, shared by all workers, and killed by
  `services/stop.py` at the end.

## One real task, end to end

```mermaid
sequenceDiagram
    autonumber
    participant W as run_agent.sh (worker)
    participant RL as e2b_relay.py (127.0.0.1)
    participant E as E2B API
    participant G as Guest sandbox<br/>(osworld-server :5000, Chrome :9222)
    participant R as agent_runner.py<br/>DesktopEnv + lib_run_single
    participant A as Agent
    participant M as Model endpoint
    participant P as hostmap proxies<br/>(host :8090 / guest :80,:8090)
    participant F as Websites fleet sandbox
    participant J as Judge LLM

    Note over W: preflight already passed: host tools, keys, live judge probe, fleet lifetime

    W->>RL: start relay (PORT_BASE = 500 × slot)
    RL->>E: Sandbox.create(GUEST_TEMPLATE, timeout, secure, network policy, metadata)
    E-->>RL: sandbox id + traffic token
    RL->>G: poll GET /health via https://5000-{sbx}.e2b.app until ready
    RL->>G: upload hostmap_proxy.py + fleet_runtime.json, start guest proxy (:80, :8090)
    W-->>W: /health on 127.0.0.1:14999 answers → relay ready

    W->>R: start agent_runner.py --task-id N
    R->>R: DesktopEnv(provider_name="e2b") → provider.get_ip_address() = 127.0.0.1:15000:19222:0:18080
    R->>RL: env.reset(task) → provider.revert_to_snapshot("init_state") → POST /reset
    RL->>E: create new sandbox, kill old one (strict reset)
    RL->>G: wait ready, reinstall guest proxy
    RL-->>R: new sandbox id, generation

    Note over R,G: upstream setup controller runs the task config unchanged
    R->>RL: POST /setup/upload (task files from gated assets)
    RL->>E: sandbox.files.write(...)
    R->>RL: POST /execute (launch LibreOffice / open Chrome)
    RL->>G: proxied to :5000/execute
    R->>RL: CDP ws://127.0.0.1:19222 → open tab http://teamchat.127.0.0.1.nip.io:8090/...
    RL->>G: WebSocket to https://9222-{sbx}.e2b.app
    G->>P: Chrome resolves nip.io → 127.0.0.1, hits guest proxy
    P->>F: https://13001-{web-sbx}.e2b.app + e2b-traffic-access-token
    F-->>G: TeamChat page renders
    opt ENABLE_RECORDING=1
        R->>RL: POST /start_recording → guest ffmpeg
    end

    loop until DONE / FAIL / max steps (default 500)
        R->>RL: GET /screenshot
        RL->>G: proxied, RL re-arms the sandbox timeout while traffic flows
        G-->>R: 1920×1080 PNG
        R->>A: agent.predict(instruction, screenshot)
        A->>M: chat / messages request
        M-->>A: response with pyautogui code (or ASK_USER / DONE / FAIL)
        A-->>R: actions
        alt ASK_USER
            R->>J: user simulator answers (upstream LLMUserSimulator)
        else pyautogui action
            R->>RL: POST /execute (python pyautogui code)
            RL->>G: mouse / keyboard on the desktop
        end
        R->>R: append traj.jsonl, save step PNG (raw dir, gitignored)
    end

    Note over R: env.evaluate() — upstream metrics run on the host
    R->>RL: POST /file (pull the saved document), /accessibility, etc.
    RL->>G: proxied
    R->>P: GET http://teamchat.127.0.0.1.nip.io:8090/api/state
    P->>F: fetch site state through fleet ingress
    opt task uses an LLM judge
        R->>J: judge call with rubric (upstream llm_metrics)
    end
    R->>R: write result.txt (score) — receipt records ids, path status, score only

    R->>RL: env.close() → POST /stop
    RL->>E: sandbox.kill(), delete snapshots
    W-->>W: exit, coordinator aggregates receipts → campaign gate PASS / FAIL
```

Notes on the sequence:

- **Steps 1–6 and 9–12 are E2B-specific.** Everything between them is unchanged upstream
  code: the setup controller, `lib_run_single.run_single_example`, the agents under
  `mm_agents/`, the evaluators, the judge and user-simulator clients.
- **The relay is transparent to DesktopEnv.** DesktopEnv believes it is talking to a VM at
  127.0.0.1 with the server, CDP and VLC ports the provider reported. The relay forwards each
  request to the right `https://{port}-{sandbox}.e2b.app` host, injects the per-sandbox
  traffic token, and strips it from responses.
- **Snapshots map to E2B snapshots.** If the loop calls `save_state`, the relay captures a
  memory + filesystem snapshot; a later `revert_to_snapshot(name)` creates a fresh sandbox from
  it. The default `init_state` is simply a new sandbox from the template.
- **Timeouts.** `SANDBOX_TIMEOUT_S` (default 1 h) is an idle ceiling that the relay re-arms
  while guest traffic flows, so a multi-hour task survives and an abandoned guest expires.
- **Recording** lands as `recording.mp4` next to the trajectory in the raw result directory.
  **Live view / human takeover (VNC)** is not built yet; see
  [issue #2](https://github.com/mattteufel-e2b/osworld-v2-e2b/issues/2).

## Where a customer runs the host side

The only hard constraint is that the relay and DesktopEnv share a machine (they talk over
127.0.0.1). Any Linux VM works, including an E2B sandbox used as the coordinator. Host needs:
`uv` (Python 3.12), `ffmpeg` and `imagemagick` for the evaluators, outbound HTTPS to E2B, the
model and judge endpoints, Hugging Face, and GitHub. Node 20 is needed only to rebuild the
guest template. The customer's laptop is not required.

## Pinned inputs

All in `examples/osworld-v2/upstream.lock.json`:

| Input | Pin | How it arrives |
|---|---|---|
| `xlang-ai/OSWorld-V2` | commit `d578d2d` | `runner/setup.sh` clones it (gitignored), copies in provider + relay + policy, applies 3 one-line patches |
| `xlang-ai/osworld-server` | commit `a3cc3f0` | `template/fetch_server.sh` downloads, patches, bakes into the guest template (never redistributed) |
| Guest base image | `ubuntu:22.04@sha256:…` | `template/template.ts` via the E2B Template SDK |
| Fleet base image | `debian:bookworm@sha256:…` + Docker | `services/build_fleet_template.py` |
| `Task-Web/OSWorld-web`, `Task-Web/gitlab` | commits in lock | cloned inside the fleet sandboxes; service images pulled by digest |
| Tasks + assets | HF dataset revisions in lock | `runner/gated_data.py` downloads with the user's HF token, verifies `task-hashes.json` |
