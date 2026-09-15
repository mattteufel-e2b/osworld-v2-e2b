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
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from contextvars import ContextVar
from functools import wraps
from typing import Callable

ROLES = ("agent", "judge", "simulator")
_WRAPPED_MARKER = "_osworld_usage_wrapped"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "reasoning_output_tokens",
)


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
            role: {"calls": 0, "unmeasured": 0, **dict.fromkeys(TOKEN_FIELDS, 0)}
            for role in ROLES
        }
        self._audit_dir = os.environ.get("OSWORLD_MODEL_USAGE_LOG_DIR")

    @property
    def usage(self) -> dict:
        """Per-role totals; token fields are None when nothing was measured.

        Input includes cache reads and writes. Cache fields are subsets of
        input; the TTL fields are subsets of cache creation. Reasoning is a
        subset of output, never additional output. Absent SDK breakdowns are
        counted as zero. Calls count SDK returns, including unmeasured streams;
        raised requests are only recorded in the optional audit log.
        """
        report = {}
        for role, bucket in self._usage.items():
            measured = bucket["calls"] - bucket["unmeasured"]
            report[role] = {
                "calls": bucket["calls"],
                **{key: bucket[key] if measured > 0 else None for key in TOKEN_FIELDS},
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

    def _audit(self, *, role, api, model, status, tokens):
        """Append metadata only; never let audit I/O change model behavior.

        Set OSWORLD_MODEL_USAGE_LOG_DIR before tracker construction to enable
        model-usage-<pid>.jsonl. Each return or raised request gets one line;
        errors and streams have no measured tokens and unknown billing.
        """
        if not self._audit_dir:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "role": role,
            "api": api,
            "model": model if isinstance(model, str) else None,
            "status": status,
            "measured": tokens is not None,
            **(tokens if tokens is not None else dict.fromkeys(TOKEN_FIELDS)),
        }
        try:
            directory = Path(self._audit_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"model-usage-{os.getpid()}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as log:
                log.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            logging.getLogger(__name__).warning("Could not append model usage audit")

    def _capture(self, create: Callable, api: str) -> Callable:
        """Wrap an SDK ``create`` so its usage block lands in the role bucket."""

        @wraps(create)
        def invoke(sdk_client, *args, **kwargs):
            role = self._role.get()
            try:
                response = create(sdk_client, *args, **kwargs)
            except Exception:
                self._audit(
                    role=role,
                    api=api,
                    model=kwargs.get("model"),
                    status="error",
                    tokens=None,
                )
                raise
            bucket = self._usage[role]
            bucket["calls"] += 1
            usage = getattr(response, "usage", None)
            inp = getattr(usage, "input_tokens", None)
            if inp is None:
                inp = getattr(usage, "prompt_tokens", None)
            out = getattr(usage, "output_tokens", None)
            if out is None:
                out = getattr(usage, "completion_tokens", None)
            tokens = None
            # Streams report usage as consumed. Do not consume or alter them.
            if kwargs.get("stream") or inp is None or out is None:
                bucket["unmeasured"] += 1
            else:
                tokens = dict.fromkeys(TOKEN_FIELDS, 0)
                tokens["input_tokens"] = int(inp)
                tokens["output_tokens"] = int(out)
                if api == "anthropic.messages":
                    tokens["cached_input_tokens"] = int(
                        getattr(usage, "cache_read_input_tokens", None) or 0
                    )
                    tokens["cache_creation_input_tokens"] = int(
                        getattr(usage, "cache_creation_input_tokens", None) or 0
                    )
                    creation = getattr(usage, "cache_creation", None)
                    for ttl in ("5m", "1h"):
                        tokens[f"cache_creation_{ttl}_input_tokens"] = int(
                            getattr(creation, f"ephemeral_{ttl}_input_tokens", None)
                            or 0
                        )
                    # Anthropic's input_tokens excludes both cache categories.
                    tokens["input_tokens"] += (
                        tokens["cached_input_tokens"]
                        + tokens["cache_creation_input_tokens"]
                    )
                else:
                    input_details = getattr(usage, "input_tokens_details", None)
                    if input_details is None:
                        input_details = getattr(usage, "prompt_tokens_details", None)
                    output_details = getattr(usage, "output_tokens_details", None)
                    if output_details is None:
                        output_details = getattr(
                            usage, "completion_tokens_details", None
                        )
                    tokens["cached_input_tokens"] = int(
                        getattr(input_details, "cached_tokens", None) or 0
                    )
                    # Mantle Responses exposes cache writes as an input subset.
                    tokens["cache_creation_input_tokens"] = int(
                        getattr(input_details, "cache_write_tokens", None) or 0
                    )
                    tokens["reasoning_output_tokens"] = int(
                        getattr(output_details, "reasoning_tokens", None) or 0
                    )
                for key, value in tokens.items():
                    bucket[key] += value
            self._audit(
                role=role,
                api=api,
                model=getattr(response, "model", None) or kwargs.get("model"),
                status="completed",
                tokens=tokens,
            )
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
        openai_responses=None,
    ) -> None:
        """Wrap the evaluator/simulator entry points so this tracker sees usage.

        The SDK wrap lands on the class object and is therefore process-global
        and bound to the first tracker that installs it -- a second tracker in
        the same process records no token usage. Only the anthropic and openai
        SDK classes are wrapped, so a judge on any other SDK (Gemini, say) still
        reports its roles but with zero calls.
        """
        if model_client is None or llm_metrics is None:
            from desktop_env.evaluators import model_client as real_model_client
            from desktop_env.evaluators.metrics import llm_metrics as real_llm_metrics
            from desktop_env.user_simulator import LLMUserSimulator

            model_client = real_model_client
            llm_metrics = real_llm_metrics
            user_simulator = LLMUserSimulator
        if all(
            sdk is None
            for sdk in (anthropic_messages, openai_completions, openai_responses)
        ):
            anthropic_messages = _sdk_class("anthropic.resources.messages", "Messages")
            openai_completions = _sdk_class(
                "openai.resources.chat.completions", "Completions"
            )
            openai_responses = _sdk_class("openai.resources.responses", "Responses")
        # AnthropicBedrock uses the same Messages resource class.
        for sdk_class, api in (
            (anthropic_messages, "anthropic.messages"),
            (openai_completions, "openai.chat.completions"),
            (openai_responses, "openai.responses"),
        ):
            # Wrapping happens on the class, which is process-global: a second
            # install must not stack a second wrapper on the same create.
            if sdk_class is not None and not getattr(
                sdk_class.create, _WRAPPED_MARKER, False
            ):
                sdk_class.create = self._capture(sdk_class.create, api)
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
