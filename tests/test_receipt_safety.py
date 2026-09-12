from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import receipt_safety  # noqa: E402
from receipt_safety import public_error, public_transport  # noqa: E402


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
    # These are invoked with system python3 (run_agent.sh:265,
    # run_agent_parallel.sh:328); `from datetime import UTC` needs 3.11+.
    runner = Path(__file__).resolve().parents[1] / "runner"
    for name in ("write_timeout_receipt.py", "aggregate_agent.py", "receipt_safety.py"):
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
        port_base=500,
    )

    assert receipt["run_nonce"] == "nonce-1"
    assert receipt["control_port"] == 15499
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
            port_base=0,
        )["m3_max_llm_retries"]
        is None
    )

    runner = Path(__file__).resolve().parents[1] / "runner"
    for name in ("agent_runner.py", "write_timeout_receipt.py"):
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
        port_base=0,
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
        port_base=0,
        recording_enabled=False,
    )
    assert receipt["recording_enabled"] is False
