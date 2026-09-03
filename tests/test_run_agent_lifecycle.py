from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_GUEST = "osworld-v2-gnome:11111111-2222-3333-4444-555555555555"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _runner_env(tmp_path: Path, *, task_timeout: int) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "python3",
        """#!/bin/sh
case "$1" in
  */preflight.py) exit 0 ;;
  *) exec "$REAL_PYTHON" "$@" ;;
esac
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
case "$*" in
  *health*) exit 0 ;;
  *) exit 0 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/bin/sh
case "$*" in
  *e2b_relay.py*)
    echo $$ > "$RELAY_PID_FILE"
    ps -o pgid= -p $$ | tr -d ' ' > "$RELAY_GROUP_FILE"
    trap '' INT TERM
    (trap '' INT TERM; while :; do :; done) &
    echo $! > "$RELAY_CHILD_PID_FILE"
    wait
    ;;
  *)
    echo $$ > "$AGENT_PID_FILE"
    ps -o pgid= -p $$ | tr -d ' ' > "$AGENT_GROUP_FILE"
    trap '' INT TERM
    (trap '' INT TERM; while :; do :; done) &
    echo $! > "$AGENT_CHILD_PID_FILE"
    wait
    ;;
esac
""",
    )

    campaign = "lifecycle-test"
    services = tmp_path / "services"
    services.mkdir()
    runtime = {
        "websites": {
            "sandbox_id": "websites-sandbox",
            "template": IMMUTABLE_GUEST,
            "traffic_token": "websites-token",
            "public_host_suffix": "example.test",
            "sites": {"example": "example.test"},
            "campaign_id": campaign,
        },
        "gitlab": {
            "sandbox_id": "gitlab-sandbox",
            "template": IMMUTABLE_GUEST,
            "traffic_token": "gitlab-token",
            "url": "https://gitlab.example.test",
            "private_token": "private-token",
            "campaign_id": campaign,
        },
    }
    runtime_path = services / ".runtime.json"
    runtime_path.write_text(json.dumps(runtime))
    runtime_path.chmod(0o600)
    token_path = services / ".gitlab-token"
    token_path.write_text("private-token\n")
    token_path.chmod(0o600)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "template": IMMUTABLE_GUEST,
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "001", "domain": "test"}],
            }
        )
    )

    osworld = tmp_path / "OSWorld-V2"
    osworld.mkdir()
    (osworld / "e2b_relay.py").write_text("")
    tasks = tmp_path / "tasks"
    (tasks / "assets").mkdir(parents=True)
    (tasks / "task_001.py").write_text("TASK = {}\n")

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "TASK_ID": "001",
        "DOMAIN": "test",
        "PORT_BASE": "500",
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": campaign,
        "E2B_API_KEY": "dummy",
        "MODEL_API_KEY": "dummy",
        "OSWORLD_ROOT": str(osworld),
        "OSWORLD_TASKS_DIR": str(tasks),
        "OSWORLD_SERVICES_DIR": str(services),
        "AGENT_MANIFEST": str(manifest),
        "RAW_DIR": str(tmp_path / "raw"),
        "OUTPUT": str(tmp_path / "receipt.json"),
        "AGENT_TASK_TIMEOUT_SECONDS": str(task_timeout),
        "PROCESS_TERMINATION_GRACE_SECONDS": "1",
        "RELAY_STOP_REQUEST_TIMEOUT_SECONDS": "1",
    }
    for name in (
        "AGENT_PID_FILE",
        "AGENT_GROUP_FILE",
        "AGENT_CHILD_PID_FILE",
        "RELAY_PID_FILE",
        "RELAY_GROUP_FILE",
        "RELAY_CHILD_PID_FILE",
    ):
        env[name] = str(tmp_path / name.lower())
    return env


def _wait_for_file(path: Path, timeout: float = 10) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _assert_process_gone(pid: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} survived runner cleanup")


def _recorded_pids(env: dict[str, str]) -> list[int]:
    return [
        int(Path(env[name]).read_text().strip())
        for name in (
            "AGENT_PID_FILE",
            "AGENT_CHILD_PID_FILE",
            "RELAY_PID_FILE",
            "RELAY_CHILD_PID_FILE",
        )
    ]


def _recorded_process_groups(env: dict[str, str]) -> list[int]:
    return [
        int(Path(env[name]).read_text().strip())
        for name in ("AGENT_GROUP_FILE", "RELAY_GROUP_FILE")
    ]


def _assert_commands_started_as_process_group_leaders(env: dict[str, str]) -> None:
    assert (
        Path(env["AGENT_PID_FILE"]).read_text().strip()
        == Path(env["AGENT_GROUP_FILE"]).read_text().strip()
    )
    assert (
        Path(env["RELAY_PID_FILE"]).read_text().strip()
        == Path(env["RELAY_GROUP_FILE"]).read_text().strip()
    )


def _assert_process_group_gone(process_group: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process group {process_group} survived runner cleanup")


def _force_cleanup(env: dict[str, str]) -> None:
    for name in ("AGENT_GROUP_FILE", "RELAY_GROUP_FILE"):
        path = Path(env[name])
        if not path.exists():
            continue
        try:
            process_group = int(path.read_text().strip())
            if process_group != os.getpgrp():
                os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    for name in (
        "AGENT_CHILD_PID_FILE",
        "AGENT_PID_FILE",
        "RELAY_CHILD_PID_FILE",
        "RELAY_PID_FILE",
    ):
        path = Path(env[name])
        if not path.exists():
            continue
        try:
            os.kill(int(path.read_text().strip()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_agent_timeout_kills_agent_and_relay_process_trees(tmp_path):
    env = _runner_env(tmp_path, task_timeout=1)
    started = time.monotonic()
    process = subprocess.Popen(
        ["bash", str(ROOT / "runner" / "run_agent.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("runner did not finish bounded timeout cleanup within 15s")

        assert process.returncode == 124, (stdout, stderr)
        assert time.monotonic() - started < 10
        assert "exceeded 1s deadline" in stderr
        _assert_commands_started_as_process_group_leaders(env)
        for pid in _recorded_pids(env):
            _assert_process_gone(pid)
        for process_group in _recorded_process_groups(env):
            _assert_process_group_gone(process_group)
        _assert_process_gone(process.pid)
    finally:
        _force_cleanup(env)
        if process.poll() is None:
            process.kill()
        process.communicate()


@pytest.mark.parametrize(
    ("runner_signal", "expected_exit_code"),
    ((signal.SIGINT, 130), (signal.SIGTERM, 143)),
)
def test_signal_kills_agent_and_relay_process_trees(
    tmp_path, runner_signal: signal.Signals, expected_exit_code: int
):
    env = _runner_env(tmp_path, task_timeout=60)
    process = subprocess.Popen(
        ["bash", str(ROOT / "runner" / "run_agent.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_file(Path(env["AGENT_CHILD_PID_FILE"]))
        process.send_signal(runner_signal)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        _force_cleanup(env)
        if process.poll() is None:
            process.kill()
        process.communicate()

    assert process.returncode == expected_exit_code, (stdout, stderr)
    _assert_commands_started_as_process_group_leaders(env)
    for pid in _recorded_pids(env):
        _assert_process_gone(pid)
    for process_group in _recorded_process_groups(env):
        _assert_process_group_gone(process_group)
    _assert_process_gone(process.pid)


def test_readme_bounds_canary_but_restores_sample_retry_policy():
    readme = (ROOT / "README.md").read_text()

    canary_retry = "M3_MAX_LLM_RETRIES=0"
    canary_timeout = "AGENT_TASK_TIMEOUT_SECONDS=900"
    sample_retry = "export M3_MAX_LLM_RETRIES=2"
    sample_campaign = 'export OSWORLD_CAMPAIGN_ID="osworld-v2-sample24-'

    assert readme.index(canary_retry) < readme.index(canary_timeout)
    assert readme.index(canary_timeout) < readme.index(sample_retry)
    assert readme.index(sample_retry) < readme.index(sample_campaign)
