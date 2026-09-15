"""runner/agents.py is the one file a customer edits to run their own agent.

It owns agent construction and the per-agent generation defaults; the runner
only asks it for an agent. Upstream's PromptAgent / M3Agent are stubbed here
because the pinned checkout is not importable from the repo's own venv.
"""

from __future__ import annotations

import importlib
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


def test_shipped_agent_kinds_are_the_upstream_agents(agents):
    assert set(agents.AGENT_KINDS) == {"prompt", "m3", "gpt_response"}


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


def _stub_upstream_packages(root: Path) -> None:
    (root / "mm_agents" / "m3").mkdir(parents=True)
    (root / "mm_agents" / "__init__.py").write_text("")
    (root / "mm_agents" / "agent.py").write_text(
        "class PromptAgent:\n    def __init__(self, **kwargs):\n        pass\n"
    )
    (root / "mm_agents" / "m3" / "__init__.py").write_text(
        "class M3Agent:\n    def __init__(self, **kwargs):\n        pass\n"
    )


def _check_kind(tmp_path: Path, kind: str) -> subprocess.CompletedProcess:
    _stub_upstream_packages(tmp_path)
    return subprocess.run(
        [sys.executable, str(RUNNER / "agents.py"), "--check", kind],
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )


def test_agents_module_check_accepts_a_known_kind(tmp_path):
    result = _check_kind(tmp_path, "m3")
    assert result.returncode == 0, result.stderr


def test_agents_module_check_rejects_an_unknown_kind_naming_the_known_ones(tmp_path):
    result = _check_kind(tmp_path, "custom")
    assert result.returncode != 0
    assert (
        "custom" in result.stderr
        and "m3" in result.stderr
        and "prompt" in result.stderr
    )


def test_agent_runner_exposes_upstream_recording_opt_in():
    # Upstream runners take --enable_recording and pass
    # force_disable_recording=not enable_recording; mirror that instead of
    # hardcoding recording off.
    runner = (RUNNER / "agent_runner.py").read_text()
    assert '"--enable-recording"' in runner
    assert "force_disable_recording=not args.enable_recording" in runner
    assert "force_disable_recording=True" not in runner
    # The receipt attests what the runner actually did, not what the shell exported.
    assert "recording_enabled=args.enable_recording" in runner


@pytest.fixture
def native_responses(monkeypatch):
    """Use the pinned native agent, replacing only the network SDK boundary."""
    requests_sent = []
    client_options = []
    replies = [
        {
            "id": "resp_1",
            "status": "completed",
            "error": None,
            "usage": {"input_tokens": 12, "output_tokens": 8},
            "output": [
                {
                    "type": "computer_call",
                    "call_id": "call_1",
                    "pending_safety_checks": [],
                    "actions": [{"type": "click", "x": 30, "y": 40}],
                }
            ],
        },
        {
            "id": "resp_2",
            "status": "completed",
            "error": None,
            "usage": {"input_tokens": 18, "output_tokens": 5},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "[DONE]"}],
                }
            ],
        },
    ]

    class Client:
        def __init__(self, **kwargs):
            client_options.append(kwargs)
            self.responses = self

        def create(self, **kwargs):
            requests_sent.append(kwargs)
            return replies.pop(0)

    sdk = types.ModuleType("openai")
    sdk.OpenAI = Client
    monkeypatch.setitem(sys.modules, "openai", sdk)
    spec = importlib.util.spec_from_file_location(
        "mm_agents.gpt_response_api", ROOT / "OSWorld-V2/mm_agents/gpt_response_api.py"
    )
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    monkeypatch.setitem(sys.modules, spec.name, native)
    return native, requests_sent, client_options


@pytest.mark.parametrize(
    "endpoint, expected_reasoning",
    [
        ("https://bedrock-mantle.us-east-1.api.aws/openai/v1", {"effort": "medium"}),
        ("https://example.test/v1", {"effort": "medium", "summary": "concise"}),
    ],
)
def test_native_responses_uses_model_credentials_and_retains_computer_call_history(
    agents, native_responses, monkeypatch, endpoint, expected_reasoning
):
    native, requests_sent, client_options = native_responses
    monkeypatch.setenv("MODEL_BASE_URL", endpoint)
    monkeypatch.setenv("MODEL_API_KEY", "agent-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.test/v1")
    env = object()
    settings = agents.agent_settings("gpt_response")
    agent = agents.build_agent(
        "gpt_response",
        model="openai.gpt-5.6-sol",
        settings=settings,
        client_password="pw",
        env=env,
    )
    assert type(agent) is native.GPTResponseAPIAgent
    assert agent.env is env
    agent.reset()
    _, actions = agent.predict("click it", {"screenshot": b"first"})
    assert (
        actions[0]["command"]
        == "import pyautogui\npyautogui.moveTo(30, 40)\npyautogui.click(button='left')"
    )
    _, final_actions = agent.predict("click it", {"screenshot": b"second"})
    assert final_actions == [{"action_type": "DONE", "raw_response": "[DONE]"}]
    assert (
        client_options
        == [
            {
                "base_url": endpoint,
                "api_key": "agent-sentinel",
            }
        ]
        * 2
    )
    assert requests_sent[0]["model"] == "openai.gpt-5.6-sol"
    assert requests_sent[0]["tools"] == [{"type": "computer"}]
    assert requests_sent[0]["reasoning"] == expected_reasoning
    assert requests_sent[0]["max_output_tokens"] == 16384
    assert "temperature" not in requests_sent[0] and "top_p" not in requests_sent[0]
    assert requests_sent[1]["previous_response_id"] == "resp_1"
    assert requests_sent[1]["input"] == [
        {
            "type": "computer_call_output",
            "call_id": "call_1",
            "output": {
                "type": "computer_screenshot",
                "image_url": "data:image/png;base64,c2Vjb25k",
                "detail": "original",
            },
        }
    ]
    assert settings == {
        "max_tokens": 16384,
        "reasoning_effort": "medium",
        "action_space": "pyautogui",
        "observation_type": "screenshot",
    }
    assert os.environ["OPENAI_API_KEY"] == "unrelated-key"
    assert os.environ["OPENAI_BASE_URL"] == "https://unrelated.test/v1"


def test_responses_reasoning_override_is_recorded(agents):
    settings = agents.agent_settings(
        "gpt_response", max_tokens=2048, reasoning_effort="high"
    )
    assert settings["max_tokens"] == 2048
    assert settings["reasoning_effort"] == "high"


@pytest.mark.parametrize("unused", ["temperature", "top_p", "max_trajectory_length"])
def test_responses_rejects_settings_the_native_agent_does_not_apply(agents, unused):
    with pytest.raises(ValueError, match="does not apply"):
        agents.agent_settings("gpt_response", **{unused: 1})
