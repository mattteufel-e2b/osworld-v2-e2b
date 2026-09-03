from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from merge_no_model_receipts import merge_receipts  # noqa: E402


TEMPLATE = "osworld-v2-gnome:11111111-2222-3333-4444-555555555555"
RELEASE = "osworld-v2-2026.08.08"
COMMIT = "a" * 40


def _manifest(*task_ids: str) -> dict:
    return {
        "release": RELEASE,
        "template": TEMPLATE,
        "osworld_commit": COMMIT,
        "tasks": [{"id": task_id, "domain": "test"} for task_id in task_ids],
    }


def _record(
    task_id: str,
    status: str,
    *,
    sandbox_id: str | None = None,
    evaluator_ran: bool | None = None,
) -> dict:
    boundary = status == "MODEL_BOUNDARY_PASS"
    record = {
        "id": task_id,
        "domain": "test",
        "path_status": status,
        "evaluator_ran": (
            status in {"PATH_PASS", "MODEL_BOUNDARY_PASS"}
            if evaluator_ran is None
            else evaluator_ran
        ),
        "score": 0.0 if status == "PATH_FAIL" else 0.25,
        "evaluation_mode": "no-model-stub",
        "external_model_calls": 0,
        "eval_model_call_attempts": 1 if boundary else 0,
        "duration_seconds": 3.5,
        "detail": "SECRET ERROR TEXT",
        "prompt": "SECRET PROMPT",
        "api_key": "SECRET KEY",
    }
    if sandbox_id is not None:
        record["sandbox"] = {
            "id": sandbox_id,
            "generation": 1,
            "restricted_ingress": True,
            "template": TEMPLATE,
            "traffic_token": "SECRET TOKEN",
        }
    return record


def _summary(records: list[dict]) -> dict:
    sandbox_ids = [
        record["sandbox"]["id"]
        for record in records
        if isinstance(record.get("sandbox"), dict)
        and isinstance(record["sandbox"].get("id"), str)
        and record["sandbox"]["id"]
    ]
    path_passes = sum(record["path_status"] == "PATH_PASS" for record in records)
    boundaries = sum(
        record["path_status"] == "MODEL_BOUNDARY_PASS" for record in records
    )
    return {
        "tasks": len(records),
        "expected_tasks": len(records),
        "missing_or_invalid_task_ids": [],
        "path_passes": path_passes,
        "model_boundary_passes": boundaries,
        "validated_tasks": path_passes + boundaries,
        "path_failures": sum(
            record["path_status"] == "PATH_FAIL" for record in records
        ),
        "evaluator_ran_count": sum(
            record["evaluator_ran"] is True for record in records
        ),
        "evaluation_mode": "no-model-stub",
        "eval_model_call_attempts": sum(
            record["eval_model_call_attempts"] for record in records
        ),
        "external_model_calls": 0,
        "unique_sandboxes": len(set(sandbox_ids)),
        "all_recorded_sandboxes_unique": len(sandbox_ids) == len(set(sandbox_ids)),
    }


def _receipt(tasks: tuple[str, ...], records: list[dict]) -> dict:
    manifest = _manifest(*tasks)
    return {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "release": RELEASE,
        "template": TEMPLATE,
        "osworld_commit": COMMIT,
        "manifest": manifest,
        "records": records,
        "summary": _summary(records),
        "host": {"platform": "SECRET HOST"},
    }


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value))
    return path


def test_merge_selects_latest_success_and_preserves_sanitized_attempt_order(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001", "002"))
    first = _write(
        tmp_path / "first.json",
        _receipt(
            ("001", "002"),
            [
                _record("001", "PATH_FAIL"),
                _record("002", "MODEL_BOUNDARY_PASS", sandbox_id="sandbox-002"),
            ],
        ),
    )
    retry = _write(
        tmp_path / "retry.json",
        _receipt(
            ("001",),
            [_record("001", "PATH_PASS", sandbox_id="sandbox-001")],
        ),
    )

    merged = merge_receipts(manifest_path, [first, retry])

    assert [record["id"] for record in merged["records"]] == ["001", "002"]
    assert merged["records"][0]["sandbox"]["id"] == "sandbox-001"
    assert [attempt["id"] for attempt in merged["attempt_history"]] == [
        "001",
        "002",
        "001",
    ]
    assert [attempt["selected"] for attempt in merged["attempt_history"]] == [
        False,
        True,
        True,
    ]
    assert merged["summary"] == {
        "tasks": 2,
        "expected_tasks": 2,
        "missing_or_invalid_task_ids": [],
        "path_passes": 1,
        "model_boundary_passes": 1,
        "validated_tasks": 2,
        "path_failures": 0,
        "evaluator_ran_count": 2,
        "evaluation_mode": "no-model-stub",
        "eval_model_call_attempts": 1,
        "external_model_calls": 0,
        "unique_sandboxes": 2,
        "all_recorded_sandboxes_unique": True,
        "attempts": 3,
        "failed_attempts": 1,
        "eligible_attempts": 2,
    }
    serialized = json.dumps(merged)
    for forbidden in (
        "SECRET ERROR TEXT",
        "SECRET PROMPT",
        "SECRET KEY",
        "SECRET TOKEN",
        "SECRET HOST",
        "detail",
        "prompt",
        "api_key",
        "traffic_token",
    ):
        assert forbidden not in serialized


def test_merge_uses_input_order_instead_of_untrusted_timestamps(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    older_input = _receipt(
        ("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-old")]
    )
    older_input["records"][0]["finished_at"] = "2999-01-01T00:00:00Z"
    newer_input = _receipt(
        ("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-new")]
    )
    newer_input["records"][0]["finished_at"] = "1900-01-01T00:00:00Z"

    merged = merge_receipts(
        manifest_path,
        [
            _write(tmp_path / "older-input.json", older_input),
            _write(tmp_path / "newer-input.json", newer_input),
        ],
    )

    assert merged["records"][0]["sandbox"]["id"] == "sandbox-new"
    assert [attempt["selected"] for attempt in merged["attempt_history"]] == [
        False,
        True,
    ]


def test_merge_reconciles_source_records_by_id_not_manifest_order(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001", "002"))
    source = _receipt(
        ("001", "002"),
        [
            _record("002", "PATH_PASS", sandbox_id="sandbox-002"),
            _record("001", "PATH_PASS", sandbox_id="sandbox-001"),
        ],
    )

    merged = merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])

    assert [record["id"] for record in merged["records"]] == ["001", "002"]
    assert [attempt["id"] for attempt in merged["attempt_history"]] == ["002", "001"]


def test_merge_preserves_positive_attempt_count_on_path_pass(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    record = _record("001", "PATH_PASS", sandbox_id="sandbox-001")
    record["eval_model_call_attempts"] = 1
    source = _receipt(("001",), [record])

    merged = merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])

    assert merged["records"][0]["path_status"] == "PATH_PASS"
    assert merged["records"][0]["eval_model_call_attempts"] == 1
    assert merged["summary"]["eval_model_call_attempts"] == 1


@pytest.mark.parametrize("field", ["release", "template", "osworld_commit"])
def test_merge_rejects_source_binding_mismatch(tmp_path, field):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source[field] = "wrong"

    with pytest.raises(ValueError, match=field):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_rejects_source_summary_that_disagrees_with_records(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["summary"]["path_passes"] = 0

    with pytest.raises(ValueError, match="summary path_passes"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_requires_exact_recorded_sandbox_uniqueness_summary(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001", "002"))
    source = _receipt(
        ("001", "002"),
        [
            _record("001", "PATH_PASS", sandbox_id="sandbox-001"),
            _record("002", "PATH_FAIL"),
        ],
    )
    source["summary"]["all_recorded_sandboxes_unique"] = False

    with pytest.raises(ValueError, match="sandbox uniqueness"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


@pytest.mark.parametrize(
    ("field", "value"),
    [("tasks", True), ("path_passes", 1.0), ("external_model_calls", False)],
)
def test_merge_rejects_noninteger_source_summary_counts(tmp_path, field, value):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["summary"][field] = value

    with pytest.raises(ValueError, match=f"summary {field}"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


@pytest.mark.parametrize("field", ["eval_model_call_attempts", "external_model_calls"])
def test_merge_requires_no_model_call_counters_in_source_summary(tmp_path, field):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    del source["summary"][field]

    with pytest.raises(ValueError, match=f"summary {field}"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_model_calls", 1),
        ("external_model_calls", False),
        ("eval_model_call_attempts", -1),
        ("eval_model_call_attempts", True),
    ],
)
def test_merge_rejects_invalid_no_model_call_counters(tmp_path, field, value):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["records"][0][field] = value
    source["summary"] = _summary(source["records"])

    with pytest.raises(ValueError, match=field):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_rejects_missing_canonical_success(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001", "002"))
    source = _receipt(
        ("001", "002"),
        [
            _record("001", "PATH_PASS", sandbox_id="sandbox-001"),
            _record("002", "PATH_FAIL"),
        ],
    )

    with pytest.raises(ValueError, match="002"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_rejects_reused_selected_sandbox(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001", "002"))
    source = _receipt(
        ("001", "002"),
        [
            _record("001", "PATH_PASS", sandbox_id="reused-sandbox"),
            _record("002", "PATH_PASS", sandbox_id="reused-sandbox"),
        ],
    )

    with pytest.raises(ValueError, match="unique"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_rejects_source_record_manifest_mismatch(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["manifest"]["tasks"][0]["domain"] = "wrong-domain"

    with pytest.raises(ValueError, match="manifest task"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_rejects_malformed_record_id_without_crashing(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["records"][0]["id"] = []

    with pytest.raises(ValueError, match="records do not exactly cover"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_rejects_success_without_complete_sandbox_attestation(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["records"][0]["sandbox"]["restricted_ingress"] = False

    with pytest.raises(ValueError, match="001"):
        merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])


def test_merge_cli_writes_receipt_only_after_gate_passes(tmp_path):
    manifest_path = _write(tmp_path / "manifest.json", _manifest("001"))
    source = _write(
        tmp_path / "source.json",
        _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")]),
    )
    output = tmp_path / "merged.json"

    result = subprocess.run(
        [
            "python3",
            str(
                Path(__file__).resolve().parents[1]
                / "runner"
                / "merge_no_model_receipts.py"
            ),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output),
            str(source),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "NO-MODEL MERGE GATE: PASS" in result.stdout
    assert json.loads(output.read_text())["summary"]["validated_tasks"] == 1


def test_merge_sanitizes_canonical_manifest_to_approved_metadata(tmp_path):
    canonical = _manifest("001")
    canonical["prompt"] = "SECRET CANONICAL PROMPT"
    canonical["api_key"] = "SECRET CANONICAL KEY"
    canonical["tasks"][0]["private_note"] = "SECRET TASK NOTE"
    manifest_path = _write(tmp_path / "manifest.json", canonical)
    source = _receipt(("001",), [_record("001", "PATH_PASS", sandbox_id="sandbox-001")])
    source["manifest"] = canonical

    merged = merge_receipts(manifest_path, [_write(tmp_path / "source.json", source)])

    assert merged["manifest"] == {
        "release": RELEASE,
        "template": TEMPLATE,
        "osworld_commit": COMMIT,
        "tasks": [{"id": "001", "domain": "test"}],
    }
    serialized = json.dumps(merged)
    assert "SECRET CANONICAL PROMPT" not in serialized
    assert "SECRET CANONICAL KEY" not in serialized
    assert "SECRET TASK NOTE" not in serialized
