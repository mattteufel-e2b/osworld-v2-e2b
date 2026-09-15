"""Exercise the real pinned evaluator and Anthropic SDK with HTTP intercepted."""

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "OSWorld-V2/.venv/bin/python"


def test_install_does_not_load_provider_sdks_when_not_selected():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys; sys.path.insert(0, sys.argv[1]); "
                "os.environ.pop('OSWORLD_EVAL_MODEL_PROVIDER', None); "
                "os.environ.pop('OSWORLD_USER_SIM_PROVIDER', None); "
                "from bedrock_bearer import install; install(); "
                "assert 'anthropic' not in sys.modules; "
                "assert 'desktop_env' not in sys.modules"
            ),
            str(ROOT / "runner"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bedrock_bearer_preserves_upstream_payloads_and_usage():
    if not PYTHON.exists():
        pytest.skip("requires the pinned upstream evaluator environment")
    result = subprocess.run(
        [str(PYTHON), "-c", PROBE, str(ROOT / "runner")],
        cwd=ROOT / "OSWorld-V2",
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


PROBE = r"""
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from PIL import Image

sys.path.insert(0, sys.argv[1])
from bedrock_bearer import install
from evaluator_model_calls import EvaluatorModelCallTracker

for name in list(os.environ):
    if name.startswith(("OSWORLD_EVAL_", "OSWORLD_USER_SIM_", "AWS_", "ANTHROPIC_")):
        del os.environ[name]
os.environ.update({
    "AWS_MANTLE": "test-bearer-key",
    "AWS_DEFAULT_REGION": "us-east-1",
    "OPENAI_BASE_URL": "https://unrelated-openai.example/v1",
    "OSWORLD_EVAL_MODEL_PROVIDER": "bedrock_bearer",
    "OSWORLD_EVAL_MODEL_NAME": "global.anthropic.claude-sonnet-4-6",
    "OSWORLD_EVAL_MODEL_API_KEY_ENV": "AWS_MANTLE",
    "OSWORLD_USER_SIM_PROVIDER": "bedrock_bearer",
    "OSWORLD_USER_SIM_MODEL": "global.anthropic.claude-sonnet-4-6",
    "OSWORLD_USER_SIM_API_KEY_ENV": "AWS_MANTLE",
})
install()
install()
from desktop_env.evaluators import model_client
from desktop_env.evaluators.backends import BackendConfig, create_backend
from desktop_env.evaluators.backends.bedrock_backend import BedrockBackend
from desktop_env.user_simulator import LLMUserSimulator

tracker = EvaluatorModelCallTracker()
tracker.install(
    model_client=model_client,
    llm_metrics=SimpleNamespace(generate_text=model_client.generate_text),
    user_simulator=LLMUserSimulator,
)
requests = []
def send(client, request, **kwargs):
    requests.append(request)
    assert request.method == "POST"
    assert str(request.url) == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
        "global.anthropic.claude-sonnet-4-6/invoke"
    )
    assert request.headers["authorization"] == "Bearer test-bearer-key"
    assert "x-amz-date" not in request.headers
    assert "x-amz-security-token" not in request.headers
    body = json.loads(request.content)
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in body
    return httpx.Response(200, request=request, json={
        "id": "test-response", "type": "message", "role": "assistant",
        "model": "global.anthropic.claude-sonnet-4-6",
        "content": [{"type": "text", "text": " YES "}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 17, "output_tokens": 2},
    })

with patch.object(httpx.Client, "send", send), tempfile.TemporaryDirectory() as tmp:
    image_path = Path(tmp) / "image.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    assert model_client.generate_text(
        "Is this red?", image_paths=[str(image_path)],
        options={"max_tokens": 5, "temperature": 0.0}, system="Reply YES or NO.",
    ) == "YES"
    body = json.loads(requests[-1].content)
    assert body["max_tokens"] == 5
    assert body["temperature"] == 0.0
    assert body["system"] == "Reply YES or NO."
    content = body["messages"][0]["content"]
    assert content[-1] == {"type": "text", "text": "Is this red?"}
    assert content[0]["source"]["media_type"] == "image/png"
    image = Image.open(io.BytesIO(base64.b64decode(content[0]["source"]["data"])))
    assert image.getpixel((0, 0)) == (255, 0, 0)
    simulator = LLMUserSimulator({"type": "llm", "model": "gpt-4o", "max_tokens": 256})
    simulator.reset("Answer the question.")
    assert simulator.respond("Are you ready?") == "YES"
    body = json.loads(requests[-1].content)
    assert body["max_tokens"] == 256
    assert body["system"]
    assert all(message["role"] != "system" for message in body["messages"])

for name, expected in {
    "calls": 1, "input_tokens": 17, "output_tokens": 2, "unmeasured_calls": 0,
}.items():
    assert tracker.usage["judge"][name] == expected
assert tracker.usage["simulator"] == tracker.usage["judge"]
assert tracker.successes == 1 and tracker.user_sim_successes == 1
backend = create_backend(BackendConfig(provider="bedrock_bearer", model="raw-id", api_key="key"))
assert type(backend)._generate_once is BedrockBackend._generate_once
assert type(backend)._chat_once is BedrockBackend._chat_once
try:
    create_backend(BackendConfig(provider="bedrock_bearer", model="raw-id", api_key=""))
except ValueError:
    pass
else:
    raise AssertionError("missing bearer key must fail before any HTTP request")
"""
