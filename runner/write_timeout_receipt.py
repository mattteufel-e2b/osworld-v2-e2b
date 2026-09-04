#!/usr/bin/env python3
"""Write the safe terminal receipt for a shell-enforced agent timeout."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from receipt_safety import atomic_write_json, public_transport


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _nonnegative_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else None


def _relay_state(control_port: int) -> dict:
    try:
        with urlopen(f"http://127.0.0.1:{control_port}/state", timeout=2) as response:
            value = json.load(response)
    except Exception:  # noqa: BLE001 -- timeout receipts must survive relay failure
        return {}
    return value if isinstance(value, dict) else {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--port-base", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--agent-kind", choices=("prompt", "m3"), required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--wall-clock-seconds", type=float, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    control_port = 14999 + args.port_base
    state = _relay_state(control_port)
    receipt = {
        "id": args.task_id,
        "run_nonce": os.environ.get("OSWORLD_RUN_NONCE"),
        "domain": args.domain,
        "agent_kind": args.agent_kind,
        "model": args.model,
        "model_transport": public_transport(os.environ.get("MODEL_BASE_URL")),
        "thinking_mode": os.environ.get("M3_THINKING_MODE") or None,
        "thinking_budget": _positive_int_env("M3_THINKING_BUDGET"),
        "m3_max_llm_retries": (
            _nonnegative_int_env("M3_MAX_LLM_RETRIES")
            if args.agent_kind == "m3"
            else None
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
        "port_base": args.port_base,
        "control_port": control_port,
        "template": os.environ.get("GUEST_TEMPLATE"),
        "campaign_id": os.environ.get("OSWORLD_CAMPAIGN_ID"),
        "max_steps": args.max_steps,
        "finished_at": _utc_now(),
        "wall_clock_s": args.wall_clock_seconds,
        "timeout_seconds": args.timeout_seconds,
        "transport_ok": False,
        "evaluator_ran": False,
        "path_status": "ERROR",
        "error_cause": "task-timeout",
        "error_type": "AgentTaskTimeout",
        "steps_taken": None,
        "score": None,
        "judge_used": None,
        "eval_model_call_attempts": None,
        "eval_model_successes": None,
        "sandbox_id": state.get("sandbox_id"),
        "sandbox_generation": state.get("generation"),
        "restricted_ingress": state.get("restricted_ingress"),
    }
    atomic_write_json(args.output, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
