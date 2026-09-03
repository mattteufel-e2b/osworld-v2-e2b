#!/usr/bin/env python3
"""Merge sharded/retry no-model receipts into one canonical safe receipt."""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


VALID_STATUSES = {"PATH_PASS", "MODEL_BOUNDARY_PASS", "PATH_FAIL"}


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _binding(value: dict, field: str, label: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{label} requires a nonempty {field}")
    return result


def _manifest_tasks(manifest: dict, label: str) -> tuple[list[dict], dict[str, dict]]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{label} requires a nonempty tasks list")
    by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError(f"{label} contains a non-object task")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{label} contains an invalid task id")
        if task_id in by_id:
            raise ValueError(f"{label} contains duplicate task id {task_id}")
        by_id[task_id] = task
    return tasks, by_id


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_score(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _public_sandbox(value: object, template: str) -> tuple[dict | None, bool]:
    if not isinstance(value, dict):
        return None, False
    sandbox_id = value.get("id")
    generation = value.get("generation")
    restricted_ingress = value.get("restricted_ingress")
    sandbox_template = value.get("template")
    structurally_valid = (
        isinstance(sandbox_id, str)
        and bool(sandbox_id)
        and _nonnegative_int(generation)
        and isinstance(restricted_ingress, bool)
        and isinstance(sandbox_template, str)
        and bool(sandbox_template)
    )
    if not structurally_valid:
        return None, False
    public = {
        "id": sandbox_id,
        "generation": generation,
        "restricted_ingress": restricted_ingress,
        "template": sandbox_template,
    }
    eligible = restricted_ingress is True and sandbox_template == template
    return public, eligible


def _source_summary(records: list[dict]) -> dict:
    sandbox_ids = [
        record["sandbox"]["id"]
        for record in records
        if isinstance(record.get("sandbox"), dict)
        and isinstance(record["sandbox"].get("id"), str)
        and record["sandbox"]["id"]
    ]
    path_passes = sum(record.get("path_status") == "PATH_PASS" for record in records)
    boundaries = sum(
        record.get("path_status") == "MODEL_BOUNDARY_PASS" for record in records
    )
    return {
        "tasks": len(records),
        "path_passes": path_passes,
        "model_boundary_passes": boundaries,
        "validated_tasks": path_passes + boundaries,
        "path_failures": sum(
            record.get("path_status") == "PATH_FAIL" for record in records
        ),
        "evaluator_ran_count": sum(
            record.get("evaluator_ran") is True for record in records
        ),
        "eval_model_call_attempts": sum(
            record["eval_model_call_attempts"] for record in records
        ),
        "external_model_calls": sum(
            record["external_model_calls"] for record in records
        ),
        "unique_sandboxes": len(set(sandbox_ids)),
        "recorded_sandboxes_unique": len(sandbox_ids) == len(set(sandbox_ids)),
    }


def _validate_summary(
    summary: object, records: list[dict], expected_tasks: int
) -> None:
    if not isinstance(summary, dict):
        raise ValueError("source receipt requires a summary object")
    computed = _source_summary(records)
    for field in (
        "tasks",
        "path_passes",
        "model_boundary_passes",
        "validated_tasks",
        "path_failures",
        "evaluator_ran_count",
        "unique_sandboxes",
    ):
        if (
            not _nonnegative_int(summary.get(field))
            or summary[field] != computed[field]
        ):
            raise ValueError(f"source summary {field} does not match its records")
    for field in ("eval_model_call_attempts", "external_model_calls"):
        if (
            not _nonnegative_int(summary.get(field))
            or summary[field] != computed[field]
        ):
            raise ValueError(f"source summary {field} does not match its records")
    if "expected_tasks" in summary and (
        not _nonnegative_int(summary["expected_tasks"])
        or summary["expected_tasks"] != expected_tasks
    ):
        raise ValueError("source summary expected_tasks does not match its manifest")
    if (
        "missing_or_invalid_task_ids" in summary
        and summary["missing_or_invalid_task_ids"] != []
    ):
        raise ValueError("source summary reports missing or invalid tasks")
    if "evaluation_mode" in summary and summary["evaluation_mode"] != "no-model-stub":
        raise ValueError("source summary evaluation_mode is not no-model-stub")
    uniqueness = summary.get("all_recorded_sandboxes_unique")
    if (
        not isinstance(uniqueness, bool)
        or uniqueness != computed["recorded_sandboxes_unique"]
    ):
        raise ValueError("source summary sandbox uniqueness does not match its records")


def _sanitize_record(
    record: dict,
    canonical_task: dict,
    *,
    input_index: int,
    record_index: int,
    template: str,
) -> tuple[dict, bool]:
    task_id = canonical_task["id"]
    if record.get("id") != task_id:
        raise ValueError(f"source record does not match manifest task {task_id}")
    if "domain" in canonical_task and record.get("domain") != canonical_task["domain"]:
        raise ValueError(f"source record domain does not match manifest task {task_id}")
    status = record.get("path_status")
    if status not in VALID_STATUSES:
        raise ValueError(f"source record {task_id} has an invalid path_status")
    evaluator_ran = record.get("evaluator_ran")
    if not isinstance(evaluator_ran, bool):
        raise ValueError(f"source record {task_id} has an invalid evaluator_ran value")
    if record.get("evaluation_mode") != "no-model-stub":
        raise ValueError(f"source record {task_id} is not a no-model attempt")
    external_calls = record.get("external_model_calls")
    if type(external_calls) is not int or external_calls != 0:
        raise ValueError(f"source record {task_id} has invalid external_model_calls")
    eval_attempts = record.get("eval_model_call_attempts")
    if not _nonnegative_int(eval_attempts):
        raise ValueError(
            f"source record {task_id} has invalid eval_model_call_attempts"
        )
    if status == "MODEL_BOUNDARY_PASS" and eval_attempts == 0:
        raise ValueError(f"source record {task_id} lacks a model-boundary attempt")

    score = record.get("score")
    score_valid = _valid_score(score)
    if score is not None and not score_valid:
        raise ValueError(f"source record {task_id} has an invalid score")
    sandbox, sandbox_eligible = _public_sandbox(record.get("sandbox"), template)
    public = {
        "input_index": input_index,
        "record_index": record_index,
        "id": task_id,
        "path_status": status,
        "evaluator_ran": evaluator_ran,
        "score": float(score) if score_valid else None,
        "evaluation_mode": "no-model-stub",
        "external_model_calls": 0,
        "eval_model_call_attempts": eval_attempts,
    }
    if "domain" in canonical_task:
        public["domain"] = canonical_task["domain"]
    duration = record.get("duration_seconds")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(float(duration))
        and duration >= 0
    ):
        public["duration_seconds"] = float(duration)
    if isinstance(record.get("multiphase"), bool):
        public["multiphase"] = record["multiphase"]
    if sandbox is not None:
        public["sandbox"] = sandbox
    eligible = (
        status in {"PATH_PASS", "MODEL_BOUNDARY_PASS"}
        and evaluator_ran is True
        and score_valid
        and sandbox_eligible
    )
    return public, eligible


def _receipt_paths(inputs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for path in inputs:
        if path.is_dir():
            paths.extend(
                sorted(child for child in path.iterdir() if child.suffix == ".json")
            )
        else:
            paths.append(path)
    if not paths:
        raise ValueError("at least one source receipt is required")
    return paths


def merge_receipts(manifest_path: Path, receipt_inputs: list[Path]) -> dict:
    canonical = _load_object(manifest_path, "canonical manifest")
    bindings = {
        field: _binding(canonical, field, "canonical manifest")
        for field in ("release", "template", "osworld_commit")
    }
    canonical_tasks, canonical_by_id = _manifest_tasks(canonical, "canonical manifest")
    public_tasks = []
    for task in canonical_tasks:
        public_task = {"id": task["id"]}
        if "domain" in task:
            domain = task["domain"]
            if not isinstance(domain, str) or not domain:
                raise ValueError("canonical manifest contains an invalid task domain")
            public_task["domain"] = domain
        public_tasks.append(public_task)
    public_manifest = {**bindings, "tasks": public_tasks}
    attempts: list[dict] = []
    selected_attempt: dict[str, int] = {}
    paths = _receipt_paths(receipt_inputs)

    for input_index, path in enumerate(paths):
        source = _load_object(path, f"source receipt {input_index}")
        embedded = source.get("manifest")
        if not isinstance(embedded, dict):
            raise ValueError(
                f"source receipt {input_index} requires an embedded manifest"
            )
        for field, expected in bindings.items():
            if _binding(source, field, f"source receipt {input_index}") != expected:
                raise ValueError(f"source receipt {input_index} {field} mismatch")
            if _binding(embedded, field, f"source manifest {input_index}") != expected:
                raise ValueError(f"source manifest {input_index} {field} mismatch")
        source_tasks, source_by_id = _manifest_tasks(
            embedded, f"source manifest {input_index}"
        )
        for task_id, task in source_by_id.items():
            if task_id not in canonical_by_id or task != canonical_by_id[task_id]:
                raise ValueError(f"source manifest task {task_id} is not canonical")
        records = source.get("records")
        if not isinstance(records, list):
            raise ValueError(f"source receipt {input_index} requires a records list")
        source_ids = [task["id"] for task in source_tasks]
        record_ids = [
            record.get("id") if isinstance(record, dict) else None for record in records
        ]
        if (
            any(not isinstance(task_id, str) for task_id in record_ids)
            or len(record_ids) != len(set(record_ids))
            or set(record_ids) != set(source_ids)
        ):
            raise ValueError(
                f"source receipt {input_index} records do not exactly cover its manifest"
            )
        sanitized_source: list[dict] = []
        eligible_source: list[bool] = []
        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(
                    f"source receipt {input_index} contains a non-object record"
                )
            task = source_by_id[record["id"]]
            public, eligible = _sanitize_record(
                record,
                task,
                input_index=input_index,
                record_index=record_index,
                template=bindings["template"],
            )
            sanitized_source.append(public)
            eligible_source.append(eligible)
        _validate_summary(source.get("summary"), records, len(source_tasks))
        for public, eligible in zip(sanitized_source, eligible_source, strict=True):
            attempt_index = len(attempts)
            attempts.append(public)
            if eligible:
                selected_attempt[public["id"]] = attempt_index

    missing = [
        task["id"] for task in canonical_tasks if task["id"] not in selected_attempt
    ]
    if missing:
        raise ValueError(
            "canonical tasks lack an eligible no-model attempt: " + ", ".join(missing)
        )

    selected_records: list[dict] = []
    selected_indices = set(selected_attempt.values())
    for task in canonical_tasks:
        attempt = attempts[selected_attempt[task["id"]]]
        selected = {
            key: value
            for key, value in attempt.items()
            if key not in {"input_index", "record_index"}
        }
        selected_records.append(selected)
    sandbox_ids = [record["sandbox"]["id"] for record in selected_records]
    if len(sandbox_ids) != len(set(sandbox_ids)):
        raise ValueError("selected no-model attempts do not use unique sandboxes")
    for record in selected_records:
        record["sandbox"]["unique_in_run"] = True
    for index, attempt in enumerate(attempts):
        attempt["selected"] = index in selected_indices

    path_passes = sum(
        record["path_status"] == "PATH_PASS" for record in selected_records
    )
    boundaries = sum(
        record["path_status"] == "MODEL_BOUNDARY_PASS" for record in selected_records
    )
    summary = {
        "tasks": len(selected_records),
        "expected_tasks": len(canonical_tasks),
        "missing_or_invalid_task_ids": [],
        "path_passes": path_passes,
        "model_boundary_passes": boundaries,
        "validated_tasks": path_passes + boundaries,
        "path_failures": 0,
        "evaluator_ran_count": sum(
            record["evaluator_ran"] for record in selected_records
        ),
        "evaluation_mode": "no-model-stub",
        "eval_model_call_attempts": sum(
            record["eval_model_call_attempts"] for record in selected_records
        ),
        "external_model_calls": 0,
        "unique_sandboxes": len(set(sandbox_ids)),
        "all_recorded_sandboxes_unique": True,
        "attempts": len(attempts),
        "failed_attempts": sum(
            attempt["path_status"] == "PATH_FAIL" for attempt in attempts
        ),
        "eligible_attempts": sum(
            attempt["path_status"] in {"PATH_PASS", "MODEL_BOUNDARY_PASS"}
            and attempt["evaluator_ran"] is True
            and _valid_score(attempt["score"])
            and isinstance(attempt.get("sandbox"), dict)
            and attempt["sandbox"].get("restricted_ingress") is True
            and attempt["sandbox"].get("template") == bindings["template"]
            for attempt in attempts
        ),
    }
    return {
        "schema_version": 2,
        "run_id": str(uuid.uuid4()),
        "finished_at": datetime.now(UTC).isoformat(),
        "purpose": "merged environment-path validation; not an agent benchmark score",
        **bindings,
        "manifest": public_manifest,
        "execution": {
            "mode": "merged-sharded-retry-receipts",
            "source_receipts": len(paths),
            "selection_order": "latest-eligible-by-input-order",
        },
        "records": selected_records,
        "attempt_history": attempts,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("receipts", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        merged = merge_receipts(args.manifest, args.receipts)
    except ValueError as exc:
        print(f"no-model receipt merge failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    print(json.dumps(merged["summary"], sort_keys=True))
    print("NO-MODEL MERGE GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
