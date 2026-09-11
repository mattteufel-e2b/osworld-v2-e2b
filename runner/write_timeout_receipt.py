#!/usr/bin/env python3
"""Write the safe terminal receipt for a shell-enforced agent timeout."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from receipt_safety import atomic_write_json, base_receipt


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    parser.add_argument("--agent-kind", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--wall-clock-seconds", type=float, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = _relay_state(14999 + args.port_base)
    receipt = base_receipt(
        task_id=args.task_id,
        domain=args.domain,
        agent_kind=args.agent_kind,
        model=args.model,
        max_steps=args.max_steps,
        port_base=args.port_base,
    )
    receipt.update(
        finished_at=_utc_now(),
        wall_clock_s=args.wall_clock_seconds,
        timeout_seconds=args.timeout_seconds,
        # Resolved by agents.py inside the worker; a timed-out worker may never
        # have reached that point, and this writer cannot import the checkout.
        agent_settings=None,
        transport_ok=False,
        evaluator_ran=False,
        path_status="ERROR",
        error_cause="task-timeout",
        error_type="AgentTaskTimeout",
        steps_taken=None,
        score=None,
        judge_used=None,
        eval_model_call_attempts=None,
        eval_model_successes=None,
        user_sim_call_attempts=None,
        user_sim_successes=None,
        sandbox_id=state.get("sandbox_id"),
        sandbox_generation=state.get("generation"),
        restricted_ingress=state.get("restricted_ingress"),
    )
    atomic_write_json(args.output, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
