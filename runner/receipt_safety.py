"""Small allowlisting helpers for shareable benchmark receipts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Retry-wave allowlist. Keep in sync with agent_runner._classify_stage_and_cause
# and write_timeout_receipt.py. "evaluator-or-agent" is a scored model attempt
# and is deliberately never retried; a shell-enforced deadline ("task-timeout") is.
RETRYABLE_ERROR_CAUSES = frozenset(
    {
        "transport",
        "chrome-cdp",
        "environment-setup",
        "reset-or-observation",
        "task-timeout",
    }
)


def public_error(error: BaseException) -> dict[str, str]:
    """Describe an exception without copying its potentially secret-bearing text."""
    return {"error_type": type(error).__name__}


def public_transport(value: str | None) -> str | None:
    """Keep endpoint routing metadata while dropping credentials and URL secrets."""
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))


def atomic_write_text(path: Path, text: str) -> None:
    """Publish a private (0600) file atomically: a reader sees the old file or
    the complete new one, never a truncation -- a SIGTERM/SIGKILL landing
    mid-write must not cost a receipt, runtime section or token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            os.chmod(staged_path, 0o600)
            staged.write(text)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_path, path)
        staged_path = None
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_text(
        path, json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )


def positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def nonnegative_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else None


def base_receipt(
    *,
    task_id: str,
    domain: str,
    agent_kind: str,
    model: str,
    max_steps: int,
    port_base: int,
) -> dict:
    """The run-configuration fields every per-task receipt carries, whether it
    is written by agent_runner.py or by the shell watchdog on a timeout. One
    definition keeps the two writers from drifting apart."""
    return {
        "id": task_id,
        "run_nonce": os.environ.get("OSWORLD_RUN_NONCE"),
        "domain": domain,
        "agent_kind": agent_kind,
        "model": model,
        "model_transport": public_transport(os.environ.get("MODEL_BASE_URL")),
        "thinking_mode": os.environ.get("M3_THINKING_MODE") or None,
        "thinking_budget": positive_int_env("M3_THINKING_BUDGET"),
        "m3_max_llm_retries": (
            nonnegative_int_env("M3_MAX_LLM_RETRIES") if agent_kind == "m3" else None
        ),
        "eval_model": os.environ.get("OSWORLD_EVAL_MODEL_NAME") or None,
        "eval_provider": os.environ.get("OSWORLD_EVAL_MODEL_PROVIDER") or None,
        "eval_model_transport": public_transport(
            os.environ.get("OSWORLD_EVAL_MODEL_BASE_URL")
        ),
        "user_sim_model": os.environ.get("OSWORLD_USER_SIM_MODEL") or None,
        "user_sim_provider": os.environ.get("OSWORLD_USER_SIM_PROVIDER") or None,
        "user_sim_transport": public_transport(
            os.environ.get("OSWORLD_USER_SIM_BASE_URL")
        ),
        "port_base": port_base,
        "control_port": 14999 + port_base,
        "template": os.environ.get("GUEST_TEMPLATE"),
        "campaign_id": os.environ.get("OSWORLD_CAMPAIGN_ID"),
        "max_steps": max_steps,
    }
