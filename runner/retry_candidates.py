#!/usr/bin/env python3
"""Print 'task_id domain' rows eligible for the infrastructure retry wave."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from receipt_safety import RETRYABLE_ERROR_CAUSES  # noqa: E402


def _scored(worker_dir: Path, task_id: str) -> bool:
    """True once any attempt produced a score. A scored rollout is never retried
    -- a retry would overwrite a real evaluation -- even if its receipt is
    missing or unparseable."""
    if (worker_dir / f"task_{task_id}" / "result.txt").exists():
        return True
    return any(worker_dir.glob(f"task_{task_id}_retry_*/result.txt"))


def main() -> int:
    manifest_path, worker_dir = sys.argv[1:3]
    for item in json.load(open(manifest_path))["tasks"]:
        task_id = item["id"]
        path = Path(worker_dir) / f"task_{task_id}.json"
        try:
            record = json.load(open(path))
        except (FileNotFoundError, json.JSONDecodeError):
            record = {}
        if _scored(Path(worker_dir), task_id):
            continue
        if not record or (
            record.get("path_status") != "OK"
            and record.get("error_cause") in RETRYABLE_ERROR_CAUSES
        ):
            print(task_id, item.get("domain", "release"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
