from __future__ import annotations

import importlib.util
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

    # Calls outside DesktopEnv.evaluate (for example a user simulator) are not
    # evaluator evidence.
    assert model_client.generate_text("prompt") == "answer"

    def evaluate():
        assert model_client.generate_text("prompt") == "answer"
        with pytest.raises(RuntimeError, match="transport failed"):
            model_client.generate_chat([])
        assert llm_metrics.generate_text("prompt") == "answer"

    tracker.track_evaluation(evaluate)()
    assert tracker.call_attempts == 3
    assert tracker.successes == 2
