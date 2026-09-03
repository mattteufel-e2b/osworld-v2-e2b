"""Fail-closed evaluator stubs for environment-path validation without an LLM."""

from __future__ import annotations


class NoModelGuard:
    def __init__(self) -> None:
        self.call_attempts = 0

    def _negative(self, *_args, **_kwargs) -> str:
        self.call_attempts += 1
        return "NO"

    @staticmethod
    def _blocked_backend(*_args, **_kwargs):
        raise RuntimeError(
            "evaluation-model backend creation is disabled in no-model mode"
        )

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
