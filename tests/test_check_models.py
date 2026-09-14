"""Probe the actual upstream interfaces without changing task model options."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from check_models import check_models, simulator_configs  # noqa: E402


def test_selected_simulator_configs_need_no_service_imports(tmp_path):
    for task_id, kind in [("007", "llm"), ("024", "llm"), ("023", "scripted")]:
        (tmp_path / f"task_{task_id}.py").write_text(
            "raise RuntimeError('service not running')\n"
            f"class Task:\n    user_simulator = {{'type': '{kind}', 'model': 'gpt-4o', "
            "'knowledge': PRIVATE_TASK_KNOWLEDGE}\n"
        )
    assert simulator_configs(
        {"tasks": [{"id": t} for t in ("007", "024", "023")]}, tmp_path
    ) == [{"type": "llm", "model": "gpt-4o"}]


def test_probe_uses_small_judge_budget_and_task_simulator_config():
    calls = []

    def generate(prompt, **kwargs):
        calls.append(kwargs)
        return "4827" if kwargs["image_paths"] else "YES"

    class Simulator:
        def __init__(self, config):
            assert config["model"] == "gpt-4o"
            assert config["max_tokens"] == 256
            assert config["knowledge"] != "private task knowledge"

        def respond(self, question):
            return "blue"

    check_models(
        generate,
        Simulator,
        [{"model": "gpt-4o", "max_tokens": 256, "knowledge": "private task knowledge"}],
        "probe.png",
    )
    assert len(calls) == 2
    assert all(c["options"] == {"max_tokens": 5, "temperature": 0.0} for c in calls)
    assert calls[0]["image_paths"] is None
    assert calls[1]["image_paths"] == ["probe.png"]


@pytest.mark.parametrize("answer", ["", " \n", None])
def test_probe_rejects_empty_judge_output(answer):
    with pytest.raises(ValueError, match="empty model response"):
        check_models(lambda *a, **k: answer, None, [], "probe.png")


def test_probe_does_not_swallow_simulator_failure():
    class Simulator:
        def __init__(self, config):
            pass

        def respond(self, question):
            raise RuntimeError("model not found")

    with pytest.raises(RuntimeError, match="model not found"):
        check_models(
            lambda *a, **k: "4827" if k["image_paths"] else "YES",
            Simulator,
            [{"model": "gpt-4o"}],
            "probe.png",
        )


def test_probe_rejects_nonempty_output_that_does_not_read_the_image():
    def generate(prompt, **kwargs):
        return "No image attached" if kwargs["image_paths"] else "YES"

    with pytest.raises(ValueError, match="judge image check"):
        check_models(generate, None, [], "probe.png")
