#!/usr/bin/env python3
"""Prepare fresh per-task receipt slots and emit a coordinator run nonce."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path


def prepare(manifest_path: Path, worker_dir: Path) -> str:
    manifest = json.loads(manifest_path.read_text())
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("agent manifest must contain a tasks list")
    task_ids = [task.get("id") for task in tasks if isinstance(task, dict)]
    if len(task_ids) != len(tasks) or any(
        not isinstance(task_id, str) or not task_id for task_id in task_ids
    ):
        raise ValueError("agent manifest contains an invalid task id")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("agent manifest contains duplicate task ids")

    # Validate every task before removing any evidence from the directory.
    for task_id in task_ids:
        if (worker_dir / f"task_{task_id}").exists() or any(
            path.is_dir() for path in worker_dir.glob(f"task_{task_id}_retry_*")
        ):
            raise ValueError("task results already exist; use a fresh RAW_DIR")

    worker_dir.mkdir(parents=True, exist_ok=True)
    for task_id in task_ids:
        for pattern in (
            f"task_{task_id}.json",
            f"task_{task_id}.log",
            f"task_{task_id}_before_retry_*.json",
            f"task_{task_id}_retry_*.log",
        ):
            for stale in worker_dir.glob(pattern):
                stale.unlink()
    (worker_dir / "retries.json").unlink(missing_ok=True)
    return str(uuid.uuid4())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--worker-dir", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.manifest, args.worker_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
