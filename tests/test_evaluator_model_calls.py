from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tracker_class():
    path = ROOT / "runner" / "evaluator_model_calls.py"
    assert path.is_file(), "evaluator model-call tracker is missing"
    spec = importlib.util.spec_from_file_location("evaluator_model_calls", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EvaluatorModelCallTracker


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
