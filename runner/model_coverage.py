#!/usr/bin/env python3
"""Require full-model coverage for every no-model boundary-only task."""

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


def verify_model_coverage(no_model_receipt: Path, agent_manifest: Path) -> list[str]:
    no_model = _load_object(no_model_receipt, "no-model receipt")
    manifest = _load_object(agent_manifest, "agent manifest")
    records = no_model.get("records")
    tasks = manifest.get("tasks")
    if not isinstance(records, list) or not isinstance(tasks, list):
        raise ValueError("receipts and manifests must contain record/task lists")
    for field in ("release", "template", "osworld_commit"):
        receipt_value = no_model.get(field)
        manifest_value = manifest.get(field)
        if (
            not isinstance(receipt_value, str)
            or not receipt_value
            or not isinstance(manifest_value, str)
            or not manifest_value
        ):
            raise ValueError(f"no-model receipt and agent manifest require {field}")
        if no_model.get(field) != manifest.get(field):
            raise ValueError(
                f"no-model receipt {field} does not match the agent manifest"
            )
    summary = no_model.get("summary")
    external_model_calls = (
        summary.get("external_model_calls") if isinstance(summary, dict) else None
    )
    if type(external_model_calls) is not int or external_model_calls != 0:
        raise ValueError("no-model receipt does not attest zero external model calls")
    boundary_ids = sorted(
        record.get("id")
        for record in records
        if isinstance(record, dict)
        and record.get("path_status") == "MODEL_BOUNDARY_PASS"
        and isinstance(record.get("id"), str)
    )
    model_ids = {
        task.get("id")
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    missing = [task_id for task_id in boundary_ids if task_id not in model_ids]
    if missing:
        raise ValueError(
            "agent manifest omits no-model boundary task(s): " + ", ".join(missing)
        )
    return boundary_ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-model-receipt", type=Path, required=True)
    parser.add_argument("--agent-manifest", type=Path, required=True)
    args = parser.parse_args()
    covered = verify_model_coverage(args.no_model_receipt, args.agent_manifest)
    print(f"model_boundary_tasks_covered={len(covered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
