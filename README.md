# OSWorld 2.0 on E2B

Run OSWorld 2.0 agents and benchmarks on E2B Firecracker sandboxes. This repo adapts the
upstream desktop environment to E2B while keeping OSWorld's tasks, agents, and evaluators.
It includes a desktop template, the E2B provider with its in-process sandbox bridge, service
fleets, and an optional parallel coordinator.

The port is experimental. See the [verified results and known limitations](FIDELITY.md).

## Quick start

Use Bash from the repository root. You need `uv`, Node >=20.18.1, `npm`, `git`, FFmpeg,
ImageMagick, `.env.local` containing `E2B_API_KEY=...`, and access to the gated OSWorld task and asset
datasets. Authenticate with `uv run --locked hf auth login`. See
[resource requirements](docs/runtime.md#resource-requirements) before building.

**1. Build the templates and download OSWorld.**

```bash
template/fetch_server.sh
npm --prefix template ci --ignore-scripts
uv run --env-file .env.local --locked npm --prefix template run typecheck
uv run --env-file .env.local --locked npm --prefix template run build
uv run --env-file .env.local --locked python services/build_fleet_template.py

export GUEST_TEMPLATE="$(python3 -c 'import json; print(json.load(open("template/results/template-build.json"))["reference"])')"
export FLEET_TEMPLATE="$(python3 -c 'import json; print(json.load(open("out/osworld-v2-raw/builds/fleet-template-build.json"))["reference"])')"
runner/setup.sh
uv sync --project OSWorld-V2 --locked --extra full --python 3.12
uv run --locked python runner/gated_data.py
```

**2. Start the services and select tasks.**

```bash
export OSWORLD_CAMPAIGN_ID="osworld-v2-$(date -u +%Y%m%dT%H%M%SZ)"
uv run --env-file .env.local --locked python services/websites/launch.py
uv run --env-file .env.local --locked python services/gitlab/launch.py

RUN_ROOT="$(mktemp -d)"
python3 runner/render_manifest.py --source validation/full-manifest.json \
    --template "$GUEST_TEMPLATE" --output "$RUN_ROOT/full-manifest.json"
```

This selects all 108 tasks. Add `--task-id 003` (repeat for more IDs) to select a subset.

**3. Configure your model and run.**

This example uses Fireworks M3 for the agent and Claude Haiku 4.5 on AWS Bedrock (Mantle)
for judging and user simulation. Export your keys in the shell. Any provider upstream
supports works here; see the
[configuration doc](docs/configuration.md#judge-and-user-simulator-configuration) for OpenAI.

```bash
export MODEL_API_KEY="..."                 # your Fireworks API key
export MODEL_BASE_URL="https://api.fireworks.ai/inference"
export MODEL="accounts/fireworks/models/minimax-m3"
export AGENT_KIND=m3 M3_THINKING_BUDGET=2048 M3_MAX_LLM_RETRIES=0
export OSWORLD_EVAL_MODEL_PROVIDER=anthropic
export OSWORLD_EVAL_MODEL_NAME=anthropic.claude-haiku-4-5
export OSWORLD_EVAL_MODEL_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/anthropic
export OSWORLD_EVAL_MODEL_API_KEY_ENV=AWS_MANTLE
export OSWORLD_USER_SIM_MODEL=anthropic.claude-haiku-4-5
export AWS_MANTLE="..."                    # judge and user simulator
export OPENAI_API_KEY="${OPENAI_API_KEY:-bedrock-legacy-presence-only}" # task 092 presence check

AGENT_MANIFEST="$RUN_ROOT/full-manifest.json" PARALLEL_CONCURRENCY=80 MAX_STEPS=500 \
    RAW_DIR="$RUN_ROOT/agent-raw" \
    OUTPUT="$RUN_ROOT/agent-full.json" runner/run_agent_parallel.sh
```

Concurrency defaults to **80** and is capped at **120**; the per-task deadline defaults to
28800 seconds (8 hours), so it is omitted above. Results are in `$RUN_ROOT/agent-full.json`;
trajectories are in `$RUN_ROOT/agent-raw`. Admitted runs stop their service fleets on exit.
See [run configuration](docs/configuration.md) for cleanup, recording, and result interpretation.
To resume an interrupted or partially-scored run, rerun the same command with
`RESUME_RUN_ID=<the previous RUN_ID>`; tasks that already scored are kept and only unscored
tasks rerun. A resume with no scored tasks reruns everything under a fresh nonce.

To use the pinned upstream Claude agent, select `AGENT_KIND=claude`. It sends native
computer-use tool calls through Anthropic Messages. For Bedrock Mantle, configure:

```bash
export AGENT_KIND=claude MODEL=anthropic.claude-opus-5 MAX_STEPS=500
export SLEEP_AFTER_EXECUTION=0             # pinned Claude launcher default
export MODEL_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/anthropic
export MODEL_API_KEY="..."                 # your Mantle bearer key
```

Use the coordinator command above with these agent settings. Claude uses adaptive max
effort, at least 16,000 output tokens, ten recent images with upstream's chunked removal,
and model-default sampling. The worker's `--max-trajectory-length` controls image retention;
`--temperature` and `--top-p` overrides are rejected. Receipts record the effective token
limit and the run's step budget. Only models with an adaptive-thinking profile in the
pinned agent are accepted; its fixed desktop password is `osworld-public-evaluation`.
Upstream prompts, inference settings, and action parsing are preserved. Setup removes the
obsolete `prompt-caching-2024-07-31` beta header, which Mantle rejects; prompt caching's
`cache_control` fields and the computer-use beta remain. [Anthropic documents that prompt
caching requires no beta header](https://platform.claude.com/docs/en/release-notes/overview#december-17th-2024).
A thin wrapper raises upstream's failure sentinel instead of treating it as a user question. The upstream
500-attempt retry loop remains; use `AGENT_TASK_TIMEOUT_SECONDS` (or the worker's
`--deadline-seconds`) to bound failures. This configuration does not establish reproduction
of the published Opus 5 batch-tool score.

Run `services/stop.py --campaign-id "$OSWORLD_CAMPAIGN_ID" [--dry-run]` to remove exactly
that campaign's fleets and any leftover guests, such as after a hard-killed run.

## Run with upstream's runner

Upstream's scripts work unchanged once `runner/setup.sh` has patched the checkout. The M3
multi-env runner accepts `--provider_name e2b` (one of `setup.sh`'s four one-line patches);
each env process owns its own E2B guest and loopback ports, so `--num_envs` is the only
concurrency knob. Fleets from Quick start step 2 must be running.

Upstream's loader resolves task classes at `OSWorld-V2/evaluation_examples/task_class/`;
`runner/gated_data.py` downloads them to `tasks/` instead, so copy them into the checkout
first (harmless: `setup.sh --verify` compares the checkout's own tracked files against the
pin, and the copied `task_*.py` files are untracked there).
The coordinator also starts the host hostmap proxy for you; on this path start it yourself,
or task setup and evaluators on the host cannot reach `*.127.0.0.1.nip.io:8090`.

```bash
# The same helper the coordinator uses: it only defines variables and functions, and
# exports the whole fleet wiring -- site suffix, GitLab URL and token, asset base, and the
# campaign CA/leaf paths (docs/runtime.md#fleet-origins-and-trust) -- from the runtime file
# the launchers already wrote, so this shell's HTTPS clients and the proxy both trust it.
source runner/common.sh && export_fleet_wiring
cp tasks/task_*.py OSWorld-V2/evaluation_examples/task_class/

HOSTMAP_PORT=8090 HOSTMAP_TLS_PORTS=8090 FLEET_RUNTIME_FILE="$OSWORLD_FLEET_RULES" \
    uv run --python 3.12 --with e2b==2.34.0 python services/hostmap_proxy.py &

cd OSWorld-V2
uv run --locked --extra full --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1 \
    python scripts/python/run_multienv_m3.py \
        --provider_name e2b --num_envs 8 --headless \
        --model accounts/fireworks/models/minimax-m3 \
        --base_url https://api.fireworks.ai/inference --api_key "$MODEL_API_KEY" \
        --client_password osworld-public-evaluation --max_steps 500 \
        --test_all_meta_path evaluation_examples/test_v2.json --result_dir ./results
```

`GUEST_TEMPLATE`, `OSWORLD_CAMPAIGN_ID` and `E2B_API_KEY` come from Quick start. Every sandbox
the run creates carries `OSWORLD_CAMPAIGN_ID` in its metadata, so use a fresh id per run.
Stop the hostmap proxy (kill its backgrounded pid) when the run ends; the coordinator does
both the starting and the stopping for you.

Task 082 dials `localhost:3000` on the host, so it runs in its own single-env invocation:
copy `evaluation_examples/test_v2.json` to a file with the `"082"` entry removed, run
that file as above with `--test_all_meta_path`, then 082 alone with `--num_envs 1
--specific_task_id 082` and `OSWORLD_TASK_SERVICE_PORTS=3000` exported. Without that mapping
the task's setup cannot reach its service and the bridge logs a hint naming the variable.
The coordinator in Quick start step 3 does this carve-out for you and adds a judge probe,
per-task deadlines and receipts; it is optional.

## Use your own agent

Register your agent in [`runner/agents.py`](runner/agents.py), implementing upstream's
`reset()` and `predict(instruction, observation)` interface, then select it with `AGENT_KIND`.
You can also use the included `prompt` agent with an OpenAI-compatible endpoint.
See [agent configuration and examples](docs/configuration.md#bring-your-own-agent).

## Licences you maintain

You build the guest image yourself; this repository redistributes none of these binaries.
The template installs the following components whose terms you are responsible for:

| Component | Version | Licence |
|---|---|---|
| Google Chrome | `153.0.8010.47-1` in build `00124a57-267c-45e7-93d1-ca0ff196e4b8`; each build freezes whatever the Google repo served, recorded in the build's own `template/results/template-build.json` and committed for this build at `out/osworld-v2-evidence/template/template-build-00124a57-267c-45e7-93d1-ca0ff196e4b8.json` | [Google Chrome Terms of Service](https://www.google.com/chrome/terms/) |
| WPS Office for Linux | 11.1.0.11723 | [Kingsoft EULA](https://www.wps.com/eula/) (proprietary) |
| REAPER | 7.79 | [Evaluation licence](https://www.reaper.fm/purchase.php); a paid licence is required for continued use |
| Visual Studio Code | 1.91.1 | [Microsoft Software License](https://code.visualstudio.com/license); Microsoft's `.deb` build is proprietary, not the MIT-licensed `vscode` source |

Open-source components installed from vendor releases or Ubuntu 22.04: MuseScore Studio 4.6.5 (GPL-3.0),
Blender 4.5.14 (GPL-2.0-or-later), KiCad 10.0 (GPL-3.0-or-later, via the KiCad PPA; `apt-get install -y kicad` runs with recommends,
so it also pulls `kicad-libraries` — the symbol, footprint and 3D-model data, which is
[CC-BY-SA-4.0 with the KiCad library exception](https://www.kicad.org/libraries/license/), a
separate obligation from the application's GPL), FreeCAD 1.1.3
(LGPL-2.1), Zotero 7.0.15 (AGPL-3.0), Shotcut (GPL-3.0), OpenBoard (GPL-3.0), LibreOffice (MPL-2.0),
x11vnc (GPL-2.0), noVNC (MPL-2.0), websockify (LGPL-3.0), and task 082's Docker Compose v2 CLI plugin
(Apache-2.0). Also from Ubuntu 22.04's archive: GIMP (GPL-3.0-or-later), VLC (GPL-2.0-or-later),
Thunderbird (MPL-2.0) and Evince (GPL-2.0-or-later), alongside the GNOME desktop, fonts, PulseAudio
and the X/screenshot tooling the evaluators call; every archive package's authoritative terms are its
own `/usr/share/doc/<package>/copyright` file inside the guest. The upstream
guest server (`xlang-ai/osworld-server`) publishes no licence and is fetched at build time, never
redistributed (see `docs/runtime.md`).

## Documentation

- [Agent, judge, and run configuration](docs/configuration.md)
- [Runtime, resources, networking, and snapshots](docs/runtime.md)
- [Maintainer validation](maintainer/README.md)
- [Verification evidence and known issues](FIDELITY.md)
- [Architecture walkthrough](docs/architecture.md)
