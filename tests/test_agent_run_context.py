from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_prepare_agent_run_clears_only_expected_receipts_and_returns_nonce(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [{"id": "003"}, {"id": "035"}]}))
    workers = tmp_path / "workers"
    workers.mkdir()
    for name in ("task_003.json", "task_035.json", "task_999.json"):
        (workers / name).write_text("stale")

    result = subprocess.run(
        [
            "python3",
            str(ROOT / "runner" / "prepare_agent_run.py"),
            "--manifest",
            str(manifest),
            "--worker-dir",
            str(workers),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    uuid.UUID(result.stdout.strip())
    assert not (workers / "task_003.json").exists()
    assert not (workers / "task_035.json").exists()
    assert (workers / "task_999.json").read_text() == "stale"
