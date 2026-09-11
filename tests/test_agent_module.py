"""runner/agents.py is the one file a customer edits to run their own agent.

It owns agent construction and the per-agent generation defaults; the runner
only asks it for an agent. Upstream's PromptAgent / M3Agent are stubbed here
because the pinned checkout is not importable from the repo's own venv.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner"


class _Recorder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def agents(monkeypatch):
    """Import runner/agents.py against stubbed upstream agent classes."""
    prompt_module = types.ModuleType("mm_agents.agent")
    prompt_module.PromptAgent = type("PromptAgent", (_Recorder,), {})
    m3_module = types.ModuleType("mm_agents.m3")
    m3_module.M3Agent = type("M3Agent", (_Recorder,), {})
    package = types.ModuleType("mm_agents")
    package.agent = prompt_module
    package.m3 = m3_module
    monkeypatch.setitem(sys.modules, "mm_agents", package)
    monkeypatch.setitem(sys.modules, "mm_agents.agent", prompt_module)
    monkeypatch.setitem(sys.modules, "mm_agents.m3", m3_module)
    monkeypatch.syspath_prepend(str(RUNNER))
    monkeypatch.delitem(sys.modules, "agents", raising=False)
    module = importlib.import_module("agents")
    yield module
    sys.modules.pop("agents", None)


def test_shipped_agent_kinds_are_the_upstream_prompt_and_m3_agents(agents):
    assert set(agents.AGENT_KINDS) == {"prompt", "m3"}


def test_prompt_settings_default_to_upstream_run_py_values(agents):
    settings = agents.agent_settings("prompt")
    assert settings == {
        "max_tokens": 1500,
        "top_p": 0.9,
        "temperature": 1.0,
        "max_trajectory_length": 3,
        "action_space": "pyautogui",
        "observation_type": "screenshot",
    }


def test_m3_settings_default_to_upstream_m3_runner_values(agents):
    settings = agents.agent_settings("m3")
    assert settings == {
        "max_tokens": 8192,
        "top_p": None,
        "temperature": 0.6,
        "max_trajectory_length": 10,
        "action_space": "pyautogui",
        "observation_type": "screenshot",
    }


def test_explicit_generation_settings_override_defaults_only_when_given(agents):
    settings = agents.agent_settings(
        "prompt",
        max_tokens=4096,
        temperature=0.2,
        top_p=None,
        max_trajectory_length=None,
    )
    assert settings["max_tokens"] == 4096
    assert settings["temperature"] == 0.2
    assert settings["top_p"] == 0.9
    assert settings["max_trajectory_length"] == 3


def test_unknown_agent_kind_is_rejected(agents):
    with pytest.raises(ValueError, match="custom"):
        agents.agent_settings("custom")


def test_prompt_agent_is_built_from_settings_and_routes_to_model_env(
    agents, monkeypatch
):
    monkeypatch.setenv("MODEL_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("MODEL_API_KEY", "sentinel")
    settings = agents.agent_settings("prompt", max_tokens=2000)
    agent = agents.build_agent(
        "prompt", model="provider/model", settings=settings, client_password="pw"
    )
    assert isinstance(agent, sys.modules["mm_agents.agent"].PromptAgent)
    assert agent.kwargs == {
        "model": "provider/model",
        "max_tokens": 2000,
        "top_p": 0.9,
        "temperature": 1.0,
        "max_trajectory_length": 3,
        "action_space": "pyautogui",
        "observation_type": "screenshot",
        "client_password": "pw",
    }
    # The OpenAI-compatible transport override is the example customization.
    assert type(agent).__name__ == "CompatiblePromptAgent"
    assert callable(getattr(agent, "call_llm"))


def test_m3_agent_is_built_with_model_env_transport(agents, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.fireworks.ai/inference")
    monkeypatch.setenv("MODEL_API_KEY", "sentinel")
    settings = agents.agent_settings("m3", temperature=0.3)
    agent = agents.build_agent(
        "m3",
        model="accounts/fireworks/models/minimax-m3",
        settings=settings,
        client_password="pw",
    )
    assert isinstance(agent, sys.modules["mm_agents.m3"].M3Agent)
    assert agent.kwargs == {
        "model": "accounts/fireworks/models/minimax-m3",
        "base_url": "https://api.fireworks.ai/inference",
        "api_key": "sentinel",
        "platform": "ubuntu",
        "max_tokens": 8192,
        "top_p": None,
        "temperature": 0.3,
        "max_trajectory_length": 10,
        "action_space": "pyautogui",
        "observation_type": "screenshot",
        "coordinate_type": "relative",
        "client_password": "pw",
    }


def test_agent_runner_delegates_agent_construction_to_the_editable_module():
    runner = (RUNNER / "agent_runner.py").read_text()
    assert "from agents import" in runner
    assert "build_agent(" in runner
    assert "agent_settings(" in runner
    assert "M3Agent(" not in runner
    assert "CompatiblePromptAgent" not in runner
    assert 'choices=("prompt", "m3")' not in runner
    # The resolved generation settings are part of the run configuration.
    assert 'receipt["agent_settings"]' in runner or "agent_settings=" in runner


def test_agent_runner_exposes_upstream_generation_flags():
    runner = (RUNNER / "agent_runner.py").read_text()
    for flag in ("--max-tokens", "--temperature", "--top-p", "--max-trajectory-length"):
        assert f'"{flag}"' in runner, flag


def test_timeout_receipt_accepts_any_agent_kind(tmp_path):
    output = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER / "write_timeout_receipt.py"),
            "--output",
            str(output),
            "--task-id",
            "001",
            "--domain",
            "test",
            "--port-base",
            "900",
            "--model",
            "provider/model",
            "--agent-kind",
            "custom",
            "--max-steps",
            "3",
            "--timeout-seconds",
            "1",
            "--wall-clock-seconds",
            "1.5",
        ],
        env={**os.environ, "OSWORLD_RUN_NONCE": "nonce"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["agent_kind"] == "custom"
