from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from aggregate_agent import aggregate  # noqa: E402


TEMPLATE = "osworld-v2-gnome:11111111-2222-3333-4444-555555555555"


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "release": "osworld-v2-2026.08.08",
                "template": TEMPLATE,
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "001", "domain": "release"}],
            }
        )
    )
    workers = tmp_path / "workers"
    workers.mkdir()
    (workers / "task_001.json").write_text(
        json.dumps(
            {
                "id": "001",
                "template": TEMPLATE,
                "path_status": "OK",
                "evaluator_ran": True,
                "score": 0.25,
                "sandbox_id": "sandbox-1",
                "restricted_ingress": True,
                "steps_taken": 2,
                "model": "model",
                "agent_kind": "m3",
                "eval_model": "judge",
                "model_transport": "https://example.test/v1",
                "eval_model_transport": "https://example.test/v1",
            }
        )
    )
    return manifest, workers


def test_agent_aggregate_accepts_complete_attested_record(tmp_path):
    manifest, workers = _inputs(tmp_path)

    run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="m3",
        model_transport="https://user:secret@example.test/v1?token=secret",
        eval_model="judge",
        eval_transport="https://example.test/v1?token=secret",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        task_082_concurrent=True,
    )

    assert ok
    assert run["summary"]["attested_records"] == 1
    assert run["model_transport"] == "https://example.test/v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [("template", "wrong"), ("score", float("nan")), ("steps_taken", 0)],
)
def test_agent_aggregate_rejects_unattested_or_invalid_records(tmp_path, field, value):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record[field] = value
    receipt.write_text(json.dumps(record))

    run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="m3",
        model_transport="https://example.test/v1",
        eval_model="judge",
        eval_transport="https://example.test/v1",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        task_082_concurrent=True,
    )

    assert not ok
    assert run["summary"]["attested_records"] == 0
