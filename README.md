# OSWorld 2.0 on E2B

Run OSWorld 2.0 agents and benchmarks on E2B Firecracker sandboxes. This repo adapts the
upstream desktop environment to E2B while keeping OSWorld's tasks, agents, and evaluators.
It includes a desktop template, the E2B provider with its in-process sandbox bridge, service
fleets, and an optional parallel coordinator.

The port is experimental. See the [verified results and known limitations](docs/pr-1-verification.md).

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

This example uses Fireworks M3 for the agent and OpenAI for judging and user simulation.
Export your keys in the shell. For other providers, including AWS Mantle, see
[model configuration](docs/configuration.md#judge-and-user-simulator-configuration).

```bash
export MODEL_API_KEY="..."                 # your Fireworks API key
export MODEL_BASE_URL="https://api.fireworks.ai/inference"
export MODEL="accounts/fireworks/models/minimax-m3"
export AGENT_KIND=m3 M3_THINKING_BUDGET=2048 M3_MAX_LLM_RETRIES=0
export OPENAI_API_KEY="..."                # judge and user simulator

AGENT_MANIFEST="$RUN_ROOT/full-manifest.json" PARALLEL_CONCURRENCY=80 MAX_STEPS=500 \
    AGENT_TASK_TIMEOUT_SECONDS=28800 RAW_DIR="$RUN_ROOT/agent-raw" \
    OUTPUT="$RUN_ROOT/agent-full.json" runner/run_agent_parallel.sh
```

Concurrency defaults to and is capped at **80**. Results are in `$RUN_ROOT/agent-full.json`;
trajectories are in `$RUN_ROOT/agent-raw`. Admitted runs stop their service fleets on exit.
See [run configuration](docs/configuration.md) for cleanup, recording, and result interpretation.

Run `services/stop.py --campaign-id "$OSWORLD_CAMPAIGN_ID" [--dry-run]` to remove exactly
that campaign's fleets and any leftover guests, such as after a hard-killed run.

## Run with upstream's runner

Upstream's scripts work unchanged once `runner/setup.sh` has patched the checkout. The M3
multi-env runner accepts `--provider_name e2b` (one of `setup.sh`'s four one-line patches);
each env process owns its own E2B guest and loopback ports, so `--num_envs` is the only
concurrency knob. Fleets from Quick start step 2 must be running.

Upstream's loader resolves task classes at `OSWorld-V2/evaluation_examples/task_class/`;
`runner/gated_data.py` downloads them to `tasks/` instead, so copy them into the checkout
first (harmless: the checkout is gitignored, so this doesn't affect `setup.sh --verify`).
The coordinator also starts the host hostmap proxy for you; on this path start it yourself,
or task setup and evaluators on the host cannot reach `*.127.0.0.1.nip.io:8090`.

```bash
export WEBSITE_HOST_SUFFIX=127.0.0.1.nip.io:8090 GITLAB_URL=http://gitlab.127.0.0.1.nip.io:8090
export GITLAB_PRIVATE_TOKEN="$(cat services/.gitlab-token)"
export HOSTMAP_PROXY_SCRIPT="$PWD/services/hostmap_proxy.py" OSWORLD_FLEET_RULES="$PWD/services/.runtime.json"
export OSWORLD_FILE_BASE_URL="$PWD/tasks/assets"
cp tasks/task_*.py OSWorld-V2/evaluation_examples/task_class/

HOSTMAP_PORT=8090 FLEET_RUNTIME_FILE="$PWD/services/.runtime.json" \
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
run the manifest without 082 as above, then 082 alone with `--num_envs 1
--specific_task_id 082` and `OSWORLD_TASK_SERVICE_PORTS=3000` exported. Without that mapping
the task's setup cannot reach its service and the bridge logs a hint naming the variable.
The coordinator in Quick start step 3 does this carve-out for you and adds a judge probe,
per-task deadlines and receipts; it is optional.

## Use your own agent

Register your agent in [`runner/agents.py`](runner/agents.py), implementing upstream's
`reset()` and `predict(instruction, observation)` interface, then select it with `AGENT_KIND`.
You can also use the included `prompt` agent with an OpenAI-compatible endpoint.
See [agent configuration and examples](docs/configuration.md#bring-your-own-agent).

## Documentation

- [Agent, judge, and run configuration](docs/configuration.md)
- [Runtime, resources, networking, and snapshots](docs/runtime.md)
- [Maintainer validation](maintainer/README.md)
- [Verification evidence](FIDELITY.md) and [known issues](docs/pr-1-verification.md)
- [Architecture walkthrough](docs/architecture.md)
