#!/usr/bin/env python3
"""Build and gate a shareable full-agent campaign receipt."""

from __future__ import annotations

import argparse
import json
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path

from receipt_safety import public_transport


def _valid_score(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def aggregate(
    manifest_path: Path,
    worker_dir: Path,
    *,
    model: str,
    agent_kind: str,
    model_transport: str,
    eval_model: str,
    eval_transport: str,
    max_steps: int,
    concurrency: int,
    thinking_mode: str | None,
    thinking_budget: int | None,
    task_082_concurrent: bool,
) -> tuple[dict, bool]:
    manifest = json.loads(manifest_path.read_text())
    expected_ids = [item["id"] for item in manifest["tasks"]]
    expected_model_transport = public_transport(model_transport)
    expected_eval_transport = public_transport(eval_transport)
    records: list[dict] = []
    invalid: dict[str, list[str]] = {}

    for task_id in expected_ids:
        path = worker_dir / f"task_{task_id}.json"
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            invalid[task_id] = ["missing-or-invalid-receipt"]
            continue
        reasons: list[str] = []
        checks = (
            (record.get("id") == task_id, "task-id"),
            (record.get("template") == manifest["template"], "template"),
            (record.get("path_status") == "OK", "path-status"),
            (record.get("evaluator_ran") is True, "evaluator"),
            (_valid_score(record.get("score")), "score"),
            (bool(record.get("sandbox_id")), "sandbox-id"),
            (record.get("restricted_ingress") is True, "restricted-ingress"),
            (
                isinstance(record.get("steps_taken"), int)
                and record["steps_taken"] > 0,
                "model-action",
            ),
            (record.get("model") == model, "model"),
            (record.get("agent_kind") == agent_kind, "agent-kind"),
            (record.get("eval_model") == eval_model, "eval-model"),
            (
                record.get("model_transport") == expected_model_transport,
                "model-transport",
            ),
            (
                record.get("eval_model_transport") == expected_eval_transport,
                "eval-transport",
            ),
        )
        reasons.extend(label for passed, label in checks if not passed)
        if reasons:
            invalid[task_id] = reasons
        records.append(record)

    scores = [
        float(record["score"])
        for record in records
        if _valid_score(record.get("score"))
    ]
    sandbox_ids = [
        record["sandbox_id"] for record in records if record.get("sandbox_id")
    ]
    attested = len(records) - sum(
        bool(invalid.get(task_id)) for task_id in expected_ids
    )
    summary = {
        "tasks": len(records),
        "expected_tasks": len(expected_ids),
        "invalid_task_ids": invalid,
        "attested_records": attested,
        "path_ok": sum(record.get("path_status") == "OK" for record in records),
        "evaluator_ran_count": sum(
            record.get("evaluator_ran") is True for record in records
        ),
        "scored_tasks": len(scores),
        "mean_score": (sum(scores) / len(scores)) if scores else None,
        "partial_score": (sum(scores) / len(scores)) if scores else None,
        "binary_successes": sum(score == 1.0 for score in scores),
        "binary_accuracy": (
            sum(score == 1.0 for score in scores) / len(scores) if scores else None
        ),
        "unique_sandboxes": len(set(sandbox_ids)),
        "all_recorded_sandboxes_unique": len(set(sandbox_ids)) == len(sandbox_ids),
    }
    run = {
        "schema_version": 2,
        "run_id": str(uuid.uuid4()),
        "finished_at": datetime.now(UTC).isoformat(),
        "purpose": "OSWorld-V2 agent benchmark on E2B",
        "release": manifest.get("release"),
        "template": manifest["template"],
        "osworld_commit": manifest["osworld_commit"],
        "model": model,
        "agent_kind": agent_kind,
        "model_transport": expected_model_transport,
        "reasoning": {"mode": thinking_mode, "budget_tokens": thinking_budget},
        "evaluator": {
            "provider": "openai_compatible",
            "model": eval_model,
            "transport": expected_eval_transport,
        },
        "max_steps": max_steps,
        "execution": {
            "mode": "bounded-parallel-with-namespaced-task-service-port",
            "max_parallel_workers": concurrency,
            "task_082_concurrent": task_082_concurrent,
            "host_proxy_owned_for_campaign": True,
            "implicit_retries": False,
        },
        "records": records,
        "summary": summary,
    }
    ok = (
        not invalid
        and len(records) == len(expected_ids)
        and attested == len(expected_ids)
        and len(sandbox_ids) == len(set(sandbox_ids)) == len(expected_ids)
    )
    return run, ok


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--worker-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--agent-kind", required=True)
    parser.add_argument("--model-transport", required=True)
    parser.add_argument("--eval-model", required=True)
    parser.add_argument("--eval-transport", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--thinking-mode")
    parser.add_argument("--thinking-budget", type=int)
    parser.add_argument("--task-082-concurrent", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run, ok = aggregate(
        args.manifest,
        args.worker_dir,
        model=args.model,
        agent_kind=args.agent_kind,
        model_transport=args.model_transport,
        eval_model=args.eval_model,
        eval_transport=args.eval_transport,
        max_steps=args.max_steps,
        concurrency=args.concurrency,
        thinking_mode=args.thinking_mode,
        thinking_budget=args.thinking_budget,
        task_082_concurrent=args.task_082_concurrent,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(run, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    print(json.dumps(run["summary"], sort_keys=True))
    print("AGENT RECEIPT GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
