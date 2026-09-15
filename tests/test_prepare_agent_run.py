import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from prepare_agent_run import prepare  # noqa: E402


def test_prepare_clears_stale_attempt_files_and_retry_list(tmp_path):
    workers = tmp_path / "workers"
    workers.mkdir()
    for name in (
        "task_001.json",
        "task_001_before_retry_1.json",
        "task_001_retry_1.log",
        "task_001.log",
        "retries.json",
        "task_999.json",
    ):
        (workers / name).write_text("{}")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"tasks": [{"id": "001"}]}))
    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "runner/prepare_agent_run.py"),
            "--manifest",
            str(manifest),
            "--worker-dir",
            str(workers),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert len(out) == 36  # nonce
    assert sorted(p.name for p in workers.iterdir()) == ["task_999.json"]


def test_prepare_rejects_old_results_before_deleting_any_evidence(tmp_path):
    workers = tmp_path / "workers"
    workers.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [{"id": "001"}, {"id": "002"}]}))
    for result_name in ("task_002", "task_002_retry_1"):
        result = workers / result_name
        result.mkdir()
        (result / "result.txt").write_text("0.5")
        receipt = workers / "task_001.json"
        receipt.write_text("original evidence")
        with pytest.raises(ValueError, match="fresh"):
            prepare(manifest, workers)
        assert receipt.read_text() == "original evidence"
        assert (result / "result.txt").read_text() == "0.5"
        (result / "result.txt").unlink()
        result.rmdir()
