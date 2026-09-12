"""Count evaluator-model calls without retaining prompts or responses.

Counts calls through desktop_env's model helpers, including multiphase
evaluators. User-simulator calls have separate counters and cannot satisfy
judge coverage. Prompts, responses, and upstream scoring are left untouched.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Callable


class EvaluatorModelCallTracker:
    def __init__(self) -> None:
        self.call_attempts = 0
        self.successes = 0
        self.user_sim_call_attempts = 0
        self.user_sim_successes = 0
        self._in_user_sim = ContextVar("osworld_user_sim_call", default=False)

    def _tracked(self, function: Callable) -> Callable:
        @wraps(function)
        def invoke(*args, **kwargs):
            prefix = "user_sim_" if self._in_user_sim.get() else ""
            attempts, successes = prefix + "call_attempts", prefix + "successes"
            setattr(self, attempts, getattr(self, attempts) + 1)
            result = function(*args, **kwargs)
            setattr(self, successes, getattr(self, successes) + 1)
            return result

        return invoke

    def install(
        self, *, model_client=None, llm_metrics=None, user_simulator=None
    ) -> None:
        if model_client is None or llm_metrics is None:
            from desktop_env.evaluators import model_client as real_model_client
            from desktop_env.evaluators.metrics import llm_metrics as real_llm_metrics
            from desktop_env.user_simulator import LLMUserSimulator

            model_client = real_model_client
            llm_metrics = real_llm_metrics
            user_simulator = LLMUserSimulator
        if user_simulator is not None:
            respond = user_simulator.respond

            @wraps(respond)
            def simulator_response(*args, **kwargs):
                token = self._in_user_sim.set(True)
                try:
                    return respond(*args, **kwargs)
                finally:
                    self._in_user_sim.reset(token)

            user_simulator.respond = simulator_response
        model_client.generate_text = self._tracked(model_client.generate_text)
        model_client.generate_chat = self._tracked(model_client.generate_chat)
        # llm_metrics binds generate_text at import time, so wrap its module
        # global before gated task modules import compare_text_with_llm.
        llm_metrics.generate_text = self._tracked(llm_metrics.generate_text)
