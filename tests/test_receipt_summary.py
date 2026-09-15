from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maintainer"))
from receipt_summary import summarize  # noqa: E402


def test_no_model_receipt_yields_no_model_kind_with_string_task_statuses():
    receipt = {
        "template": "osworld-v2-gnome:1111",
        "summary": {
            "evaluation_mode": "no-model-stub",
            "path_passes": 1,
            "path_failures": 0,
        },
        "records": [{"id": "001", "path_status": "PATH_PASS"}],
    }

    summary = summarize(receipt, "no-model.json")

    assert summary["kind"] == "no-model-ladder-summary"
    assert summary["evaluation_mode"] == "no-model-stub"
    assert summary["task_statuses"] == {"001": "PATH_PASS"}
    assert isinstance(summary["task_statuses"]["001"], str)


def test_agent_receipt_yields_agent_kind_with_model_usage_and_per_task_dicts():
    receipt = {
        "template": "osworld-v2-gnome:1111",
        "execution": {
            "retried_task_ids": ["001"],
            "implicit_retries": True,
        },
        "summary": {
            "attested_records": 1,
            "scored_tasks": 1,
            "mean_score": 1.0,
            "binary_accuracy": 1.0,
            "model_usage": {"agent": {"calls": 3, "input_tokens": 10}},
        },
        "records": [
            {
                "id": "001",
                "path_status": "OK",
                "score": 1.0,
                "steps_taken": 4,
                "wall_clock_s": 12.3,
            },
            {
                "id": "002",
                "path_status": "FAIL",
                "score": 0.0,
                "error_cause": "agent-timeout",
                "steps_taken": 500,
            },
        ],
    }

    summary = summarize(receipt, "agent.json")

    assert summary["kind"] == "agent-run-summary"
    assert summary["attested_records"] == 1
    assert summary["scored_tasks"] == 1
    assert summary["mean_score"] == 1.0
    assert summary["binary_accuracy"] == 1.0
    assert summary["retried_task_ids"] == ["001"]
    assert summary["implicit_retries"] is True
    assert summary["model_usage"] == {"agent": {"calls": 3, "input_tokens": 10}}
    assert summary["task_statuses"]["001"] == {
        "path_status": "OK",
        "score": 1.0,
        "steps_taken": 4,
    }
    assert summary["task_statuses"]["002"] == {
        "path_status": "FAIL",
        "score": 0.0,
        "error_cause": "agent-timeout",
        "steps_taken": 500,
    }
    assert "wall_clock_s" not in summary["task_statuses"]["001"]


def test_no_model_records_with_a_null_score_keep_the_status_string_shape():
    receipt = {
        "summary": {"evaluation_mode": "no-model-stub", "tasks": 1},
        "records": [
            {
                "id": "001",
                "path_status": "PATH_PASS",
                "score": None,
                "evaluation_mode": "no-model-stub",
            }
        ],
    }
    summary = summarize(receipt, "ladder.json")
    assert summary["kind"] == "no-model-ladder-summary"
    assert summary["task_statuses"] == {"001": "PATH_PASS"}


def test_serial_harness_receipt_without_summary_evaluation_mode_is_still_a_ladder():
    receipt = {
        "summary": {"tasks": 1, "path_passes": 1},
        "records": [
            {
                "id": "001",
                "path_status": "PATH_PASS",
                "score": 0.0,
                "evaluation_mode": "no-model-stub",
            }
        ],
    }
    summary = summarize(receipt, "validate-final.json")
    assert summary["kind"] == "no-model-ladder-summary"
    assert summary["task_statuses"] == {"001": "PATH_PASS"}


def test_agent_records_keep_the_dict_shape():
    receipt = {
        "summary": {"scored_tasks": 1},
        "records": [
            {
                "id": "003",
                "path_status": "OK",
                "score": 0.6,
                "steps_taken": 27,
                "agent_kind": "m3",
            }
        ],
    }
    summary = summarize(receipt, "agent.json")
    assert summary["kind"] == "agent-run-summary"
    assert summary["task_statuses"]["003"] == {
        "path_status": "OK",
        "score": 0.6,
        "steps_taken": 27,
    }
