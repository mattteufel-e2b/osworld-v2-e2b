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

    worker_dir.mkdir(parents=True, exist_ok=True)
    for task_id in task_ids:
        (worker_dir / f"task_{task_id}.json").unlink(missing_ok=True)
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
