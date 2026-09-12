from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runner" / "retry_candidates.py"


def _write_manifest(tmp_path: Path, task_ids: list[str]) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"tasks": [{"id": tid, "domain": "release"} for tid in task_ids]})
    )
    return manifest


def _write_receipt(worker_dir: Path, task_id: str, payload: dict) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / f"task_{task_id}.json").write_text(json.dumps(payload))


def _run(manifest: Path, worker_dir: Path) -> list[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest), str(worker_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_task_timeout_receipt_is_selected_for_retry(tmp_path):
    worker_dir = tmp_path / "workers"
    manifest = _write_manifest(tmp_path, ["001"])
    _write_receipt(
        worker_dir, "001", {"path_status": "FAIL", "error_cause": "task-timeout"}
    )

    rows = _run(manifest, worker_dir)

    assert rows == ["001 release"]


def test_evaluator_or_agent_receipt_is_not_selected(tmp_path):
    worker_dir = tmp_path / "workers"
    manifest = _write_manifest(tmp_path, ["002"])
    _write_receipt(
        worker_dir, "002", {"path_status": "FAIL", "error_cause": "evaluator-or-agent"}
    )

    rows = _run(manifest, worker_dir)

    assert rows == []


def test_missing_receipt_is_selected(tmp_path):
    worker_dir = tmp_path / "workers"
    manifest = _write_manifest(tmp_path, ["003"])
    worker_dir.mkdir(parents=True, exist_ok=True)

    rows = _run(manifest, worker_dir)

    assert rows == ["003 release"]


def test_ok_receipt_is_not_selected(tmp_path):
    worker_dir = tmp_path / "workers"
    manifest = _write_manifest(tmp_path, ["004"])
    _write_receipt(worker_dir, "004", {"path_status": "OK"})

    rows = _run(manifest, worker_dir)

    assert rows == []


def test_mixed_manifest_selects_only_retryable_rows(tmp_path):
    worker_dir = tmp_path / "workers"
    manifest = _write_manifest(tmp_path, ["001", "002", "003", "004"])
    _write_receipt(
        worker_dir, "001", {"path_status": "FAIL", "error_cause": "task-timeout"}
    )
    _write_receipt(
        worker_dir, "002", {"path_status": "FAIL", "error_cause": "evaluator-or-agent"}
    )
    _write_receipt(worker_dir, "004", {"path_status": "OK"})
    # "003" has no receipt file at all.

    rows = _run(manifest, worker_dir)

    assert rows == ["001 release", "003 release"]
