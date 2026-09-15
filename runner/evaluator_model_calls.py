"""Count evaluator-model calls without retaining prompts or responses.

Counts calls through desktop_env's model helpers, including multiphase
evaluators. User-simulator calls have separate counters and cannot satisfy
judge coverage. Prompts, responses, and upstream scoring are left untouched.

Token usage is read from the provider SDKs themselves: the wrapped ``create``
records only the integers in ``response.usage``, attributed to whichever role
-- agent, judge, or simulator -- is running when the call is made.
"""

from __future__ import annotations

import importlib
from contextvars import ContextVar
from functools import wraps
from typing import Callable

ROLES = ("agent", "judge", "simulator")
_WRAPPED_MARKER = "_osworld_usage_wrapped"


def require_response(result: str) -> str:
    if not isinstance(result, str) or not result.strip():
        raise ValueError("empty model response from judge or user simulator")
    return result


def _sdk_class(module: str, name: str):
    """The SDK class whose ``create`` carries token usage, or None if absent.

    The runner also runs against checkouts with no provider SDK installed, so
    a missing module is a normal outcome, not an error.
    """
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


class EvaluatorModelCallTracker:
    def __init__(self) -> None:
        self.call_attempts = 0
        self.successes = 0
        self.user_sim_call_attempts = 0
        self.user_sim_successes = 0
        self._role = ContextVar("osworld_model_role", default="agent")
        self._usage = {
            role: {"calls": 0, "input": 0, "output": 0, "unmeasured": 0}
            for role in ROLES
        }

    @property
    def usage(self) -> dict:
        """Per-role token counts. Tokens are None when nothing was measured."""
        report = {}
        for role, bucket in self._usage.items():
            measured = bucket["calls"] - bucket["unmeasured"]
            report[role] = {
                "calls": bucket["calls"],
                "input_tokens": bucket["input"] if measured > 0 else None,
                "output_tokens": bucket["output"] if measured > 0 else None,
                "unmeasured_calls": bucket["unmeasured"],
            }
        return report

    def _tracked(self, function: Callable) -> Callable:
        @wraps(function)
        def invoke(*args, **kwargs):
            role = self._role.get()
            prefix = "user_sim_" if role == "simulator" else ""
            attempts, successes = prefix + "call_attempts", prefix + "successes"
            setattr(self, attempts, getattr(self, attempts) + 1)
            # The simulator reaches the model through these same helpers, so a
            # judge helper running inside it stays attributed to the simulator.
            token = self._role.set("judge" if role != "simulator" else "simulator")
            try:
                result = require_response(function(*args, **kwargs))
            finally:
                self._role.reset(token)
            setattr(self, successes, getattr(self, successes) + 1)
            return result

        return invoke

    def _capture(self, create: Callable) -> Callable:
        """Wrap an SDK ``create`` so its usage block lands in the role bucket."""

        @wraps(create)
        def invoke(sdk_client, *args, **kwargs):
            response = create(sdk_client, *args, **kwargs)
            bucket = self._usage[self._role.get()]
            bucket["calls"] += 1
            usage = getattr(response, "usage", None)
            # anthropic names them input/output; openai prompt/completion.
            inp = getattr(usage, "input_tokens", None)
            if inp is None:
                inp = getattr(usage, "prompt_tokens", None)
            out = getattr(usage, "output_tokens", None)
            if out is None:
                out = getattr(usage, "completion_tokens", None)
            # A stream reports usage only as it is consumed, which is not ours
            # to consume, so it is counted but never measured.
            if kwargs.get("stream") or inp is None or out is None:
                bucket["unmeasured"] += 1
            else:
                bucket["input"] += int(inp)
                bucket["output"] += int(out)
            return response

        setattr(invoke, _WRAPPED_MARKER, True)
        return invoke

    def install(
        self,
        *,
        model_client=None,
        llm_metrics=None,
        user_simulator=None,
        anthropic_messages=None,
        openai_completions=None,
    ) -> None:
        if model_client is None or llm_metrics is None:
            from desktop_env.evaluators import model_client as real_model_client
            from desktop_env.evaluators.metrics import llm_metrics as real_llm_metrics
            from desktop_env.user_simulator import LLMUserSimulator

            model_client = real_model_client
            llm_metrics = real_llm_metrics
            user_simulator = LLMUserSimulator
        if anthropic_messages is None and openai_completions is None:
            anthropic_messages = _sdk_class("anthropic.resources.messages", "Messages")
            openai_completions = _sdk_class(
                "openai.resources.chat.completions", "Completions"
            )
        for sdk_class in (anthropic_messages, openai_completions):
            # Wrapping happens on the class, which is process-global: a second
            # install must not stack a second wrapper on the same create.
            if sdk_class is not None and not getattr(
                sdk_class.create, _WRAPPED_MARKER, False
            ):
                sdk_class.create = self._capture(sdk_class.create)
        if user_simulator is not None:
            respond = user_simulator.respond

            @wraps(respond)
            def simulator_response(*args, **kwargs):
                token = self._role.set("simulator")
                try:
                    return respond(*args, **kwargs)
                finally:
                    self._role.reset(token)

            user_simulator.respond = simulator_response
        model_client.generate_text = self._tracked(model_client.generate_text)
        model_client.generate_chat = self._tracked(model_client.generate_chat)
        # llm_metrics binds generate_text at import time, so wrap its module
        # global before gated task modules import compare_text_with_llm.
        llm_metrics.generate_text = self._tracked(llm_metrics.generate_text)
