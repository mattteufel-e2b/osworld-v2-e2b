#!/usr/bin/env python3
"""Fail-fast validation of local and run-scoped OSWorld V2 inputs."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.release_lock import validate_release_lock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gated_data import verify_task_snapshot  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        fail(f"required {label} missing: {path}")


def require_private_file(path: Path, label: str) -> None:
    require_file(path, label)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        fail(f"{label} must not be group/world accessible: {path}")


def load_json(path: Path, label: str) -> object:
    require_file(path, label)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"invalid {label}: {path}: {type(exc).__name__}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--osworld-root", type=Path, required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--services-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--task-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_path = args.services_dir / ".runtime.json"
    token_path = args.services_dir / ".gitlab-token"
    require_private_file(runtime_path, "service runtime file")
    require_private_file(token_path, "GitLab token file")

    runtime = load_json(runtime_path, "service runtime file")
    if not isinstance(runtime, dict):
        fail("service runtime file must contain an object")
    required_runtime = {
        "websites": (
            "sandbox_id",
            "template",
            "traffic_token",
            "public_host_suffix",
            "sites",
        ),
        "gitlab": ("sandbox_id", "template", "traffic_token", "url", "private_token"),
    }
    campaign_id = os.environ.get("OSWORLD_CAMPAIGN_ID")
    if not campaign_id:
        fail("OSWORLD_CAMPAIGN_ID is required")
    for section, keys in required_runtime.items():
        value = runtime.get(section)
        if not isinstance(value, dict):
            fail(f"service runtime file missing {section} object")
        missing = [key for key in keys if not value.get(key)]
        if missing:
            fail(
                f"service runtime {section} missing required fields: {', '.join(missing)}"
            )
        if value.get("campaign_id") != campaign_id:
            fail(f"service runtime {section} belongs to a different campaign")

    if not token_path.read_text().strip():
        fail(f"GitLab token file is empty: {token_path}")
    if token_path.read_text().strip() != runtime["gitlab"]["private_token"]:
        fail("GitLab token file does not match service runtime")

    if not args.osworld_root.is_dir():
        fail(f"required patched OSWorld checkout missing: {args.osworld_root}")
    require_file(args.osworld_root / "e2b_relay.py", "patched OSWorld relay")
    if not args.tasks_dir.is_dir():
        fail(f"required gated tasks directory missing: {args.tasks_dir}")
    if not (args.tasks_dir / "assets").is_dir():
        fail(
            f"required gated task assets directory missing: {args.tasks_dir / 'assets'}"
        )
    try:
        release_lock = validate_release_lock(
            ROOT / "examples" / "osworld-v2" / "upstream.lock.json"
        )
        verify_task_snapshot(args.tasks_dir, release_lock["tasks_data"])
    except ValueError as exc:
        fail(str(exc))

    manifest = load_json(args.manifest, "validation manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list):
        fail("validation manifest must contain a tasks list")
    if manifest.get("template") != os.environ.get("GUEST_TEMPLATE"):
        fail("validation manifest template does not match GUEST_TEMPLATE")
    task_ids = [item.get("id") for item in manifest["tasks"] if isinstance(item, dict)]
    if len(task_ids) != len(manifest["tasks"]) or any(
        not isinstance(task_id, str) or not task_id.isdigit() for task_id in task_ids
    ):
        fail("validation manifest contains an invalid task id")
    if len(task_ids) != len(set(task_ids)):
        fail("validation manifest contains duplicate task ids")
    selected = [args.task_id] if args.task_id else task_ids
    if args.task_id and args.task_id not in task_ids:
        fail(f"task {args.task_id} is absent from validation manifest")
    missing_tasks = [
        task_id
        for task_id in selected
        if not (args.tasks_dir / f"task_{task_id}.py").is_file()
    ]
    if missing_tasks:
        fail(f"gated task files missing for ids: {', '.join(missing_tasks)}")

    print(f"runner preflight ok: {len(selected)} task(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
