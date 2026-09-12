#!/usr/bin/env python3
"""Require full-model coverage for every model-dependent no-model task."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected an object")
    return value


def _task_ids(manifest: dict, label: str) -> list[str]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError(f"{label} must contain a tasks list")
    task_ids = [task.get("id") for task in tasks if isinstance(task, dict)]
    if len(task_ids) != len(tasks) or any(
        not isinstance(task_id, str) or not task_id for task_id in task_ids
    ):
        raise ValueError(f"{label} contains an invalid task id")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError(f"{label} contains duplicate task ids")
    return task_ids


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def verify_model_coverage(no_model_receipt: Path, agent_manifest: Path) -> list[str]:
    no_model = _load_object(no_model_receipt, "no-model receipt")
    manifest = _load_object(agent_manifest, "agent manifest")
    receipt_manifest = no_model.get("manifest")
    if not isinstance(receipt_manifest, dict):
        raise ValueError("no-model receipt must contain its embedded manifest")
    records = no_model.get("records")
    if not isinstance(records, list):
        raise ValueError("no-model receipt must contain a records list")
    for field in ("release", "template", "osworld_commit"):
        values = (
            no_model.get(field),
            receipt_manifest.get(field),
            manifest.get(field),
        )
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError(
                f"no-model receipt, embedded manifest, and agent manifest require {field}"
            )
        if len(set(values)) != 1:
            raise ValueError(
                f"no-model receipt {field} does not match its embedded manifest "
                "and the agent manifest"
            )

    expected_ids = _task_ids(receipt_manifest, "no-model receipt embedded manifest")
    if not expected_ids:
        raise ValueError("no-model receipt embedded manifest has no tasks")
    model_ids = set(_task_ids(manifest, "agent manifest"))

    record_ids: list[str] = []
    sandbox_ids: list[str] = []
    required_model_ids: set[str] = set()
    path_passes = 0
    boundary_passes = 0
    evaluator_ran_count = 0
    eval_model_call_attempts = 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("no-model receipt contains an invalid task record")
        task_id = record.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("no-model receipt contains an invalid task record id")
        record_ids.append(task_id)

        status = record.get("path_status")
        if status not in {"PATH_PASS", "MODEL_BOUNDARY_PASS"}:
            raise ValueError(f"no-model task {task_id} has a nonpassing path status")
        path_passes += status == "PATH_PASS"
        boundary_passes += status == "MODEL_BOUNDARY_PASS"

        if record.get("evaluator_ran") is not True:
            raise ValueError(f"no-model task {task_id} did not run its evaluator")
        evaluator_ran_count += 1
        if (
            type(record.get("external_model_calls")) is not int
            or record.get("external_model_calls") != 0
        ):
            raise ValueError(
                f"no-model task {task_id} does not attest zero external model calls"
            )

        attempts = record.get("eval_model_call_attempts")
        if not _nonnegative_int(attempts):
            raise ValueError(
                f"no-model task {task_id} has an invalid evaluator-model attempt count"
            )
        eval_model_call_attempts += attempts
        if attempts > 0 or status == "MODEL_BOUNDARY_PASS":
            required_model_ids.add(task_id)

        sandbox = record.get("sandbox")
        sandbox_id = sandbox.get("id") if isinstance(sandbox, dict) else None
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError(f"no-model task {task_id} has no sandbox id")
        sandbox_ids.append(sandbox_id)

    if len(record_ids) != len(set(record_ids)):
        raise ValueError("no-model receipt contains duplicate task records")
    if len(record_ids) != len(expected_ids) or set(record_ids) != set(expected_ids):
        raise ValueError(
            "no-model receipt records do not cover the embedded manifest's complete task set"
        )
    if len(sandbox_ids) != len(set(sandbox_ids)):
        raise ValueError("no-model receipt contains duplicate sandbox ids")

    summary = no_model.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("no-model receipt must contain a summary object")
    external_model_calls = (
        summary.get("external_model_calls") if isinstance(summary, dict) else None
    )
    if type(external_model_calls) is not int or external_model_calls != 0:
        raise ValueError("no-model receipt does not attest zero external model calls")
    expected_summary = {
        "tasks": len(records),
        "expected_tasks": len(expected_ids),
        "missing_or_invalid_task_ids": [],
        "path_passes": path_passes,
        "model_boundary_passes": boundary_passes,
        "validated_tasks": path_passes + boundary_passes,
        "path_failures": 0,
        "evaluator_ran_count": evaluator_ran_count,
        "evaluation_mode": "no-model-stub",
        "eval_model_call_attempts": eval_model_call_attempts,
        "external_model_calls": 0,
        "unique_sandboxes": len(set(sandbox_ids)),
        "all_recorded_sandboxes_unique": True,
    }
    summary_mismatches = [
        field
        for field, expected in expected_summary.items()
        if summary.get(field) != expected
        or type(summary.get(field)) is not type(expected)
    ]
    if summary_mismatches:
        raise ValueError(
            "no-model receipt summary does not reconcile: "
            + ", ".join(summary_mismatches)
        )

    required_ids = sorted(required_model_ids)
    missing = [task_id for task_id in required_ids if task_id not in model_ids]
    if missing:
        raise ValueError(
            "agent manifest omits model-dependent no-model task(s): "
            + ", ".join(missing)
        )
    unvalidated = sorted(model_ids - set(expected_ids))
    if unvalidated:
        raise ValueError(
            "agent manifest includes task(s) absent from the validated no-model "
            "manifest: " + ", ".join(unvalidated)
        )
    return required_ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-model-receipt", type=Path, required=True)
    parser.add_argument("--agent-manifest", type=Path, required=True)
    args = parser.parse_args()
    covered = verify_model_coverage(args.no_model_receipt, args.agent_manifest)
    print(f"model_dependent_tasks_covered={len(covered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
