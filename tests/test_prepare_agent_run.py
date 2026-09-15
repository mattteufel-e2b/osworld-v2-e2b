import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
