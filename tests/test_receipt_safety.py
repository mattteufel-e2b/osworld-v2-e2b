from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import receipt_safety  # noqa: E402
from receipt_safety import (  # noqa: E402
    classify_failure,
    public_error,
    public_transport,
)


def test_public_error_never_serializes_exception_text():
    secret = "glpat-super-secret"
    payload = public_error(RuntimeError(f"request failed with token {secret}"))

    assert payload == {"error_type": "RuntimeError"}
    assert secret not in json.dumps(payload)


def test_public_transport_strips_credentials_query_and_fragment():
    secret = "api-key-secret"

    value = public_transport(
        f"https://user:{secret}@api.example.test:8443/v1/chat?key={secret}#debug"
    )

    assert value == "https://api.example.test:8443/v1/chat"
    assert secret not in value


def test_atomic_write_json_is_private_and_replaces(tmp_path):
    target = tmp_path / "receipt.json"
    target.write_text("old")
    receipt_safety.atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".receipt.json.*"))  # no staged leftovers


def test_bare_python3_receipt_scripts_run_on_310():
    # These are invoked with system python3 from the coordinator
    # (runner/run_agent_parallel.sh, around its `export ATTEMPT_SUFFIX` retry
    # loop); `from datetime import UTC` needs 3.11+.
    runner = Path(__file__).resolve().parents[1] / "runner"
    for name in ("aggregate_agent.py", "receipt_safety.py"):
        assert "from datetime import UTC" not in (runner / name).read_text(), name


def test_agent_runner_receipt_write_is_atomic():
    source = (
        Path(__file__).resolve().parents[1] / "runner" / "agent_runner.py"
    ).read_text()
    assert "atomic_write_json" in source
    assert "args.output.write_text" not in source


def test_atomic_write_text_is_private_and_replaces(tmp_path):
    target = tmp_path / "token"
    target.write_text("old")
    target.chmod(0o644)
    receipt_safety.atomic_write_text(target, "new\n")
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".token.*"))


def test_base_receipt_is_the_single_source_for_both_receipt_writers(monkeypatch):
    monkeypatch.setenv("OSWORLD_RUN_NONCE", "nonce-1")
    monkeypatch.setenv("GUEST_TEMPLATE", "osworld-v2-gnome:build")
    monkeypatch.setenv("OSWORLD_CAMPAIGN_ID", "campaign")
    monkeypatch.setenv("MODEL_BASE_URL", "https://u:p@model.test/v1?k=s")
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_NAME", "judge")
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_BASE_URL", "https://judge.test/v1?k=s")
    monkeypatch.setenv("M3_THINKING_BUDGET", "2048")
    monkeypatch.setenv("M3_MAX_LLM_RETRIES", "2")
    monkeypatch.delenv("OSWORLD_USER_SIM_MODEL", raising=False)

    receipt = receipt_safety.base_receipt(
        task_id="001",
        domain="release",
        agent_kind="m3",
        model="provider/model",
        max_steps=500,
    )

    assert receipt["run_nonce"] == "nonce-1"
    assert receipt["model_transport"] == "https://model.test/v1"
    assert receipt["eval_model_transport"] == "https://judge.test/v1"
    assert receipt["thinking_budget"] == 2048
    assert receipt["m3_max_llm_retries"] == 2
    assert receipt["user_sim_model"] is None
    assert receipt["recording_enabled"] is False  # ENABLE_RECORDING unset
    assert "k=s" not in json.dumps(receipt)
    assert (
        receipt_safety.base_receipt(
            task_id="001",
            domain="release",
            agent_kind="prompt",
            model="m",
            max_steps=1,
        )["m3_max_llm_retries"]
        is None
    )

    runner = Path(__file__).resolve().parents[1] / "runner"
    for name in ("agent_runner.py",):
        source = (runner / name).read_text()
        assert "base_receipt(" in source, name
        assert '"eval_model_transport": public_transport' not in source, name


def test_base_receipt_records_screen_recording_opt_in(monkeypatch):
    monkeypatch.setenv("OSWORLD_RUN_NONCE", "nonce-1")
    monkeypatch.setenv("ENABLE_RECORDING", "1")
    receipt = receipt_safety.base_receipt(
        task_id="001",
        domain="release",
        agent_kind="m3",
        model="m",
        max_steps=1,
    )
    assert receipt["recording_enabled"] is True


def test_base_receipt_prefers_the_explicit_recording_flag_over_the_environment(
    monkeypatch,
):
    monkeypatch.setenv("OSWORLD_RUN_NONCE", "nonce-1")
    monkeypatch.setenv("ENABLE_RECORDING", "1")
    receipt = receipt_safety.base_receipt(
        task_id="001",
        domain="release",
        agent_kind="m3",
        model="m",
        max_steps=1,
        recording_enabled=False,
    )
    assert receipt["recording_enabled"] is False


class _Timeout(BaseException):
    pass


def test_scored_attempt_is_never_reclassified_as_transport(tmp_path):
    (tmp_path / "result.txt").write_text("0.0")
    cause, transport_ok, evaluator_ran = classify_failure(
        tmp_path, RuntimeError("Connection reset by peer while logging completion")
    )
    assert (cause, evaluator_ran) == ("evaluator-or-agent", True)


def test_timeout_type_wins_over_everything(tmp_path):
    (tmp_path / "result.txt").write_text("0.0")
    cause, _, evaluator_ran = classify_failure(
        tmp_path, _Timeout("deadline"), timeout_types=(_Timeout,)
    )
    assert (cause, evaluator_ran) == ("task-timeout", True)


@pytest.mark.parametrize(
    "message, cause",
    [
        ("HTTPConnectionPool: Max retries exceeded", "transport"),
        ("503 Service Unavailable from ingress", "transport"),
        ("BrowserType.connect_over_cdp: Unexpected status 500", "chrome-cdp"),
        ("EnvironmentSetupError: compose failed", "environment-setup"),
    ],
)
def test_unscored_failures_classify_by_message(tmp_path, message, cause):
    assert classify_failure(tmp_path, RuntimeError(message))[0] == cause


def test_unscored_failure_with_frames_is_evaluator_or_agent(tmp_path):
    (tmp_path / "step_1.png").write_bytes(b"x")
    cause, transport_ok, _ = classify_failure(tmp_path, RuntimeError("boom"))
    assert (cause, transport_ok) == ("evaluator-or-agent", True)


def test_unscored_failure_without_frames_is_reset_or_observation(tmp_path):
    assert classify_failure(tmp_path, RuntimeError("boom"))[0] == "reset-or-observation"
