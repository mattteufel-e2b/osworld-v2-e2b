from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from gated_data import verify_task_snapshot  # noqa: E402


def _snapshot(tmp_path: Path) -> tuple[Path, dict]:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task = tasks / "task_001.py"
    task.write_text("TASK = {}\n")
    payload = task.read_bytes()
    manifest = {
        "source_revision": "a" * 40,
        "task_count": 1,
        "files": {
            task.name: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        },
    }
    manifest_path = tmp_path / "task-hashes.json"
    manifest_path.write_text(json.dumps(manifest))
    lock = {
        "revision": "a" * 40,
        "hash_manifest": "examples/osworld-v2/task-hashes.json",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "task_count": 1,
    }
    return tasks, lock, manifest_path


def test_verify_task_snapshot_accepts_exact_content_addressed_payload(tmp_path):
    tasks, lock, manifest_path = _snapshot(tmp_path)

    assert verify_task_snapshot(tasks, lock, manifest_path=manifest_path) == 1


def test_verify_task_snapshot_rejects_modified_task(tmp_path):
    tasks, lock, manifest_path = _snapshot(tmp_path)
    (tasks / "task_001.py").write_text("TASK = {'modified': True}\n")

    with pytest.raises(ValueError, match="task snapshot integrity mismatch"):
        verify_task_snapshot(tasks, lock, manifest_path=manifest_path)
