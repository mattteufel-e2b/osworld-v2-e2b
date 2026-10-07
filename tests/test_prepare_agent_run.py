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


def _resume(workers: Path, manifest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "runner/prepare_agent_run.py"),
            "--manifest",
            str(manifest),
            "--worker-dir",
            str(workers),
            "--resume",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _resume_tree(tmp_path: Path, *, second_nonce: str = "run-nonce") -> tuple:
    """Two scored tasks (one scored on a retry attempt) and one unscored."""
    workers = tmp_path / "workers"
    workers.mkdir()
    (workers / "task_001").mkdir()
    (workers / "task_001" / "result.txt").write_text("1.0")
    (workers / "task_001.json").write_text(json.dumps({"run_nonce": "run-nonce"}))
    (workers / "task_001.log").write_text("first")
    (workers / "task_002_retry_1").mkdir()
    (workers / "task_002_retry_1" / "result.txt").write_text("0.0")
    (workers / "task_002.json").write_text(json.dumps({"run_nonce": second_nonce}))
    (workers / "task_002_retry_1.log").write_text("second")
    (workers / "task_003.json").write_text(json.dumps({"run_nonce": "run-nonce"}))
    (workers / "task_003.log").write_text("third")
    (workers / "task_003_before_retry_1.json").write_text("{}")
    (workers / "retries.json").write_text("[]")
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps({"tasks": [{"id": "001"}, {"id": "002"}, {"id": "003"}]})
    )
    return workers, manifest


def test_resume_keeps_scored_tasks_and_recovers_their_nonce(tmp_path):
    workers, manifest = _resume_tree(tmp_path)

    result = _resume(workers, manifest)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "run-nonce"
    assert (
        result.stderr.strip() == "resuming run-nonce: skipping 2 scored tasks: 001 002"
    )
    for kept in (
        "task_001.json",
        "task_001.log",
        "task_001/result.txt",
        "task_002.json",
        "task_002_retry_1.log",
        "task_002_retry_1/result.txt",
        "retries.json",
    ):
        assert (workers / kept).exists(), kept
    for cleared in ("task_003.json", "task_003.log", "task_003_before_retry_1.json"):
        assert not (workers / cleared).exists(), cleared


def test_resume_rejects_disagreeing_nonces(tmp_path):
    workers, manifest = _resume_tree(tmp_path, second_nonce="other-nonce")

    result = _resume(workers, manifest)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "run_nonce" in result.stderr
    assert (workers / "task_003.json").exists()  # a rejected resume changes nothing


def test_resume_starts_fresh_when_nothing_has_ever_run(tmp_path):
    """The most common resume: interrupted before any task scored. This must
    behave like a fresh run, not raise -- see the live-run evidence in the
    commit that introduced this test."""
    workers = tmp_path / "workers"
    workers.mkdir()
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"tasks": [{"id": "001"}]}))

    result = _resume(workers, manifest)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert len(result.stdout.strip()) == 36  # nonce
    assert (
        result.stderr.strip()
        == f"resuming {workers}: no scored tasks yet; starting a fresh nonce"
    )


def test_resume_clears_stale_unscored_receipts_when_nothing_scored(tmp_path):
    workers = tmp_path / "workers"
    workers.mkdir()
    (workers / "task_001.json").write_text(json.dumps({"error_cause": "timeout"}))
    (workers / "task_001.log").write_text("attempt")
    (workers / "task_001_before_retry_1.json").write_text("{}")
    (workers / "task_001_retry_1.log").write_text("retry")
    (workers / "retries.json").write_text("[]")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"tasks": [{"id": "001"}]}))

    result = _resume(workers, manifest)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert len(result.stdout.strip()) == 36
    assert (
        result.stderr.strip()
        == f"resuming {workers}: no scored tasks yet; starting a fresh nonce"
    )
    assert sorted(p.name for p in workers.iterdir()) == []


def test_resume_rejects_a_scored_task_with_no_recoverable_nonce(tmp_path):
    workers = tmp_path / "workers"
    workers.mkdir()
    (workers / "task_001").mkdir()
    (workers / "task_001" / "result.txt").write_text("1.0")
    (workers / "task_001.json").write_text(json.dumps({"path_status": "OK"}))
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"tasks": [{"id": "001"}]}))

    result = _resume(workers, manifest)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "nothing to resume" in result.stderr
    assert (workers / "task_001.json").exists()  # a rejected resume changes nothing
