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
    summary = summarize(receipt, "stamped-records-no-summary-mode.json")
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


def test_browser_probe_record_keeps_acceptance_and_reduces_origin_lists():
    record = {
        "schema_version": 1,
        "build_id": "00124a57",
        "passed": True,
        "acceptance": {"teamchat.secure": True, "all_origins_probed": True},
        "bridge": {"sandbox_id": "sbx"},
        "cdp": {
            "browser_version": {"Browser": "Chrome/153"},
            "enable_errors": {"Log": None},
            "event_count": 223,
        },
        "origins": [
            {
                "label": "teamchat",
                "requested_url": "https://teamchat.127.0.0.1.nip.io",
                "raw_probe": "{...}",
                "values": {"secure": True, "clipboard": "object"},
                "security": {"source": "event", "value": "secure", "events": [1, 2]},
                "console_entries": [{"text": "x"}],
                "blocked_urls": [],
                "network": {
                    "total_requests": 2,
                    "schemes": {"https": 2},
                    "requests": [{"url": "a"}, {"url": "b"}],
                    "insecure_requests": [],
                    "insecure_urls": [],
                    "mixed_content_blocked": [],
                    "failures": [],
                    "requests_truncated": False,
                },
            }
        ],
    }

    summary = summarize(record, "browser-probe-00124a57.json")

    assert summary["kind"] == "browser-probe-summary"
    assert summary["acceptance"] == record["acceptance"]
    assert summary["passed"] is True
    assert "bridge" not in summary
    assert summary["cdp"] == {
        "browser": "Chrome/153",
        "enable_errors": {"Log": None},
        "event_count": 223,
    }
    origin = summary["origins"][0]
    assert "raw_probe" not in origin
    assert origin["values"] == {"secure": True, "clipboard": "object"}
    assert origin["security"] == {"source": "event", "value": "secure"}
    assert origin["console_entries_count"] == 1
    assert origin["blocked_urls"] == []
    assert origin["network"] == {
        "total_requests": 2,
        "schemes": {"https": 2},
        "requests_truncated": False,
        "insecure_urls": [],
        "requests_count": 2,
        "failures_count": 0,
        "insecure_requests_count": 0,
        "mixed_content_blocked_count": 0,
    }
