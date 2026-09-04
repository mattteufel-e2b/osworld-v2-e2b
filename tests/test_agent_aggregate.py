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
                "run_nonce": "run-nonce-1",
                "campaign_id": "campaign-1",
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
                "eval_provider": "openai_compatible",
                "model_transport": "https://example.test/v1",
                "eval_model_transport": "https://example.test/v1",
                "user_sim_model": "simulator",
                "user_sim_provider": "openai_compatible",
                "user_sim_transport": "https://example.test/v1",
                "max_steps": 500,
                "thinking_mode": None,
                "thinking_budget": 2048,
                "m3_max_llm_retries": 2,
                "eval_model_call_attempts": 1,
                "eval_model_successes": 1,
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
        eval_provider="openai_compatible",
        eval_transport="https://example.test/v1?token=secret",
        user_sim_model="simulator",
        user_sim_provider="openai_compatible",
        user_sim_transport="https://example.test/v1?token=secret",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        m3_max_llm_retries=2,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids={"001"},
    )

    assert ok
    assert run["summary"]["attested_records"] == 1
    assert run["model_transport"] == "https://example.test/v1"
    assert run["evaluator"]["provider"] == "openai_compatible"
    assert run["user_simulator"] == {
        "provider": "openai_compatible",
        "model": "simulator",
        "transport": "https://example.test/v1",
    }
    assert run["reasoning"]["max_llm_retries"] == 2


def test_agent_aggregate_counts_only_present_valid_records_as_attested(tmp_path):
    """Missing receipts must not drive the attested count below zero."""
    manifest, workers = _inputs(tmp_path)
    manifest_payload = json.loads(manifest.read_text())
    manifest_payload["tasks"].extend(
        {"id": task_id, "domain": "release"} for task_id in ("002", "003", "004")
    )
    manifest.write_text(json.dumps(manifest_payload))

    run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="m3",
        model_transport="https://example.test/v1",
        eval_model="judge",
        eval_provider="openai_compatible",
        eval_transport="https://example.test/v1",
        user_sim_model="simulator",
        user_sim_provider="openai_compatible",
        user_sim_transport="https://example.test/v1",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        m3_max_llm_retries=2,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids={"001"},
    )

    assert not ok
    assert run["summary"]["tasks"] == 1
    assert run["summary"]["expected_tasks"] == 4
    assert run["summary"]["attested_records"] == 1
    assert run["summary"]["invalid_task_ids"] == {
        "002": ["missing-or-invalid-receipt"],
        "003": ["missing-or-invalid-receipt"],
        "004": ["missing-or-invalid-receipt"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("template", "wrong"),
        ("score", float("nan")),
        ("steps_taken", 0),
        ("run_nonce", "stale-run"),
        ("campaign_id", "stale-campaign"),
        ("max_steps", 75),
        ("thinking_mode", "adaptive"),
        ("thinking_budget", 1024),
        ("m3_max_llm_retries", 0),
        ("eval_provider", "wrong-provider"),
        ("user_sim_model", "wrong-simulator"),
        ("user_sim_provider", "wrong-provider"),
        ("user_sim_transport", "https://wrong.example/v1"),
        ("eval_model_call_attempts", "1"),
        ("eval_model_successes", -1),
    ],
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
        eval_provider="openai_compatible",
        eval_transport="https://example.test/v1",
        user_sim_model="simulator",
        user_sim_provider="openai_compatible",
        user_sim_transport="https://example.test/v1",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        m3_max_llm_retries=2,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids={"001"},
    )

    assert not ok
    assert run["summary"]["attested_records"] == 0


def test_agent_aggregate_requires_successful_evaluator_call_for_boundary_task(tmp_path):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record["eval_model_successes"] = 0
    receipt.write_text(json.dumps(record))

    run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="m3",
        model_transport="https://example.test/v1",
        eval_model="judge",
        eval_provider="openai_compatible",
        eval_transport="https://example.test/v1",
        user_sim_model="simulator",
        user_sim_provider="openai_compatible",
        user_sim_transport="https://example.test/v1",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        m3_max_llm_retries=2,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids={"001"},
    )

    assert not ok
    assert run["summary"]["invalid_task_ids"] == {"001": ["eval-model-success"]}


def test_agent_aggregate_allows_zero_evaluator_calls_for_non_boundary_task(tmp_path):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record["eval_model_call_attempts"] = 0
    record["eval_model_successes"] = 0
    receipt.write_text(json.dumps(record))

    run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="m3",
        model_transport="https://example.test/v1",
        eval_model="judge",
        eval_provider="openai_compatible",
        eval_transport="https://example.test/v1",
        user_sim_model="simulator",
        user_sim_provider="openai_compatible",
        user_sim_transport="https://example.test/v1",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        m3_max_llm_retries=2,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids=set(),
    )

    assert ok
    assert run["run_id"] == "run-nonce-1"


def test_agent_aggregate_accepts_null_m3_retry_policy_for_prompt_agent(tmp_path):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record["agent_kind"] = "prompt"
    record["thinking_budget"] = None
    record["m3_max_llm_retries"] = None
    receipt.write_text(json.dumps(record))

    _run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="prompt",
        model_transport="https://example.test/v1",
        eval_model="judge",
        eval_provider="openai_compatible",
        eval_transport="https://example.test/v1",
        user_sim_model="simulator",
        user_sim_provider="openai_compatible",
        user_sim_transport="https://example.test/v1",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=None,
        m3_max_llm_retries=None,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids={"001"},
    )

    assert ok
