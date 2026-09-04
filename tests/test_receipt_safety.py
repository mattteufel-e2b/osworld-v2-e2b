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
    assert not list(tmp_path.glob(".receipt.json.*"))   # no staged leftovers


def test_bare_python3_receipt_scripts_run_on_310():
    # These are invoked with system python3 (run_agent.sh:265,
    # run_agent_parallel.sh:328); `from datetime import UTC` needs 3.11+.
    runner = Path(__file__).resolve().parents[1] / "runner"
    for name in ("write_timeout_receipt.py", "aggregate_agent.py", "receipt_safety.py"):
        assert "from datetime import UTC" not in (runner / name).read_text(), name


def test_agent_runner_receipt_write_is_atomic():
    source = (Path(__file__).resolve().parents[1] / "runner" / "agent_runner.py").read_text()
    assert "atomic_write_json" in source
    assert "args.output.write_text" not in source
