#!/usr/bin/env python3
"""Fetch and verify the private OSWorld V2 release payload at immutable revisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.release_lock import validate_release_lock  # noqa: E402

LOCKFILE = ROOT / "examples" / "osworld-v2" / "upstream.lock.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_task_snapshot(
    tasks_dir: Path, task_lock: dict, *, manifest_path: Path | None = None
) -> int:
    manifest_path = manifest_path or ROOT / task_lock["hash_manifest"]
    if not manifest_path.is_file():
        raise ValueError(f"task hash manifest missing: {manifest_path}")
    if _sha256(manifest_path) != task_lock["manifest_sha256"]:
        raise ValueError("task hash manifest does not match the release lock")
    try:
        manifest = json.loads(manifest_path.read_text())
        files = manifest["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"task hash manifest invalid: {type(exc).__name__}") from exc
    if manifest.get("source_revision") != task_lock["revision"]:
        raise ValueError(
            "task hash manifest source revision does not match the release lock"
        )
    if manifest.get("task_count") != task_lock["task_count"] or not isinstance(
        files, dict
    ):
        raise ValueError("task hash manifest count does not match the release lock")

    expected_names = set(files)
    actual_names = {path.name for path in tasks_dir.glob("task_*.py")}
    failures: list[str] = []
    if actual_names != expected_names:
        failures.append("task file set")
    for name, expected in files.items():
        path = tasks_dir / name
        if not path.is_file() or not isinstance(expected, dict):
            failures.append(name)
            continue
        if path.stat().st_size != expected.get("size") or _sha256(path) != expected.get(
            "sha256"
        ):
            failures.append(name)
    if failures:
        raise ValueError(
            "task snapshot integrity mismatch: " + ", ".join(sorted(failures)[:12])
        )
    return len(actual_names)


def _safe_replace_directory(staged: Path, target: Path) -> None:
    resolved = target.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}
    if resolved in forbidden:
        raise ValueError(f"refusing unsafe gated-data target: {resolved}")
    if target.exists():
        shutil.rmtree(target)
    os.replace(staged, target)


def fetch(tasks_dir: Path, assets_dir: Path, *, tasks_only: bool) -> None:
    from huggingface_hub import snapshot_download

    lock = validate_release_lock(LOCKFILE)
    task_lock = lock["tasks_data"]
    tasks_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="osworld-v2-tasks-") as temporary:
        staged = Path(
            snapshot_download(
                repo_id=task_lock["repository"],
                repo_type="dataset",
                revision=task_lock["revision"],
                allow_patterns=["task_*.py"],
                local_dir=temporary,
            )
        )
        verify_task_snapshot(staged, task_lock)
        for stale in tasks_dir.glob("task_*.py"):
            stale.unlink()
        for task in staged.glob("task_*.py"):
            shutil.copy2(task, tasks_dir / task.name)
    count = verify_task_snapshot(tasks_dir, task_lock)
    print(f"verified_task_files={count}")
    print(f"tasks_revision={task_lock['revision']}")

    if tasks_only:
        return
    asset_lock = lock["assets_data"]
    assets_dir.parent.mkdir(parents=True, exist_ok=True)
    staged_assets = Path(
        tempfile.mkdtemp(prefix=f".{assets_dir.name}-", dir=assets_dir.parent)
    )
    try:
        snapshot_download(
            repo_id=asset_lock["repository"],
            repo_type="dataset",
            revision=asset_lock["revision"],
            local_dir=staged_assets,
        )
        (staged_assets / ".source.json").write_text(
            json.dumps(asset_lock, indent=2, sort_keys=True) + "\n"
        )
        _safe_replace_directory(staged_assets, assets_dir)
    finally:
        if staged_assets.exists():
            shutil.rmtree(staged_assets)
    print(f"assets_revision={asset_lock['revision']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-dir", type=Path, default=ROOT / "tasks")
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--tasks-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock = validate_release_lock(LOCKFILE)
    if args.verify_only:
        count = verify_task_snapshot(args.tasks_dir, lock["tasks_data"])
        print(f"verified_task_files={count}")
        return 0
    assets_dir = args.assets_dir or args.tasks_dir / "assets"
    fetch(args.tasks_dir, assets_dir, tasks_only=args.tasks_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
