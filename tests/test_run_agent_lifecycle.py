from __future__ import annotations

import json
import os
import re
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
        "OSWORLD_RUN_NONCE": "run-nonce-1",
        "E2B_API_KEY": "e2b-sentinel-never-publish",
        "MODEL_API_KEY": "model-sentinel-never-publish",
        "MODEL_BASE_URL": "https://model-user:model-secret@example.test/v1?token=secret",
        "MODEL": "provider/model",
        "AGENT_KIND": "m3",
        "M3_THINKING_BUDGET": "2048",
        "M3_MAX_LLM_RETRIES": "2",
        "EVAL_MODEL_BASE_URL": "https://judge-user:judge-secret@judge.test/v1?key=secret",
        "EVAL_MODEL": "judge-model",
        "EVAL_MODEL_API_KEY": "judge-sentinel-never-publish",
        "USER_SIM_MODEL": "simulator-model",
        "USER_SIM_API_KEY": "simulator-sentinel-never-publish",
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
    # The production watchdog polls every 5s; this test's 10s cleanup bound
    # measures kill/cleanup latency, not poll latency, so poll fast here.
    env["AGENT_WATCHDOG_POLL_SECONDS"] = "1"
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
        receipt = json.loads(Path(env["OUTPUT"]).read_text())
        assert {
            "id": receipt["id"],
            "run_nonce": receipt["run_nonce"],
            "campaign_id": receipt["campaign_id"],
            "template": receipt["template"],
            "path_status": receipt["path_status"],
            "error_cause": receipt["error_cause"],
            "error_type": receipt["error_type"],
            "model": receipt["model"],
            "agent_kind": receipt["agent_kind"],
            "model_transport": receipt["model_transport"],
            "eval_model_transport": receipt["eval_model_transport"],
            "max_steps": receipt["max_steps"],
        } == {
            "id": "001",
            "run_nonce": "run-nonce-1",
            "campaign_id": "lifecycle-test",
            "template": IMMUTABLE_GUEST,
            "path_status": "ERROR",
            "error_cause": "task-timeout",
            "error_type": "AgentTaskTimeout",
            "model": "provider/model",
            "agent_kind": "m3",
            "model_transport": "https://example.test/v1",
            "eval_model_transport": "https://judge.test/v1",
            "max_steps": 75,
        }
        serialized_receipt = Path(env["OUTPUT"]).read_text()
        for sentinel in (
            "secret",
            "e2b-sentinel-never-publish",
            "model-sentinel-never-publish",
            "judge-sentinel-never-publish",
            "simulator-sentinel-never-publish",
            "websites-token",
            "gitlab-token",
            "private-token",
        ):
            assert sentinel not in serialized_receipt
        assert receipt["evaluator_ran"] is False
        assert receipt["score"] is None
        assert receipt["steps_taken"] is None
        assert receipt["eval_model_call_attempts"] is None
        assert receipt["eval_model_successes"] is None
        assert receipt["timeout_seconds"] == 1
        assert receipt["wall_clock_s"] >= 1
        assert Path(env["OUTPUT"]).stat().st_mode & 0o777 == 0o600
        assert not list(Path(env["OUTPUT"]).parent.glob(".receipt.json.*.tmp"))
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


def test_timeout_preserves_completed_receipt(tmp_path):
    env = _runner_env(tmp_path, task_timeout=2)
    env["AGENT_WATCHDOG_POLL_SECONDS"] = "1"
    output = Path(env["OUTPUT"])
    completed = json.dumps({"id": "001", "path_status": "OK", "score": 1.0})
    _write_executable(
        tmp_path / "bin" / "uv",
        f"""#!/bin/sh
case "$*" in
  *e2b_relay.py*) trap '' INT TERM; (while :; do :; done) & wait ;;
  *)
    printf '%s' '{completed}' > "$OUTPUT"
    trap '' INT TERM; (while :; do :; done) & wait
    ;;
esac
""",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "runner" / "run_agent.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    receipt = json.loads(output.read_text())
    assert receipt["path_status"] == "OK", result.stderr  # not clobbered
    assert result.returncode == 124  # still reported as timeout


def test_agent_runner_requires_run_nonce_before_launching_resources(tmp_path):
    env = _runner_env(tmp_path, task_timeout=1)
    env.pop("OSWORLD_RUN_NONCE")

    result = subprocess.run(
        ["bash", str(ROOT / "runner" / "run_agent.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "OSWORLD_RUN_NONCE is required" in result.stderr
    assert not Path(env["RELAY_PID_FILE"]).exists()
    assert not Path(env["AGENT_PID_FILE"]).exists()


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


def test_retry_allowlist_includes_task_timeout():
    # A shell-enforced deadline is infrastructure, not a scored model attempt;
    # write_timeout_receipt.py emits error_cause="task-timeout" and the retry
    # wave must be able to pick it up. The allowlist now lives in
    # receipt_safety.RETRYABLE_ERROR_CAUSES, shared by run_agent_parallel.sh
    # (via retry_candidates.py) instead of being duplicated inline.
    sys.path.insert(0, str(ROOT / "runner"))
    from receipt_safety import RETRYABLE_ERROR_CAUSES

    assert "task-timeout" in RETRYABLE_ERROR_CAUSES
    assert "evaluator-or-agent" not in RETRYABLE_ERROR_CAUSES  # scored attempts stay unretried


def test_retry_selection_procsub_survives_macos_bash_3_2(tmp_path):
    # Regression test for a confirmed production bug: the retry-wave selection
    # used to run a python heredoc INSIDE a process substitution
    # (`< <(python3 - ... <<'PY' ... PY )`). macOS system bash 3.2.57 (the only
    # bash on stock Macs) mis-parses that construct: in a live run it spawned
    # the substitution multiple times, emitted "ambiguous redirect", and
    # produced ZERO rows, so retryable failures were silently never retried.
    # This test extracts the *real* selection lines from run_agent_parallel.sh
    # (between "failed_rows=()" and the line after the closing paren) and runs
    # them under /bin/bash — the actual macOS bash 3.2 binary — against fixture
    # receipts. It must fail if someone reintroduces a heredoc-in-procsub here.
    bash32 = Path("/bin/bash")
    if not bash32.exists():
        pytest.skip("/bin/bash is not available on this system")
    version = subprocess.run(
        [str(bash32), "--version"], capture_output=True, text=True, check=True
    ).stdout
    if "version 3." not in version:
        pytest.skip(f"/bin/bash is not bash 3.x here: {version.splitlines()[0]!r}")

    source_lines = (ROOT / "runner" / "run_agent_parallel.sh").read_text().splitlines()
    start = next(i for i, line in enumerate(source_lines) if "failed_rows=()" in line)
    end = next(
        i
        for i, line in enumerate(source_lines)
        if i > start and line.strip() == ")"
    )
    selection_lines = source_lines[start : end + 1]
    assert any("retry_candidates.py" in line for line in selection_lines), (
        "expected the extracted block to invoke retry_candidates.py"
    )

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "001", "domain": "release"},
                    {"id": "002", "domain": "release"},
                ]
            }
        )
    )
    worker_dir = tmp_path / "workers"
    worker_dir.mkdir()
    (worker_dir / "task_001.json").write_text(
        json.dumps({"path_status": "FAIL", "error_cause": "task-timeout"})
    )
    (worker_dir / "task_002.json").write_text(json.dumps({"path_status": "OK"}))

    script = tmp_path / "extracted_selection.sh"
    script.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                'HERE="{here}"'.format(here=ROOT / "runner"),
                'MANIFEST="{manifest}"'.format(manifest=manifest),
                'RAW_DIR="{raw_dir}"'.format(raw_dir=tmp_path),
                *selection_lines,
                "printf '%s\\n' \"${failed_rows[@]}\"",
                "",
            ]
        )
    )
    script.chmod(0o755)

    result = subprocess.run(
        [str(bash32), str(script)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    rows = [line for line in result.stdout.splitlines() if line]
    assert rows == ["001 release"], (rows, result.stdout, result.stderr)
