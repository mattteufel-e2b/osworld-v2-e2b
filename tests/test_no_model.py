from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from no_model import (  # noqa: E402
    NoModelEvaluationBoundary,
    NoModelGuard,
    model_boundary_result,
)


def test_no_model_guard_returns_deterministic_negative_without_backend_creation():
    def forbidden_backend(*_args, **_kwargs):
        raise AssertionError("backend creation must be unreachable")

    model_client = SimpleNamespace(
        generate_text=lambda *_args, **_kwargs: "live",
        generate_chat=lambda *_args, **_kwargs: "live",
        create_backend=forbidden_backend,
    )
    llm_metrics = SimpleNamespace(generate_text=lambda *_args, **_kwargs: "live")
    guard = NoModelGuard()

    guard.install(model_client=model_client, llm_metrics=llm_metrics)

    with pytest.raises(NoModelEvaluationBoundary):
        model_client.generate_text("secret prompt")
    with pytest.raises(NoModelEvaluationBoundary):
        model_client.generate_chat([{"content": "secret"}])
    with pytest.raises(NoModelEvaluationBoundary):
        llm_metrics.generate_text("secret prompt")
    with pytest.raises(NoModelEvaluationBoundary):
        model_client.create_backend(object())
    assert guard.call_attempts == 4


def test_model_boundary_result_is_explicit_and_only_applies_during_evaluation():
    boundary = NoModelEvaluationBoundary("model disabled")
    wrapped = RuntimeError("evaluator wrapped the model boundary")
    wrapped.__cause__ = boundary

    assert model_boundary_result("evaluate", wrapped) == {
        "path_status": "MODEL_BOUNDARY_PASS",
        "stage": "model-boundary",
        "evaluator_ran": True,
        "score": 0.0,
        "error_type": "RuntimeError",
    }
    assert model_boundary_result("reset", wrapped) is None
    assert model_boundary_result("evaluate", RuntimeError("unrelated")) is None


def test_unrelated_crash_with_boundary_context_is_not_a_pass():
    # An evaluator catches the boundary broadly, then crashes on its own bug.
    # The boundary rides along only in __context__ — that is a PATH_FAIL.
    try:
        try:
            raise NoModelEvaluationBoundary("disabled")
        except Exception:
            raise TypeError("broken fallback scoring")
    except TypeError as error:
        assert model_boundary_result("evaluate", error) is None


def test_explicitly_chained_boundary_is_a_pass():
    try:
        try:
            raise NoModelEvaluationBoundary("disabled")
        except NoModelEvaluationBoundary as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as error:
        result = model_boundary_result("evaluate", error)
        assert result is not None and result["path_status"] == "MODEL_BOUNDARY_PASS"
