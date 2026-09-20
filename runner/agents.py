"""Agent construction for OSWorld-V2 on E2B -- edit this file to run your own agent.

This is the only place the runner constructs an agent. The rest of the runner
(the provider's in-process bridge and sandbox lifecycle, upstream
``run_single_example`` loop, receipts) never looks inside the agent, so bringing
your own is upstream's documented workflow:

  1. Implement upstream's agent interface: ``reset(runtime_logger=None)`` and
     ``predict(instruction, observation) -> (response, actions)``, plus
     ``action_space`` / ``observation_type`` attributes. Prompts, model calls,
     memory and context management live inside that class.
  2. Add a builder for it to ``AGENT_KINDS`` below, and its generation defaults
     to ``_GENERATION_DEFAULTS`` so receipts record what it actually ran with.
     Keep the class under ``runner/`` (already on ``sys.path``), not inside the
     ``OSWorld-V2/`` checkout, which ``setup.sh --restore`` resets.
  3. Launch with ``AGENT_KIND=<your name>``; ``MODEL`` is passed through verbatim.

The shipped kinds are upstream's own agents from the pinned checkout:

  prompt  ``mm_agents.agent.PromptAgent`` routed at an OpenAI-compatible
          chat-completions endpoint (``MODEL_BASE_URL`` + ``MODEL_API_KEY``).
  m3      ``mm_agents.m3.M3Agent`` (MiniMax-M3; Anthropic Messages transport).
          ``M3_THINKING_MODE`` / ``M3_THINKING_BUDGET`` / ``M3_MAX_LLM_RETRIES``
          are read by the upstream agent itself.
  claude  ``mm_agents.anthropic.main.AnthropicAgent`` with native computer-use
          actions over Anthropic Messages, including Bedrock Mantle endpoints.
          A thin subclass raises upstream's failure sentinel as an exception.

Generation defaults mirror upstream's ``run.py`` (prompt) and
``scripts/python/run_multienv_m3.py`` (m3); the runner's ``--max-tokens``,
``--temperature``, ``--top-p`` and ``--max-trajectory-length`` flags override
them. Claude uses adaptive max effort, at least 16000 output tokens and native
``claude_computer_use`` action dictionaries. Its image-retention override maps
to ``only_n_most_recent_images``; sampling overrides are rejected because Opus 5
does not support them. All agents observe screenshots. No secrets are logged.
"""

from __future__ import annotations

import os
import sys
import time

import requests
from mm_agents.agent import PromptAgent
from mm_agents.m3 import M3Agent


_GENERATION_DEFAULTS = {
    "prompt": {
        "max_tokens": 1500,
        "top_p": 0.9,
        "temperature": 1.0,
        "max_trajectory_length": 3,
    },
    "m3": {
        "max_tokens": 8192,
        "top_p": None,
        "temperature": 0.6,
        "max_trajectory_length": 10,
    },
    "claude": {
        "max_tokens": 16000,
        "top_p": None,
        "temperature": None,
        "only_n_most_recent_images": 10,
        "effort": "max",
    },
}
_INTERACTION = {"action_space": "pyautogui", "observation_type": "screenshot"}


class CompatiblePromptAgent(PromptAgent):
    """Reference PromptAgent routed at an OpenAI-compatible endpoint.

    Upstream's ``call_llm`` picks the backend from the model-name prefix, so a
    provider slug such as ``openai/gpt-4o`` never reaches ``OPENAI_BASE_URL``.
    This subclass reuses the parent's prompt construction, screenshot encoding
    and action parsing verbatim; only ``call_llm`` is overridden to honor
    MODEL_BASE_URL / MODEL_API_KEY unconditionally, send the model slug as given,
    and retry rate limits / 5xx. It is the example of the kind of change a
    custom agent makes. No secrets are logged.
    """

    def call_llm(self, payload):  # noqa: D401
        base_url = os.environ["MODEL_BASE_URL"].rstrip("/")
        api_url = base_url + (
            "/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['MODEL_API_KEY']}",
        }
        last_status = None
        for attempt in range(8):
            try:
                response = requests.post(
                    api_url, headers=headers, json=payload, timeout=180
                )
            except requests.RequestException as exc:
                print(
                    f"[agent] LLM transport error (attempt {attempt}): {exc}",
                    file=sys.stderr,
                )
                time.sleep(min(30, 5 * (attempt + 1)))
                continue
            last_status = response.status_code
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            if response.status_code == 400:
                body = (
                    response.json()
                    if response.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                code = (body.get("error") or {}).get("code")
                if code == "context_length_exceeded":
                    payload["messages"] = [payload["messages"][0]] + payload[
                        "messages"
                    ][-1:]
                    continue
                print(
                    f"[agent] LLM 400 (non-retryable): {str(response.text)[:200]}",
                    file=sys.stderr,
                )
                return ""
            # 429 / 5xx: back off and retry.
            print(
                f"[agent] LLM status {response.status_code} (attempt {attempt}); retrying",
                file=sys.stderr,
            )
            time.sleep(min(45, 8 * (attempt + 1)))
        print(
            f"[agent] LLM exhausted retries (last status {last_status})",
            file=sys.stderr,
        )
        return ""


def build_prompt_agent(model: str, settings: dict, client_password: str):
    return CompatiblePromptAgent(
        model=model, **settings, client_password=client_password
    )


def build_m3_agent(model: str, settings: dict, client_password: str):
    return M3Agent(
        model=model,
        base_url=os.environ["MODEL_BASE_URL"],
        api_key=os.environ["MODEL_API_KEY"],
        platform="ubuntu",
        coordinate_type="relative",
        **settings,
        client_password=client_password,
    )


def build_claude_agent(model: str, settings: dict, client_password: str):
    # Workers run in separate processes. Upstream creates its own SDK client on
    # each predict(); route that unchanged client via its supported env vars.
    from mm_agents.anthropic.main import AnthropicAgent
    from mm_agents.anthropic.utils import APIProvider, get_claude_runtime_profile

    if client_password != "osworld-public-evaluation":
        raise ValueError(
            "claude requires the upstream desktop password 'osworld-public-evaluation'"
        )
    if get_claude_runtime_profile(model).thinking_mode != "adaptive":
        raise ValueError(
            f"claude model {model!r} has no adaptive-thinking profile in the pinned upstream agent"
        )

    class NativeClaudeAgent(AnthropicAgent):
        def predict(self, *args, **kwargs):
            response, actions = super().predict(*args, **kwargs)
            if response is None:
                raise RuntimeError("Claude prediction failed; see the raw agent logs")
            return response, actions

    os.environ["ANTHROPIC_BASE_URL"] = os.environ["MODEL_BASE_URL"]
    os.environ["ANTHROPIC_AUTH_TOKEN"] = os.environ["MODEL_API_KEY"]
    return NativeClaudeAgent(
        model=model,
        api_key=os.environ["MODEL_API_KEY"],
        # Mantle accepts Anthropic Messages with bearer auth, not the SDK's
        # SigV4/Bedrock Runtime transport selected by APIProvider.BEDROCK.
        provider=APIProvider.ANTHROPIC,
        platform="Ubuntu",
        screen_size=(1920, 1080),
        **settings,
    )


# AGENT_KIND -> builder(model, settings, client_password). Add yours here.
AGENT_KINDS = {
    "prompt": build_prompt_agent,
    "m3": build_m3_agent,
    "claude": build_claude_agent,
}


def agent_settings(
    kind: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_trajectory_length: int | None = None,
    max_steps: int = 500,
) -> dict:
    """Resolve the generation settings for ``kind``: upstream defaults, with any
    explicitly given value overriding. The result is what the agent is built
    from and what the receipt records."""
    if kind not in AGENT_KINDS:
        raise ValueError(
            f"unknown AGENT_KIND {kind!r}; known kinds: {sorted(AGENT_KINDS)}"
        )
    settings = dict(_GENERATION_DEFAULTS.get(kind, {}))
    if kind == "claude":
        if temperature is not None or top_p is not None:
            raise ValueError(
                "claude uses model-default sampling; temperature/top_p overrides are unsupported"
            )
        settings["max_tokens"] = max(
            16000, max_tokens if max_tokens is not None else 16000
        )
        if max_trajectory_length is not None:
            settings["only_n_most_recent_images"] = max_trajectory_length
        settings.update(
            max_steps=max_steps,
            action_space="claude_computer_use",
            observation_type="screenshot",
        )
        return settings
    overrides = {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "max_trajectory_length": max_trajectory_length,
    }
    settings.update({k: v for k, v in overrides.items() if v is not None})
    settings.update(_INTERACTION)
    return settings


def build_agent(kind: str, *, model: str, settings: dict, client_password: str):
    return AGENT_KINDS[kind](model, settings, client_password)


if __name__ == "__main__":
    # `python agents.py --check KIND`: the coordinator rejects an unknown
    # AGENT_KIND here, before any guest sandbox is admitted or created.
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", required=True, metavar="AGENT_KIND")
    kind = parser.parse_args().check
    if kind not in AGENT_KINDS:
        raise SystemExit(
            f"AGENT_KIND {kind!r} is not defined in runner/agents.py "
            f"(known kinds: {', '.join(sorted(AGENT_KINDS))})"
        )
