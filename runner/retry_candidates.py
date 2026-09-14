#!/usr/bin/env python3
"""Print 'task_id domain' rows eligible for the infrastructure retry wave."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from receipt_safety import RETRYABLE_ERROR_CAUSES  # noqa: E402


def main() -> int:
    manifest_path, worker_dir = sys.argv[1:3]
    for item in json.load(open(manifest_path))["tasks"]:
        task_id = item["id"]
        path = Path(worker_dir) / f"task_{task_id}.json"
        try:
            record = json.load(open(path))
        except (FileNotFoundError, json.JSONDecodeError):
            record = {}
        if not record or (
            record.get("path_status") != "OK"
            and record.get("error_cause") in RETRYABLE_ERROR_CAUSES
        ):
            print(task_id, item.get("domain", "release"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
