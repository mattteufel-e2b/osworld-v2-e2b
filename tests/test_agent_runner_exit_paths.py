"""agent_runner.py must publish a receipt on every exit path.

Runs the real runner with a fake checkout on sys.path: DesktopEnv and
lib_run_single are stubs, so no network or sandbox is involved.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fake_checkout(tmp_path: Path, run_single_body: str) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "desktop_env").mkdir(parents=True)
    (checkout / "desktop_env" / "__init__.py").write_text("")
    (checkout / "desktop_env" / "desktop_env.py").write_text(
        textwrap.dedent(
            """
            class _Bridge:
                def state(self):
                    return {"sandbox_id": "sbx-fake", "generation": 1, "restricted_ingress": True}
            class _Provider:
                bridge = _Bridge()
            class DesktopEnv:
                def __init__(self, **kwargs):
                    self.provider = _Provider()
                def close(self):
                    pass
            """
        )
    )
    # EvaluatorModelCallTracker.install() imports these three upstream modules.
    (checkout / "desktop_env" / "evaluators").mkdir()
    (checkout / "desktop_env" / "evaluators" / "__init__.py").write_text("")
    (checkout / "desktop_env" / "evaluators" / "model_client.py").write_text(
        "def generate_text(*a, **k):\n    return 'x'\n"
        "def generate_chat(*a, **k):\n    return 'x'\n"
    )
    (checkout / "desktop_env" / "evaluators" / "metrics").mkdir()
    (checkout / "desktop_env" / "evaluators" / "metrics" / "__init__.py").write_text("")
    (checkout / "desktop_env" / "evaluators" / "metrics" / "llm_metrics.py").write_text(
        "def generate_text(*a, **k):\n    return 'x'\n"
    )
    (checkout / "desktop_env" / "user_simulator.py").write_text(
        "class LLMUserSimulator:\n    def respond(self, text):\n        return text\n"
    )
    (checkout / "lib_run_single.py").write_text(
        "def run_single_example(agent, env, example, max_steps, instruction, args, result_dir, scores):\n"
        + textwrap.indent(textwrap.dedent(run_single_body), "    ")
    )
    (checkout / "task_loader.py").write_text(
        "def load_task_from_file(path):\n    return {'instruction': 'do the thing'}\n"
    )
    for name in ("lazy_import.py", "receipt_safety.py", "evaluator_model_calls.py"):
        (checkout / name).write_text((ROOT / "runner" / name).read_text())
    (checkout / "agents.py").write_text(
        "AGENT_KINDS = {'prompt': None}\n"
        "def agent_settings(kind, **kw):\n    return {'kind': kind}\n"
        "def build_agent(kind, **kw):\n    return object()\n"
    )
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "task_001.py").write_text("TASK = {}\n")
    return checkout


def _run(
    tmp_path: Path, checkout: Path, extra: list[str]
) -> tuple[subprocess.Popen, Path]:
    receipt = tmp_path / "receipt.json"
    cmd = [
        sys.executable,
        str(ROOT / "runner" / "agent_runner.py"),
        "--task-id",
        "001",
        "--domain",
        "test",
        "--tasks-dir",
        str(tmp_path / "tasks"),
        "--result-dir",
        str(tmp_path / "raw"),
        "--output",
        str(receipt),
        "--agent-kind",
        "prompt",
        "--model",
        "m",
        "--max-steps",
        "3",
        *extra,
    ]
    env = {**os.environ, "OSWORLD_RUN_NONCE": "nonce", "PYTHONPATH": str(checkout)}
    process = subprocess.Popen(
        cmd,
        cwd=checkout,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process, receipt


def _wait_for_exit(process: subprocess.Popen) -> None:
    """Never leave a runner behind when a hang or an assertion ends the test."""
    try:
        process.wait(timeout=20)
    finally:
        if process.returncode is None:
            process.kill()
            process.wait(timeout=5)


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def test_deadline_writes_a_task_timeout_receipt_and_exits_124(tmp_path):
    checkout = _fake_checkout(tmp_path, "import time\ntime.sleep(30)")
    process, receipt = _run(tmp_path, checkout, ["--deadline-seconds", "1"])
    _wait_for_exit(process)
    assert process.returncode == 124, process.communicate()
    data = json.loads(receipt.read_text())
    assert data["path_status"] == "ERROR"
    assert data["error_cause"] == "task-timeout"
    assert data["error_type"] == "AgentTaskTimeout"
    assert data["sandbox_id"] == "sbx-fake"
    assert "port_base" not in data and "control_port" not in data


def test_sigterm_writes_an_interrupted_receipt_and_exits_143(tmp_path):
    checkout = _fake_checkout(
        tmp_path,
        "import time\nopen(result_dir + '/started', 'w').close()\ntime.sleep(30)",
    )
    process, receipt = _run(tmp_path, checkout, ["--deadline-seconds", "60"])
    _wait_for(
        tmp_path / "raw" / "started"
    )  # handlers are installed before the loop runs
    process.send_signal(signal.SIGTERM)
    _wait_for_exit(process)
    assert process.returncode == 143, process.communicate()
    data = json.loads(receipt.read_text())
    assert data["error_cause"] == "interrupted"


def test_failure_before_env_exists_still_writes_a_receipt(tmp_path):
    checkout = _fake_checkout(tmp_path, "pass")
    (checkout / "desktop_env" / "desktop_env.py").write_text(
        "class DesktopEnv:\n    def __init__(self, **kw):\n        raise OSError('bind failed: address in use')\n"
    )
    process, receipt = _run(tmp_path, checkout, [])
    _wait_for_exit(process)
    assert process.returncode == 0
    data = json.loads(receipt.read_text())
    assert data["path_status"] == "ERROR"
    assert data["sandbox_id"] is None
    assert data["error_type"] == "OSError"


def test_a_hung_task_loader_still_hits_the_deadline(tmp_path):
    # Task loading and agent construction run before DesktopEnv; the deadline
    # must already be armed there, or a hung loader runs forever.
    checkout = _fake_checkout(tmp_path, "pass")
    (checkout / "task_loader.py").write_text(
        "import time\n"
        "def load_task_from_file(path):\n"
        "    time.sleep(30)\n"
        "    return {'instruction': 'do the thing'}\n"
    )
    process, receipt = _run(tmp_path, checkout, ["--deadline-seconds", "1"])
    _wait_for_exit(process)
    assert process.returncode == 124, process.communicate()
    data = json.loads(receipt.read_text())
    assert data["path_status"] == "ERROR"
    assert data["error_cause"] == "task-timeout"


def test_a_failing_task_loader_still_writes_a_receipt(tmp_path):
    checkout = _fake_checkout(tmp_path, "pass")
    (checkout / "task_loader.py").write_text(
        "def load_task_from_file(path):\n    raise RuntimeError('bad task')\n"
    )
    process, receipt = _run(tmp_path, checkout, [])
    _wait_for_exit(process)
    assert process.returncode == 0, process.communicate()
    data = json.loads(receipt.read_text())
    assert data["path_status"] == "ERROR"
    assert data["error_type"] == "RuntimeError"
    assert data["sandbox_id"] is None


def test_success_path_records_bridge_sandbox(tmp_path):
    checkout = _fake_checkout(
        tmp_path, "open(result_dir + '/result.txt', 'w').write('0.5')"
    )
    process, receipt = _run(tmp_path, checkout, [])
    _wait_for_exit(process)
    assert process.returncode == 0, process.communicate()
    data = json.loads(receipt.read_text())
    assert data["path_status"] == "OK"
    assert data["score"] == 0.5
    assert data["sandbox_id"] == "sbx-fake"
    # Token accounting is always present, even with no SDK on the path.
    assert sorted(data["model_usage"]) == ["agent", "judge", "simulator"]
    assert data["model_usage"]["agent"]["calls"] == 0


def test_a_second_sigterm_during_teardown_cannot_cost_the_receipt(tmp_path):
    # env.close() can take minutes (the bridge joins its proxy threads) and the
    # receipt is written after the finally block, so a coordinator escalating
    # with a second SIGTERM must not abort teardown half way.
    checkout = _fake_checkout(
        tmp_path,
        "import time\nopen(result_dir + '/started', 'w').close()\ntime.sleep(30)",
    )
    raw = tmp_path / "raw"
    (checkout / "desktop_env" / "desktop_env.py").write_text(
        textwrap.dedent(
            """
            import time
            class _Bridge:
                def state(self):
                    return {"sandbox_id": "sbx-fake", "generation": 1, "restricted_ingress": True}
            class _Provider:
                bridge = _Bridge()
            class DesktopEnv:
                def __init__(self, **kwargs):
                    self.provider = _Provider()
                def close(self):
                    open(CLOSING_MARKER, "w").close()
                    time.sleep(3)
            """
        ).replace("CLOSING_MARKER", repr(str(raw / "closing")))
    )
    process, receipt = _run(tmp_path, checkout, ["--deadline-seconds", "60"])
    _wait_for(raw / "started")
    process.send_signal(signal.SIGTERM)
    _wait_for(raw / "closing")  # teardown has begun and is deliberately slow
    process.send_signal(signal.SIGTERM)  # the escalation that must be ignored
    _wait_for_exit(process)
    assert process.returncode == 143, process.communicate()
    data = json.loads(receipt.read_text())
    assert data["error_cause"] == "interrupted"
    assert data["sandbox_id"] == "sbx-fake"
