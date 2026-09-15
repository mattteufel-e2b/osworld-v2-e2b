"""Small allowlisting helpers for shareable benchmark receipts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Retry-wave allowlist. It pairs with classify_failure() below -- the one
# classifier every caller uses -- so the two never drift across files.
# "evaluator-or-agent" is a scored model attempt and is never retried; the
# runner's own deadline ("task-timeout") is. "interrupted" (SIGTERM from the
# coordinator or operator) is not retried either: the operator cancelled it.
# A timed-out or interrupted rollout that already wrote result.txt keeps its
# own cause, but is still never retried, because retry_candidates.py refuses
# any task whose directory holds a result.txt.
RETRYABLE_ERROR_CAUSES = frozenset(
    {
        "transport",
        "chrome-cdp",
        "environment-setup",
        "reset-or-observation",
        "task-timeout",
    }
)


_TRANSPORT_WORDS = (
    "connection",
    "timed out",
    "max retries",
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
)
_CDP_WORDS = ("cdp", "playwright", "websocket", "connect_over_cdp")


def classify_failure(
    result_dir: Path,
    error: BaseException,
    *,
    timeout_types: tuple[type, ...] = (),
    interrupt_types: tuple[type, ...] = (),
) -> tuple[str, bool, bool]:
    """Return (cause, transport_ok, evaluator_ran) for a failed rollout.

    Precedence: the caller's own deadline/cancel types; then a scored attempt
    (result.txt exists -> "evaluator-or-agent", never retried) regardless of the
    exception text; then transport / chrome-cdp / environment-setup by message;
    then "evaluator-or-agent" if frames were produced, else "reset-or-observation".
    transport_ok means reset+observation produced at least one frame;
    evaluator_ran means result.txt landed (evaluate completed).
    Exception text is read only to pick a public bucket; it is never returned.
    """
    evaluator_ran = (result_dir / "result.txt").exists()
    transport_ok = any(result_dir.glob("*.png")) or (result_dir / "traj.jsonl").exists()
    if timeout_types and isinstance(error, timeout_types):
        return "task-timeout", transport_ok, evaluator_ran
    if interrupt_types and isinstance(error, interrupt_types):
        return "interrupted", transport_ok, evaluator_ran
    if evaluator_ran:
        # A scored attempt: whatever failed afterwards, never retry it -- a
        # retry would overwrite a real score with a fresh rollout.
        return "evaluator-or-agent", transport_ok, True
    detail = f"{type(error).__name__}: {error}".lower()
    if any(word in detail for word in _TRANSPORT_WORDS):
        cause = "transport"
    elif any(word in detail for word in _CDP_WORDS):
        cause = "chrome-cdp"
    elif "environmentsetuperror" in detail:
        cause = "environment-setup"
    elif transport_ok:
        cause = "evaluator-or-agent"
    else:
        cause = "reset-or-observation"
    return cause, transport_ok, False


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


def atomic_write_json(path: Path, payload: dict | list) -> None:
    atomic_write_text(
        path, json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )


def read_retry_waves(path: Path) -> list[dict]:
    """Absent ledger means no retries; unreadable or malformed history raises."""
    try:
        waves = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(waves, list) or any(
        not isinstance(wave, dict)
        or type(wave.get("attempt")) is not int
        or wave["attempt"] < 1
        or not isinstance(wave.get("task_ids"), list)
        or not all(isinstance(tid, str) and tid for tid in wave["task_ids"])
        for wave in waves
    ):
        raise ValueError("invalid retry history")
    return waves


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
    recording_enabled: bool | None = None,
) -> dict:
    """The run-configuration fields every per-task receipt carries. One
    definition keeps every receipt writer from drifting apart."""
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
        # Screen recording is upstream's --enable_recording opt-in. The agent
        # runner passes what it actually asked DesktopEnv for; the timeout
        # writer falls back to the ENABLE_RECORDING the launch script exported.
        "recording_enabled": (
            recording_enabled
            if recording_enabled is not None
            else os.environ.get("ENABLE_RECORDING") == "1"
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
        "template": os.environ.get("GUEST_TEMPLATE"),
        "campaign_id": os.environ.get("OSWORLD_CAMPAIGN_ID"),
        "max_steps": max_steps,
    }
