from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from no_model import NoModelGuard, model_boundary_result  # noqa: E402


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

    assert model_client.generate_text("secret prompt") == "NO"
    assert model_client.generate_chat([{"content": "secret"}]) == "NO"
    assert llm_metrics.generate_text("secret prompt") == "NO"
    assert guard.call_attempts == 3
    with pytest.raises(RuntimeError, match="disabled"):
        model_client.create_backend(object())


def test_model_boundary_result_is_explicit_and_only_applies_during_evaluation():
    error = RuntimeError("structured evaluator rejected the deterministic stub")

    assert model_boundary_result("evaluate", error, call_attempts=1) == {
        "path_status": "MODEL_BOUNDARY_PASS",
        "stage": "model-boundary",
        "evaluator_ran": True,
        "score": 0.0,
        "error_type": "RuntimeError",
    }
    assert model_boundary_result("reset", error, call_attempts=1) is None
    assert model_boundary_result("evaluate", error, call_attempts=0) is None
