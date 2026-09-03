"""Fail-closed evaluator stubs for environment-path validation without an LLM."""

from __future__ import annotations


class NoModelEvaluationBoundary(RuntimeError):
    """Control-flow sentinel proving evaluation reached a disabled model call."""


def _contains_boundary(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, NoModelEvaluationBoundary):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def model_boundary_result(stage: str, error: BaseException) -> dict[str, object] | None:
    """Attest that a no-model evaluator reached its intentionally stubbed boundary."""
    if stage != "evaluate" or not _contains_boundary(error):
        return None
    return {
        "path_status": "MODEL_BOUNDARY_PASS",
        "stage": "model-boundary",
        "evaluator_ran": True,
        "score": 0.0,
        "error_type": type(error).__name__,
    }


class NoModelGuard:
    def __init__(self) -> None:
        self.call_attempts = 0

    def _negative(self, *_args, **_kwargs) -> str:
        self.call_attempts += 1
        raise NoModelEvaluationBoundary("external evaluation model disabled")

    def _blocked_backend(self, *_args, **_kwargs):
        self.call_attempts += 1
        raise NoModelEvaluationBoundary("evaluation-model backend creation disabled")

    def install(self, *, model_client=None, llm_metrics=None) -> None:
        if model_client is None or llm_metrics is None:
            from desktop_env.evaluators import model_client as real_model_client
            from desktop_env.evaluators.metrics import llm_metrics as real_llm_metrics

            model_client = real_model_client
            llm_metrics = real_llm_metrics
        model_client.generate_text = self._negative
        model_client.generate_chat = self._negative
        model_client.create_backend = self._blocked_backend
        # llm_metrics binds generate_text during import, so patch that module
        # global before any gated task module can bind its metric functions.
        llm_metrics.generate_text = self._negative
