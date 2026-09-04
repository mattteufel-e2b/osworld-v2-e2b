#!/usr/bin/env python3
"""Build and gate a shareable full-agent campaign receipt."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
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
    eval_provider: str,
    eval_transport: str,
    user_sim_model: str,
    user_sim_provider: str,
    user_sim_transport: str,
    max_steps: int,
    concurrency: int,
    thinking_mode: str | None,
    thinking_budget: int | None,
    m3_max_llm_retries: int | None,
    task_082_concurrent: bool,
    run_nonce: str,
    campaign_id: str,
    required_eval_model_ids: set[str],
) -> tuple[dict, bool]:
    manifest = json.loads(manifest_path.read_text())
    expected_ids = [item["id"] for item in manifest["tasks"]]
    expected_model_transport = public_transport(model_transport)
    expected_eval_transport = public_transport(eval_transport)
    expected_user_sim_transport = public_transport(user_sim_transport)
    records: list[dict] = []
    invalid: dict[str, list[str]] = {}
    attested = 0
    expected_thinking_budget = thinking_budget if thinking_budget else None

    for task_id in expected_ids:
        path = worker_dir / f"task_{task_id}.json"
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            invalid[task_id] = ["missing-or-invalid-receipt"]
            continue
        reasons: list[str] = []
        eval_attempts = record.get("eval_model_call_attempts")
        eval_successes = record.get("eval_model_successes")
        valid_eval_attempts = (
            isinstance(eval_attempts, int)
            and not isinstance(eval_attempts, bool)
            and eval_attempts >= 0
        )
        valid_eval_successes = (
            valid_eval_attempts
            and isinstance(eval_successes, int)
            and not isinstance(eval_successes, bool)
            and 0 <= eval_successes <= eval_attempts
        )
        checks = (
            (record.get("id") == task_id, "task-id"),
            (record.get("run_nonce") == run_nonce, "run-nonce"),
            (record.get("campaign_id") == campaign_id, "campaign-id"),
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
            (record.get("eval_provider") == eval_provider, "eval-provider"),
            (record.get("user_sim_model") == user_sim_model, "user-sim-model"),
            (
                record.get("user_sim_provider") == user_sim_provider,
                "user-sim-provider",
            ),
            (
                record.get("user_sim_transport") == expected_user_sim_transport,
                "user-sim-transport",
            ),
            (record.get("max_steps") == max_steps, "max-steps"),
            (record.get("thinking_mode") == thinking_mode, "thinking-mode"),
            (
                record.get("thinking_budget") == expected_thinking_budget,
                "thinking-budget",
            ),
            (
                record.get("m3_max_llm_retries") == m3_max_llm_retries,
                "m3-max-llm-retries",
            ),
            (
                record.get("model_transport") == expected_model_transport,
                "model-transport",
            ),
            (
                record.get("eval_model_transport") == expected_eval_transport,
                "eval-transport",
            ),
            (valid_eval_attempts, "eval-model-attempts"),
            (valid_eval_successes, "eval-model-success-count"),
        )
        reasons.extend(label for passed, label in checks if not passed)
        if task_id in required_eval_model_ids and not (
            valid_eval_successes and eval_successes > 0
        ):
            reasons.append("eval-model-success")
        if reasons:
            invalid[task_id] = reasons
        else:
            attested += 1
        records.append(record)

    scores = [
        float(record["score"])
        for record in records
        if _valid_score(record.get("score"))
    ]
    sandbox_ids = [
        record["sandbox_id"] for record in records if record.get("sandbox_id")
    ]
    summary = {
        "tasks": len(records),
        "expected_tasks": len(expected_ids),
        "invalid_task_ids": invalid,
        "attested_records": attested,
        "path_ok": sum(record.get("path_status") == "OK" for record in records),
        "evaluator_ran_count": sum(
            record.get("evaluator_ran") is True for record in records
        ),
        "eval_model_call_attempts": sum(
            record.get("eval_model_call_attempts", 0)
            for record in records
            if isinstance(record.get("eval_model_call_attempts"), int)
            and not isinstance(record.get("eval_model_call_attempts"), bool)
        ),
        "eval_model_successes": sum(
            record.get("eval_model_successes", 0)
            for record in records
            if isinstance(record.get("eval_model_successes"), int)
            and not isinstance(record.get("eval_model_successes"), bool)
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
    # run_agent_parallel.sh copies the pre-retry receipt to
    # task_<id>_before_retry_<n>.json before overwriting it, so the worker dir
    # is the ground truth for whether the retry wave replaced any receipt.
    retried_task_ids = sorted(
        {
            path.name[len("task_"):].split("_before_retry_")[0]
            for path in worker_dir.glob("task_*_before_retry_*.json")
        }
    )
    run = {
        "schema_version": 2,
        "run_id": run_nonce,
        "campaign_id": campaign_id,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "OSWorld-V2 agent benchmark on E2B",
        "release": manifest.get("release"),
        "template": manifest["template"],
        "osworld_commit": manifest["osworld_commit"],
        "model": model,
        "agent_kind": agent_kind,
        "model_transport": expected_model_transport,
        "reasoning": {
            "mode": thinking_mode,
            "budget_tokens": thinking_budget,
            "max_llm_retries": m3_max_llm_retries,
        },
        "evaluator": {
            "provider": eval_provider,
            "model": eval_model,
            "transport": expected_eval_transport,
            "required_success_task_ids": sorted(required_eval_model_ids),
        },
        "user_simulator": {
            "provider": user_sim_provider,
            "model": user_sim_model,
            "transport": expected_user_sim_transport,
        },
        "max_steps": max_steps,
        "execution": {
            "mode": "bounded-parallel-with-namespaced-task-service-port",
            "max_parallel_workers": concurrency,
            "task_082_concurrent": task_082_concurrent,
            "host_proxy_owned_for_campaign": True,
            "retried_task_ids": retried_task_ids,
            "implicit_retries": bool(retried_task_ids),
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
    parser.add_argument("--eval-provider", required=True)
    parser.add_argument("--eval-transport", required=True)
    parser.add_argument("--user-sim-model", required=True)
    parser.add_argument("--user-sim-provider", required=True)
    parser.add_argument("--user-sim-transport", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--thinking-mode")
    parser.add_argument("--thinking-budget", type=int)
    parser.add_argument("--m3-max-llm-retries", type=int)
    parser.add_argument("--task-082-concurrent", action="store_true")
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--no-model-receipt", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    required_eval_model_ids: set[str] = set()
    if args.no_model_receipt is not None:
        from model_coverage import verify_model_coverage

        required_eval_model_ids.update(
            verify_model_coverage(args.no_model_receipt, args.manifest)
        )
    run, ok = aggregate(
        args.manifest,
        args.worker_dir,
        model=args.model,
        agent_kind=args.agent_kind,
        model_transport=args.model_transport,
        eval_model=args.eval_model,
        eval_provider=args.eval_provider,
        eval_transport=args.eval_transport,
        user_sim_model=args.user_sim_model,
        user_sim_provider=args.user_sim_provider,
        user_sim_transport=args.user_sim_transport,
        max_steps=args.max_steps,
        concurrency=args.concurrency,
        thinking_mode=args.thinking_mode,
        thinking_budget=args.thinking_budget,
        m3_max_llm_retries=args.m3_max_llm_retries,
        task_082_concurrent=args.task_082_concurrent,
        run_nonce=args.run_nonce,
        campaign_id=args.campaign_id,
        required_eval_model_ids=required_eval_model_ids,
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
