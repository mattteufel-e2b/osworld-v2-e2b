from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tracker_module():
    path = ROOT / "runner" / "evaluator_model_calls.py"
    assert path.is_file(), "evaluator model-call tracker is missing"
    spec = importlib.util.spec_from_file_location("evaluator_model_calls", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tracker_class():
    return _tracker_module().EvaluatorModelCallTracker


def test_evaluator_model_tracker_counts_attempts_and_only_successful_returns():
    tracker = _tracker_class()()

    def success(*_args, **_kwargs):
        return "answer"

    def failure(*_args, **_kwargs):
        raise RuntimeError("transport failed")

    model_client = SimpleNamespace(generate_text=success, generate_chat=failure)
    llm_metrics = SimpleNamespace(generate_text=success)
    tracker.install(model_client=model_client, llm_metrics=llm_metrics)

    # Every call routed through the installed model helpers counts, wherever
    # it happens -- env.evaluate, a multiphase phase["evaluate"](env), or a
    # metric helper -- there is no depth gate that only counts calls made
    # while some wrapped "evaluate" frame is on the stack.
    assert model_client.generate_text("prompt") == "answer"

    def evaluate():
        assert model_client.generate_text("prompt") == "answer"
        with pytest.raises(RuntimeError, match="transport failed"):
            model_client.generate_chat([])
        assert llm_metrics.generate_text("prompt") == "answer"

    evaluate()
    assert tracker.call_attempts == 4
    assert tracker.successes == 3


def test_counts_calls_outside_env_evaluate():
    # Multiphase tasks evaluate via phase["evaluate"](env) with no wrapped
    # env.evaluate frame on the stack; their judge calls must still count,
    # mirroring where NoModelGuard raises its boundary.
    tracker = _tracker_class()()
    model_client = types.SimpleNamespace(
        generate_text=lambda *a, **k: "x",
        generate_chat=lambda *a, **k: "x",
    )
    llm_metrics = types.SimpleNamespace(generate_text=lambda *a, **k: "x")
    tracker.install(model_client=model_client, llm_metrics=llm_metrics)
    model_client.generate_text("prompt")
    assert tracker.call_attempts == 1
    assert tracker.successes == 1


def test_failed_call_counts_attempt_only():
    tracker = _tracker_class()()

    def boom(*a, **k):
        raise RuntimeError("judge transport failure")

    model_client = types.SimpleNamespace(generate_text=boom, generate_chat=boom)
    llm_metrics = types.SimpleNamespace(generate_text=boom)
    tracker.install(model_client=model_client, llm_metrics=llm_metrics)
    with pytest.raises(RuntimeError):
        model_client.generate_text("prompt")
    assert tracker.call_attempts == 1
    assert tracker.successes == 0


@pytest.mark.parametrize("answer", ["", "  \n", None])
def test_empty_response_cannot_validate_a_judge_or_simulator_call(answer):
    tracker = _tracker_class()()
    model_client = SimpleNamespace(
        generate_text=lambda *a, **k: answer,
        generate_chat=lambda *a, **k: answer,
    )

    class Simulator:
        def respond(self, question):
            return model_client.generate_chat([question])

    tracker.install(
        model_client=model_client,
        llm_metrics=SimpleNamespace(generate_text=model_client.generate_text),
        user_simulator=Simulator,
    )
    with pytest.raises(ValueError, match="empty model response"):
        model_client.generate_text("judge")
    with pytest.raises(ValueError, match="empty model response"):
        Simulator().respond("question")
    assert tracker.call_attempts == tracker.user_sim_call_attempts == 1
    assert tracker.successes == tracker.user_sim_successes == 0


def test_simulator_success_does_not_count_as_a_judge_success():
    tracker = _tracker_class()()
    model_client = SimpleNamespace(
        generate_text=lambda *a, **k: "YES",
        generate_chat=lambda *a, **k: "user answer",
    )

    class Simulator:
        def respond(self, question):
            return model_client.generate_chat([question])

    tracker.install(
        model_client=model_client,
        llm_metrics=SimpleNamespace(generate_text=model_client.generate_text),
        user_simulator=Simulator,
    )
    assert Simulator().respond("question") == "user answer"
    assert tracker.call_attempts == tracker.successes == 0
    assert tracker.user_sim_call_attempts == tracker.user_sim_successes == 1
    assert model_client.generate_text("judge") == "YES"
    assert tracker.call_attempts == tracker.successes == 1


def test_simulator_failure_does_not_leak_context_into_later_judge_calls():
    tracker = _tracker_class()()

    def failure(*args):
        raise RuntimeError("simulator unavailable")

    model_client = SimpleNamespace(
        generate_text=lambda *args: "YES", generate_chat=failure
    )

    class Simulator:
        def respond(self, question):
            try:
                return model_client.generate_chat([question])
            except RuntimeError:
                return "fallback"  # upstream can swallow model failures

    tracker.install(
        model_client=model_client,
        llm_metrics=SimpleNamespace(generate_text=model_client.generate_text),
        user_simulator=Simulator,
    )
    assert Simulator().respond("question") == "fallback"
    assert tracker.user_sim_call_attempts == 1
    assert tracker.user_sim_successes == 0
    assert model_client.generate_text("judge") == "YES"
    assert tracker.call_attempts == tracker.successes == 1


class _Usage:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Resp:
    def __init__(self, usage):
        self.usage = usage


def _anthropic_messages():
    class Messages:  # stands in for anthropic.resources.messages.Messages
        def create(self, **_body):
            return _Resp(_Usage(input_tokens=10, output_tokens=3))

    return Messages


def _openai_completions():
    class Completions:  # stands in for openai ... chat.completions.Completions
        def create(self, **_body):
            return _Resp(_Usage(prompt_tokens=7, completion_tokens=2))

    return Completions


def test_sdk_usage_is_attributed_by_role():
    tracker = _tracker_class()()
    Messages = _anthropic_messages()
    Completions = _openai_completions()

    class NoUsage:
        def create(self, **_body):
            return object()

    model_client = types.SimpleNamespace(
        generate_text=lambda *a, **k: Messages().create() and "YES",
        generate_chat=lambda *a, **k: "ok",
    )

    class Simulator:
        def respond(self, question):
            return Completions().create() and "blue"

    tracker.install(
        model_client=model_client,
        llm_metrics=types.SimpleNamespace(generate_text=lambda *a, **k: "YES"),
        user_simulator=Simulator,
        anthropic_messages=Messages,
        openai_completions=Completions,
    )
    Messages().create()  # agent role: the M3 agent's own SDK client
    model_client.generate_text("judge")  # judge role, one anthropic call inside
    Simulator().respond("question")  # simulator role, one openai call inside
    NoUsage().create()  # never wrapped, so never counted

    usage = tracker.usage
    assert usage["agent"] == {
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 3,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "unmeasured_calls": 0,
    }
    assert usage["judge"] == {
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 3,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "unmeasured_calls": 0,
    }
    assert usage["simulator"] == {
        "calls": 1,
        "input_tokens": 7,
        "output_tokens": 2,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "unmeasured_calls": 0,
    }


def test_usage_reports_zero_calls_with_null_tokens_for_every_role(monkeypatch):
    # install() falls back to the real anthropic/openai SDK classes when neither
    # is passed; stub the resolver out so this test never patches a real SDK.
    module = _tracker_module()
    monkeypatch.setattr(module, "_sdk_class", lambda *_args, **_kwargs: None)
    tracker = module.EvaluatorModelCallTracker()
    tracker.install(
        model_client=types.SimpleNamespace(
            generate_text=lambda *a, **k: "x", generate_chat=lambda *a, **k: "x"
        ),
        llm_metrics=types.SimpleNamespace(generate_text=lambda *a, **k: "x"),
    )
    assert tracker.usage == {
        role: {
            "calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_creation_5m_input_tokens": None,
            "cache_creation_1h_input_tokens": None,
            "reasoning_output_tokens": None,
            "unmeasured_calls": 0,
        }
        for role in ("agent", "judge", "simulator")
    }


def test_response_without_usage_counts_as_unmeasured():
    tracker = _tracker_class()()

    class Messages:
        def create(self, **_body):
            return object()

    tracker.install(
        model_client=types.SimpleNamespace(
            generate_text=lambda *a, **k: "x", generate_chat=lambda *a, **k: "x"
        ),
        llm_metrics=types.SimpleNamespace(generate_text=lambda *a, **k: "x"),
        user_simulator=None,
        anthropic_messages=Messages,
        openai_completions=None,
    )
    Messages().create()
    assert tracker.usage["agent"] == {
        "calls": 1,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_creation_5m_input_tokens": None,
        "cache_creation_1h_input_tokens": None,
        "reasoning_output_tokens": None,
        "unmeasured_calls": 1,
    }


def test_streaming_call_is_counted_but_not_measured():
    tracker = _tracker_class()()
    Messages = _anthropic_messages()
    tracker.install(
        model_client=types.SimpleNamespace(
            generate_text=lambda *a, **k: "x", generate_chat=lambda *a, **k: "x"
        ),
        llm_metrics=types.SimpleNamespace(generate_text=lambda *a, **k: "x"),
        anthropic_messages=Messages,
    )
    Messages().create(stream=True)
    assert tracker.usage["agent"] == {
        "calls": 1,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_creation_5m_input_tokens": None,
        "cache_creation_1h_input_tokens": None,
        "reasoning_output_tokens": None,
        "unmeasured_calls": 1,
    }


def test_installing_twice_does_not_double_count_sdk_calls():
    # install() wraps SDK classes in place, which is process-global state; a
    # second install must not stack a second wrapper on the same create.
    tracker = _tracker_class()()
    Messages = _anthropic_messages()
    model_client = types.SimpleNamespace(
        generate_text=lambda *a, **k: "x", generate_chat=lambda *a, **k: "x"
    )
    llm_metrics = types.SimpleNamespace(generate_text=lambda *a, **k: "x")
    tracker.install(
        model_client=model_client, llm_metrics=llm_metrics, anthropic_messages=Messages
    )
    tracker.install(
        model_client=model_client, llm_metrics=llm_metrics, anthropic_messages=Messages
    )
    Messages().create()
    assert tracker.usage["agent"]["calls"] == 1
    assert tracker.usage["agent"]["input_tokens"] == 10


def test_judge_helper_called_inside_the_simulator_stays_simulator_usage():
    # The real LLMUserSimulator.respond goes through model_client.generate_chat,
    # so the judge wrapper must not reclaim a simulator SDK call as judge work.
    tracker = _tracker_class()()
    Messages = _anthropic_messages()
    model_client = types.SimpleNamespace(
        generate_text=lambda *a, **k: "YES",
        generate_chat=lambda *a, **k: Messages().create() and "blue",
    )

    class Simulator:
        def respond(self, question):
            return model_client.generate_chat([question])

    tracker.install(
        model_client=model_client,
        llm_metrics=types.SimpleNamespace(generate_text=lambda *a, **k: "YES"),
        user_simulator=Simulator,
        anthropic_messages=Messages,
    )
    assert Simulator().respond("question") == "blue"
    assert tracker.usage["simulator"]["calls"] == 1
    assert tracker.usage["judge"]["calls"] == 0
    assert tracker.user_sim_call_attempts == tracker.user_sim_successes == 1
    assert tracker.call_attempts == 0


def test_install_without_sdks_still_reports_every_role(monkeypatch):
    # The runner's fake checkout has no anthropic/openai on the path; install()
    # must tolerate that and still produce the three roles. The resolver is
    # stubbed to None so the test does not depend on -- or wrap -- a real SDK.
    module = _tracker_module()
    monkeypatch.setattr(module, "_sdk_class", lambda *_args, **_kwargs: None)
    tracker = module.EvaluatorModelCallTracker()
    tracker.install(
        model_client=types.SimpleNamespace(
            generate_text=lambda *a, **k: "x", generate_chat=lambda *a, **k: "x"
        ),
        llm_metrics=types.SimpleNamespace(generate_text=lambda *a, **k: "x"),
    )
    assert sorted(tracker.usage) == ["agent", "judge", "simulator"]


def _install_sdk(tracker, **sdks):
    model_client = SimpleNamespace(
        generate_text=lambda *a, **k: "YES", generate_chat=lambda *a, **k: "YES"
    )
    tracker.install(
        model_client=model_client,
        llm_metrics=SimpleNamespace(generate_text=lambda *a, **k: "YES"),
        **sdks,
    )
    return model_client


def test_responses_usage_keeps_cached_and_reasoning_tokens_as_subsets():
    tracker = _tracker_class()()

    class Responses:
        def create(self, **kwargs):
            return _Resp(
                _Usage(
                    input_tokens=100,
                    output_tokens=40,
                    input_tokens_details=_Usage(cached_tokens=60),
                    output_tokens_details=_Usage(reasoning_tokens=30),
                )
            )

    _install_sdk(tracker, openai_responses=Responses)
    response = Responses().create(model="sol", input="private prompt")
    assert response.usage.input_tokens == 100
    usage = tracker.usage["agent"]
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    assert usage["cached_input_tokens"] == 60
    assert usage["reasoning_output_tokens"] == 30
    assert usage["cache_creation_input_tokens"] == 0


def test_anthropic_cache_reads_and_writes_are_included_in_total_input():
    tracker = _tracker_class()()

    class Messages:
        def create(self, **kwargs):
            return _Resp(
                _Usage(
                    input_tokens=10,
                    output_tokens=3,
                    cache_read_input_tokens=60,
                    cache_creation_input_tokens=30,
                    cache_creation=_Usage(
                        ephemeral_5m_input_tokens=20,
                        ephemeral_1h_input_tokens=10,
                    ),
                )
            )

    class BedrockMessages(Messages):
        pass

    _install_sdk(tracker, anthropic_messages=Messages)
    BedrockMessages().create()
    usage = tracker.usage["agent"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 3
    assert usage["cached_input_tokens"] == 60
    assert usage["cache_creation_input_tokens"] == 30
    assert usage["cache_creation_5m_input_tokens"] == 20
    assert usage["cache_creation_1h_input_tokens"] == 10
    assert usage["reasoning_output_tokens"] == 0


def test_chat_usage_keeps_cache_and_reasoning_as_subsets():
    tracker = _tracker_class()()

    class Completions:
        def create(self, **kwargs):
            return _Resp(
                _Usage(
                    prompt_tokens=100,
                    completion_tokens=40,
                    prompt_tokens_details=_Usage(cached_tokens=60),
                    completion_tokens_details=_Usage(reasoning_tokens=30),
                )
            )

    _install_sdk(tracker, openai_completions=Completions)
    Completions().create()
    usage = tracker.usage["agent"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    assert usage["cached_input_tokens"] == 60
    assert usage["reasoning_output_tokens"] == 30


def test_default_sdk_discovery_includes_responses(monkeypatch):
    module = _tracker_module()

    class Responses:
        def create(self, **kwargs):
            return _Resp(_Usage(input_tokens=4, output_tokens=2))

    def resolve(module, name):
        return (
            Responses
            if (module, name) == ("openai.resources.responses", "Responses")
            else None
        )

    monkeypatch.setattr(module, "_sdk_class", resolve)
    tracker = module.EvaluatorModelCallTracker()
    _install_sdk(tracker)
    Responses().create()
    assert tracker.usage["agent"]["input_tokens"] == 4


def test_usage_audit_records_private_per_call_metadata_and_errors(
    tmp_path, monkeypatch
):
    import json
    import os
    import stat

    monkeypatch.setenv("OSWORLD_MODEL_USAGE_LOG_DIR", str(tmp_path / "audit"))
    tracker = _tracker_class()()

    class Responses:
        def create(self, **kwargs):
            if kwargs.get("fail"):
                raise RuntimeError("SECRET provider detail")
            return _Resp(_Usage(input_tokens=4, output_tokens=2))

    model_client = _install_sdk(tracker, openai_responses=Responses)
    model_client.generate_chat = tracker._tracked(
        lambda: Responses().create(model="sol", input="SECRET prompt") and "answer"
    )
    model_client.generate_chat()
    with pytest.raises(RuntimeError, match="SECRET"):
        Responses().create(model="sol", fail=True)
    Responses().create(model="sol", stream=True)
    files = list((tmp_path / "audit").glob("model-usage-*.jsonl"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    raw = files[0].read_text()
    assert "SECRET" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert len(events) == 3
    assert [e["status"] for e in events] == ["completed", "error", "completed"]
    assert [e["measured"] for e in events] == [True, False, False]
    assert [e["role"] for e in events] == ["judge", "agent", "agent"]
    assert events[0]["model"] == "sol"
    assert events[0]["api"] == "openai.responses"
    assert events[0]["pid"] == os.getpid()
    assert events[0]["timestamp"]
    assert events[0]["input_tokens"] == 4
    assert events[1]["input_tokens"] is None
    assert events[2]["input_tokens"] is None
    assert tracker.usage["agent"]["calls"] == 1
    assert tracker.usage["agent"]["unmeasured_calls"] == 1


def test_usage_audit_failure_does_not_change_provider_result(
    tmp_path, monkeypatch, caplog
):
    invalid_dir = tmp_path / "file"
    invalid_dir.write_text("not a directory")
    monkeypatch.setenv("OSWORLD_MODEL_USAGE_LOG_DIR", str(invalid_dir))
    tracker = _tracker_class()()
    Messages = _anthropic_messages()
    _install_sdk(tracker, anthropic_messages=Messages)
    assert Messages().create().usage.input_tokens == 10
    assert tracker.usage["agent"]["input_tokens"] == 10
    assert "usage audit" in caplog.text.lower()


def test_responses_cache_writes_are_counted_without_adding_to_input_total():
    tracker = _tracker_class()()

    class Responses:
        def create(self, **kwargs):
            return _Resp(
                _Usage(
                    input_tokens=3000,
                    output_tokens=40,
                    input_tokens_details=_Usage(
                        cached_tokens=600, cache_write_tokens=1971
                    ),
                    output_tokens_details=_Usage(reasoning_tokens=30),
                )
            )

    _install_sdk(tracker, openai_responses=Responses)
    Responses().create(model="sol")
    usage = tracker.usage["agent"]
    assert usage["input_tokens"] == 3000
    assert usage["cached_input_tokens"] == 600
    assert usage["cache_creation_input_tokens"] == 1971
    assert usage["output_tokens"] == 40
    assert usage["reasoning_output_tokens"] == 30
