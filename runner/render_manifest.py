#!/usr/bin/env python3
"""Render a run-scoped manifest for one immutable guest build."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2b_policy import require_immutable_template_ref  # noqa: E402
from services.release_lock import validate_release_lock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="include only this task id; repeat to create a sample manifest",
    )
    args = parser.parse_args()
    template = require_immutable_template_ref(args.template, "--template")
    manifest = json.loads(args.source.read_text())
    lock = validate_release_lock(
        ROOT / "examples" / "osworld-v2" / "upstream.lock.json"
    )
    manifest["template"] = template
    manifest["release"] = lock["release"]
    manifest["osworld_commit"] = lock["code"]["commit"]
    if args.task_ids:
        if len(args.task_ids) != len(set(args.task_ids)):
            parser.error("--task-id values must be unique")
        tasks_by_id = {task["id"]: task for task in manifest["tasks"]}
        unknown = [task_id for task_id in args.task_ids if task_id not in tasks_by_id]
        if unknown:
            parser.error(f"unknown task id(s): {', '.join(unknown)}")
        manifest["tasks"] = [tasks_by_id[task_id] for task_id in args.task_ids]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
