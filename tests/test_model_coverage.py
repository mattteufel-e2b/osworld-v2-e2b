from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from model_coverage import verify_model_coverage  # noqa: E402


RELEASE = "release-1"
TEMPLATE = "template:11111111-2222-3333-4444-555555555555"
OSWORLD_COMMIT = "a" * 40


def _manifest(task_ids: list[str], **overrides) -> dict:
    manifest = {
        "release": RELEASE,
        "template": TEMPLATE,
        "osworld_commit": OSWORLD_COMMIT,
        "tasks": [{"id": task_id} for task_id in task_ids],
    }
    manifest.update(overrides)
    return manifest


def _record(
    task_id: str,
    *,
    path_status: str = "PATH_PASS",
    call_attempts: int = 0,
    sandbox_id: str | None = None,
) -> dict:
    return {
        "id": task_id,
        "path_status": path_status,
        "evaluator_ran": True,
        "external_model_calls": 0,
        "eval_model_call_attempts": call_attempts,
        "sandbox": {"id": sandbox_id or f"sandbox-{task_id}"},
    }


def _receipt(records: list[dict], *, manifest: dict | None = None) -> dict:
    embedded_manifest = manifest or _manifest([record["id"] for record in records])
    path_passes = sum(record["path_status"] == "PATH_PASS" for record in records)
    boundary_passes = sum(
        record["path_status"] == "MODEL_BOUNDARY_PASS" for record in records
    )
    sandbox_ids = [record["sandbox"]["id"] for record in records]
    return {
        "release": embedded_manifest["release"],
        "template": embedded_manifest["template"],
        "osworld_commit": embedded_manifest["osworld_commit"],
        "manifest": embedded_manifest,
        "summary": {
            "tasks": len(records),
            "expected_tasks": len(embedded_manifest["tasks"]),
            "missing_or_invalid_task_ids": [],
            "path_passes": path_passes,
            "model_boundary_passes": boundary_passes,
            "validated_tasks": path_passes + boundary_passes,
            "path_failures": 0,
            "evaluator_ran_count": sum(
                record["evaluator_ran"] is True for record in records
            ),
            "evaluation_mode": "no-model-stub",
            "eval_model_call_attempts": sum(
                record["eval_model_call_attempts"] for record in records
            ),
            "external_model_calls": 0,
            "unique_sandboxes": len(set(sandbox_ids)),
            "all_recorded_sandboxes_unique": len(set(sandbox_ids)) == len(sandbox_ids),
        },
        "records": records,
    }


def _verify(tmp_path: Path, receipt: dict, agent_manifest: dict) -> list[str]:
    no_model = tmp_path / "no-model.json"
    no_model.write_text(json.dumps(receipt))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(agent_manifest))
    return verify_model_coverage(no_model, manifest)


def test_model_coverage_requires_every_boundary_task(tmp_path):
    receipt = _receipt(
        [
            _record("003"),
            _record("035", path_status="MODEL_BOUNDARY_PASS"),
            _record("082"),
        ]
    )

    assert _verify(tmp_path, receipt, _manifest(["035", "082"])) == ["035"]
    with pytest.raises(ValueError, match="035"):
        _verify(tmp_path, receipt, _manifest(["082"]))


def test_model_coverage_requires_path_passes_that_attempted_a_model_call(tmp_path):
    receipt = _receipt([_record("035", call_attempts=1)])

    assert _verify(tmp_path, receipt, _manifest(["035"])) == ["035"]
    with pytest.raises(ValueError, match="035"):
        _verify(tmp_path, receipt, _manifest(["082"]))


def test_agent_manifest_must_be_subset_of_validated_no_model_manifest(tmp_path):
    receipt = _receipt([_record("035", call_attempts=1)])

    with pytest.raises(ValueError, match="082"):
        _verify(tmp_path, receipt, _manifest(["035", "082"]))


def test_model_coverage_rejects_receipt_from_another_template(tmp_path):
    other_template = "template:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    receipt = _receipt(
        [_record("035", path_status="MODEL_BOUNDARY_PASS")],
        manifest=_manifest(["035"], template=other_template),
    )

    with pytest.raises(ValueError, match="template"):
        _verify(tmp_path, receipt, _manifest(["035"]))


def test_model_coverage_requires_nonempty_release_binding(tmp_path):
    receipt = _receipt([_record("003")], manifest=_manifest(["003"], release=""))

    with pytest.raises(ValueError, match="release"):
        _verify(tmp_path, receipt, _manifest(["003"], release=""))


@pytest.mark.parametrize("external_model_calls", [False, 0.0])
def test_model_coverage_requires_integer_zero_call_attestation(
    tmp_path, external_model_calls
):
    receipt = _receipt([_record("003")])
    receipt["summary"]["external_model_calls"] = external_model_calls

    with pytest.raises(ValueError, match="external model calls"):
        _verify(tmp_path, receipt, _manifest(["003"]))


def test_model_coverage_rejects_incomplete_receipt(tmp_path):
    receipt = _receipt([_record("003")], manifest=_manifest(["003", "035"]))

    with pytest.raises(ValueError, match="complete task set"):
        _verify(tmp_path, receipt, _manifest(["003", "035"]))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda receipt: receipt["records"][0].update(path_status="PATH_FAIL"),
            "status",
        ),
        (
            lambda receipt: receipt["records"][0].update(evaluator_ran=False),
            "evaluator",
        ),
        (
            lambda receipt: receipt["records"][0].update(external_model_calls=1),
            "external model calls",
        ),
    ],
)
def test_model_coverage_rejects_nonpassing_task_record(tmp_path, mutation, message):
    receipt = _receipt([_record("003")])
    mutation(receipt)

    with pytest.raises(ValueError, match=message):
        _verify(tmp_path, receipt, _manifest(["003"]))


def test_model_coverage_rejects_duplicate_sandbox_ids(tmp_path):
    receipt = _receipt(
        [_record("003", sandbox_id="same"), _record("035", sandbox_id="same")]
    )

    with pytest.raises(ValueError, match="sandbox"):
        _verify(tmp_path, receipt, _manifest(["003", "035"]))


def test_model_coverage_rejects_summary_that_does_not_reconcile(tmp_path):
    receipt = _receipt([_record("003")])
    receipt["summary"]["validated_tasks"] = 0

    with pytest.raises(ValueError, match="summary"):
        _verify(tmp_path, receipt, _manifest(["003"]))


def test_model_coverage_rejects_wrong_summary_value_type(tmp_path):
    receipt = _receipt([_record("003")])
    receipt["summary"]["all_recorded_sandboxes_unique"] = 1

    with pytest.raises(ValueError, match="summary"):
        _verify(tmp_path, receipt, _manifest(["003"]))


def test_model_coverage_rejects_embedded_manifest_binding_mismatch(tmp_path):
    receipt = _receipt([_record("003")])
    receipt["manifest"] = copy.deepcopy(receipt["manifest"])
    receipt["manifest"]["osworld_commit"] = "b" * 40

    with pytest.raises(ValueError, match="osworld_commit"):
        _verify(tmp_path, receipt, _manifest(["003"]))
