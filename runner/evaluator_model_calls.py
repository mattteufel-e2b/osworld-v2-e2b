"""Count evaluator-model calls without retaining prompts or responses."""

from __future__ import annotations

from functools import wraps
from typing import Callable


class EvaluatorModelCallTracker:
    def __init__(self) -> None:
        self.call_attempts = 0
        self.successes = 0
        self._evaluation_depth = 0

    def _tracked(self, function: Callable) -> Callable:
        @wraps(function)
        def invoke(*args, **kwargs):
            if self._evaluation_depth == 0:
                return function(*args, **kwargs)
            self.call_attempts += 1
            result = function(*args, **kwargs)
            self.successes += 1
            return result

        return invoke

    def track_evaluation(self, function: Callable) -> Callable:
        """Count model-helper calls made while this evaluator function runs."""

        @wraps(function)
        def invoke(*args, **kwargs):
            self._evaluation_depth += 1
            try:
                return function(*args, **kwargs)
            finally:
                self._evaluation_depth -= 1

        return invoke

    def install(self, *, model_client=None, llm_metrics=None) -> None:
        if model_client is None or llm_metrics is None:
            from desktop_env.evaluators import model_client as real_model_client
            from desktop_env.evaluators.metrics import llm_metrics as real_llm_metrics

            model_client = real_model_client
            llm_metrics = real_llm_metrics
        model_client.generate_text = self._tracked(model_client.generate_text)
        model_client.generate_chat = self._tracked(model_client.generate_chat)
        # llm_metrics binds generate_text at import time, so wrap its module
        # global before gated task modules import compare_text_with_llm.
        llm_metrics.generate_text = self._tracked(llm_metrics.generate_text)
