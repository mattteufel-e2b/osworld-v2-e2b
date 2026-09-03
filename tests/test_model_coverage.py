from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from model_coverage import verify_model_coverage  # noqa: E402


def test_model_coverage_requires_every_boundary_task(tmp_path):
    no_model = tmp_path / "no-model.json"
    no_model.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "summary": {"external_model_calls": 0},
                "records": [
                    {"id": "003", "path_status": "PATH_PASS"},
                    {"id": "035", "path_status": "MODEL_BOUNDARY_PASS"},
                ],
            }
        )
    )
    complete = tmp_path / "complete.json"
    complete.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "035"}, {"id": "082"}],
            }
        )
    )
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "082"}],
            }
        )
    )

    assert verify_model_coverage(no_model, complete) == ["035"]
    try:
        verify_model_coverage(no_model, incomplete)
    except ValueError as error:
        assert "035" in str(error)
    else:
        raise AssertionError("missing model-boundary task was accepted")


def test_model_coverage_rejects_receipt_from_another_template(tmp_path):
    no_model = tmp_path / "no-model.json"
    no_model.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "osworld_commit": "a" * 40,
                "summary": {"external_model_calls": 0},
                "records": [{"id": "035", "path_status": "MODEL_BOUNDARY_PASS"}],
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "035"}],
            }
        )
    )

    try:
        verify_model_coverage(no_model, manifest)
    except ValueError as error:
        assert "template" in str(error)
    else:
        raise AssertionError("no-model receipt from another template was accepted")


def test_model_coverage_requires_nonempty_release_binding(tmp_path):
    no_model = tmp_path / "no-model.json"
    no_model.write_text(
        json.dumps(
            {
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "summary": {"external_model_calls": 0},
                "records": [],
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "tasks": [],
            }
        )
    )

    with pytest.raises(ValueError, match="release"):
        verify_model_coverage(no_model, manifest)


@pytest.mark.parametrize("external_model_calls", [False, 0.0])
def test_model_coverage_requires_integer_zero_call_attestation(
    tmp_path, external_model_calls
):
    no_model = tmp_path / "no-model.json"
    no_model.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "summary": {"external_model_calls": external_model_calls},
                "records": [],
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "release": "release-1",
                "template": "template:11111111-2222-3333-4444-555555555555",
                "osworld_commit": "a" * 40,
                "tasks": [],
            }
        )
    )

    with pytest.raises(ValueError, match="external model calls"):
        verify_model_coverage(no_model, manifest)
