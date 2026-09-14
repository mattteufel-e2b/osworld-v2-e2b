"""Probe the actual upstream interfaces without changing task model options."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from check_models import check_models, simulator_configs  # noqa: E402


def _verdict(text):
    # mirror of upstream _extract_verdict_and_explanation
    t = (text or "").strip()
    parts = t.split(None, 1)
    first = "".join(ch for ch in parts[0] if ch.isalpha()).upper() if parts else ""
    return ("YES" if first == "YES" else "NO"), (
        parts[1].strip() if len(parts) > 1 else ""
    )


def _probe(text_answer, image_answer):
    def generate(prompt, image_paths=None, options=None, system=None):
        return image_answer if image_paths else text_answer

    class Sim:
        def __init__(self, config):
            pass

        def respond(self, question):
            return "blue"

    return generate, Sim


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
        parse_verdict=_verdict,
    )
    assert len(calls) == 2
    assert all(c["options"] == {"max_tokens": 5, "temperature": 0.0} for c in calls)
    assert calls[0]["image_paths"] is None
    assert calls[1]["image_paths"] == ["probe.png"]


@pytest.mark.parametrize("answer", ["", " \n", None])
def test_probe_rejects_empty_judge_output(answer):
    with pytest.raises(ValueError, match="empty model response"):
        check_models(
            lambda *a, **k: answer, None, [], "probe.png", parse_verdict=_verdict
        )


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
            parse_verdict=_verdict,
        )


def test_probe_rejects_nonempty_output_that_does_not_read_the_image():
    def generate(prompt, **kwargs):
        return "No image attached" if kwargs["image_paths"] else "YES"

    with pytest.raises(ValueError, match="judge image check"):
        check_models(generate, None, [], "probe.png", parse_verdict=_verdict)


@pytest.mark.parametrize("text", ["YES", "YES.", '"YES"', "Yes, two plus two is four"])
def test_probe_admits_punctuated_and_explained_yes(text):
    generate, Sim = _probe(text, "4827")
    check_models(generate, Sim, [], "probe.png", parse_verdict=_verdict)


@pytest.mark.parametrize("digits", ["4827", "4827.", "The digits are 4827"])
def test_probe_admits_digits_with_surrounding_text(digits):
    generate, Sim = _probe("YES", digits)
    check_models(generate, Sim, [], "probe.png", parse_verdict=_verdict)


@pytest.mark.parametrize("text", ["NO", "NO. YES", "Maybe YES", "I think so"])
def test_probe_rejects_wrong_or_ambiguous_text_verdict(text):
    generate, Sim = _probe(text, "4827")
    with pytest.raises(ValueError, match="judge text check"):
        check_models(generate, Sim, [], "probe.png", parse_verdict=_verdict)


@pytest.mark.parametrize("digits", ["1234", "4827 or 4828", "48 27 1"])
def test_probe_rejects_wrong_or_ambiguous_digits(digits):
    generate, Sim = _probe("YES", digits)
    with pytest.raises(ValueError, match="judge image check"):
        check_models(generate, Sim, [], "probe.png", parse_verdict=_verdict)


def test_text_probe_carries_upstreams_binary_system_prompt():
    calls = []

    def generate(prompt, image_paths=None, options=None, system=None):
        calls.append(system)
        return "4827" if image_paths else "YES"

    class Sim:
        def __init__(self, config):
            pass

        def respond(self, question):
            return "blue"

    check_models(
        generate, Sim, [], "probe.png", parse_verdict=_verdict, binary_system="SYS"
    )
    assert calls[0] == "SYS" and calls[1] is None
