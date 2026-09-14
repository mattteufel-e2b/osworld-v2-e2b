from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import aggregate_agent  # noqa: E402
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
                "user_sim_call_attempts": 0,
                "user_sim_successes": 0,
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
        no_model_coverage_enforced=False,
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


@pytest.mark.parametrize(
    "attempts,successes,required", [(1, 0, set()), (2, 1, {"001"})]
)
def test_failed_judge_invalidates_score_even_after_another_success(
    tmp_path, attempts, successes, required
):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record.update(eval_model_call_attempts=attempts, eval_model_successes=successes)
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
        required_eval_model_ids=required,
        no_model_coverage_enforced=bool(required),
    )
    assert not ok
    assert "eval-model-failure" in run["summary"]["invalid_task_ids"]["001"]


def test_invalid_receipts_are_excluded_from_score_summary(tmp_path):
    manifest, workers = _inputs(tmp_path)
    record = json.loads((workers / "task_001.json").read_text())
    record.update(eval_model_call_attempts=1, eval_model_successes=0)
    (workers / "task_001.json").write_text(json.dumps(record))
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
        no_model_coverage_enforced=False,
    )
    assert not ok
    assert run["summary"]["scored_tasks"] == 0
    assert run["summary"]["mean_score"] is None


def test_failed_simulator_invalidates_score_even_after_another_success(tmp_path):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record.update(user_sim_call_attempts=2, user_sim_successes=1)
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
        no_model_coverage_enforced=False,
    )

    assert not ok
    assert run["summary"]["invalid_task_ids"] == {"001": ["user-sim-failure"]}


def test_unset_model_configs_are_normalized_to_null(tmp_path):
    manifest, workers = _inputs(tmp_path)
    receipt = workers / "task_001.json"
    record = json.loads(receipt.read_text())
    record.update(
        eval_model=None,
        eval_provider=None,
        eval_model_transport=None,
        user_sim_model=None,
        user_sim_provider=None,
        user_sim_transport=None,
    )
    receipt.write_text(json.dumps(record))

    run, ok = aggregate(
        manifest,
        workers,
        model="model",
        agent_kind="m3",
        model_transport="https://example.test/v1",
        eval_model="",
        eval_provider="",
        eval_transport="",
        user_sim_model="",
        user_sim_provider="",
        user_sim_transport="",
        max_steps=500,
        concurrency=1,
        thinking_mode=None,
        thinking_budget=2048,
        m3_max_llm_retries=2,
        task_082_concurrent=True,
        run_nonce="run-nonce-1",
        campaign_id="campaign-1",
        required_eval_model_ids=set(),
        no_model_coverage_enforced=False,
    )

    assert ok
    assert run["evaluator"]["provider"] is None
    assert run["evaluator"]["model"] is None
    assert run["user_simulator"]["provider"] is None
    assert run["user_simulator"]["model"] is None


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
        no_model_coverage_enforced=False,
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
        ("user_sim_call_attempts", 1),
        ("user_sim_successes", True),
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
        no_model_coverage_enforced=False,
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
        no_model_coverage_enforced=False,
    )

    assert not ok
    assert run["summary"]["invalid_task_ids"] == {
        "001": ["eval-model-failure", "eval-model-success"]
    }


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
        no_model_coverage_enforced=False,
    )

    assert ok
    assert run["run_id"] == "run-nonce-1"


def test_execution_block_reports_actual_retries(tmp_path):
    manifest, workers = _inputs(tmp_path)
    (workers / "task_001_before_retry_1.json").write_text("{}")

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
        no_model_coverage_enforced=True,
    )

    assert run["execution"]["retried_task_ids"] == ["001"]
    assert run["execution"]["implicit_retries"] is True
    assert run["execution"]["no_model_coverage_enforced"] is True


def test_execution_block_without_retries(tmp_path):
    manifest, workers = _inputs(tmp_path)

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
        no_model_coverage_enforced=False,
    )

    assert run["execution"]["retried_task_ids"] == []
    assert run["execution"]["implicit_retries"] is False
    assert run["execution"]["no_model_coverage_enforced"] is False


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
        no_model_coverage_enforced=False,
    )

    assert ok


def test_main_wiring_passes_no_model_coverage_enforced_from_receipt_flag():
    """main() must derive no_model_coverage_enforced from whether
    --no-model-receipt was supplied, not from a separate flag (no new CLI
    flag was introduced for this)."""
    source = Path(aggregate_agent.__file__).read_text()
    assert "no_model_coverage_enforced=args.no_model_receipt is not None" in source


def test_main_writes_campaign_receipt_atomically_with_private_perms(
    tmp_path, monkeypatch
):
    manifest, workers = _inputs(tmp_path)
    output = tmp_path / "campaign-receipt.json"
    argv = [
        "aggregate_agent.py",
        "--manifest",
        str(manifest),
        "--worker-dir",
        str(workers),
        "--output",
        str(output),
        "--model",
        "model",
        "--agent-kind",
        "m3",
        "--model-transport",
        "https://example.test/v1",
        "--eval-model",
        "judge",
        "--eval-provider",
        "openai_compatible",
        "--eval-transport",
        "https://example.test/v1",
        "--user-sim-model",
        "simulator",
        "--user-sim-provider",
        "openai_compatible",
        "--user-sim-transport",
        "https://example.test/v1",
        "--max-steps",
        "500",
        "--concurrency",
        "1",
        "--thinking-budget",
        "2048",
        "--m3-max-llm-retries",
        "2",
        "--task-082-concurrent",
        "--run-nonce",
        "run-nonce-1",
        "--campaign-id",
        "campaign-1",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    aggregate_agent.main()

    # 0600 perms only happen via atomic_write_json's staged-file chmod + replace;
    # this proves main() no longer writes the campaign receipt with a plain
    # write_text (which would inherit the process umask instead).
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    receipt = json.loads(output.read_text())
    assert receipt["run_id"] == "run-nonce-1"
    # No --no-model-receipt was passed on argv, so main() must record the
    # coverage gate as unenforced rather than silently defaulting to True.
    assert receipt["execution"]["no_model_coverage_enforced"] is False
