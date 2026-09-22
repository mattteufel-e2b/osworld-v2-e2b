"""Coordinator behaviour: runner/run_agent_parallel.sh launches one
agent_runner.py per task and owns admission, cancellation and the retry wave.

Every test here drives the real shell script with a fake `uv`/`python3`/`curl`
on PATH, so nothing reaches E2B and each run is bounded.
"""

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


def _hostmap_port_is_busy() -> bool:
    """True when something already listens on the coordinator's hostmap port.

    The coordinator refuses to start while 127.0.0.1:8090 is occupied, so every
    test that drives it past admission needs the port free. A live campaign in
    another terminal legitimately holds it; those tests skip rather than fail.
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", 8090)) == 0


needs_free_hostmap_port = pytest.mark.skipif(
    _hostmap_port_is_busy(),
    reason="127.0.0.1:8090 is occupied (a campaign is running); "
    "the coordinator refuses to start",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _runner_env(tmp_path: Path, *, task_timeout: int) -> dict[str, str]:
    """A coordinator environment whose workers fail immediately without a
    receipt, which is exactly what retry_candidates.py treats as retryable."""
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
    _write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "uv",
        """#!/bin/sh
case "$*" in
  *agents.py*|*check_models.py*) exit 0 ;;
  *agent_runner.py*) exit 1 ;;
  *) exit 0 ;;
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
    (services / "hostmap_proxy.py").write_text("")

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
    tasks = tmp_path / "tasks"
    (tasks / "assets").mkdir(parents=True)
    (tasks / "task_001.py").write_text("TASK = {}\n")

    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": campaign,
        "E2B_API_KEY": "e2b-sentinel-never-publish",
        "MODEL_API_KEY": "model-sentinel-never-publish",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "provider/model",
        "AGENT_KIND": "m3",
        "M3_THINKING_BUDGET": "2048",
        "M3_MAX_LLM_RETRIES": "2",
        "OSWORLD_ROOT": str(osworld),
        "OSWORLD_TASKS_DIR": str(tasks),
        "OSWORLD_SERVICES_DIR": str(services),
        "AGENT_MANIFEST": str(manifest),
        "RAW_DIR": str(tmp_path / "raw"),
        "OUTPUT": str(tmp_path / "receipt.json"),
        "AGENT_TASK_TIMEOUT_SECONDS": str(task_timeout),
    }


def test_maintainer_readme_bounds_canary_but_restores_sample_retry_policy():
    readme = (ROOT / "maintainer" / "README.md").read_text()

    canary_retry = "M3_MAX_LLM_RETRIES=0"
    canary_timeout = "AGENT_TASK_TIMEOUT_SECONDS=900"
    sample_retry = "export M3_MAX_LLM_RETRIES=2"
    sample_campaign = 'export OSWORLD_CAMPAIGN_ID="osworld-v2-sample24-'

    assert readme.index(canary_retry) < readme.index(canary_timeout)
    assert readme.index(canary_timeout) < readme.index(sample_retry)
    assert readme.index(sample_retry) < readme.index(sample_campaign)


def test_retry_allowlist_includes_task_timeout():
    # A deadline the runner enforces on itself is infrastructure, not a scored
    # model attempt; agent_runner.py emits error_cause="task-timeout" and the
    # retry wave must be able to pick it up. The allowlist lives in
    # receipt_safety.RETRYABLE_ERROR_CAUSES, shared by run_agent_parallel.sh
    # (via retry_candidates.py) instead of being duplicated inline.
    sys.path.insert(0, str(ROOT / "runner"))
    from receipt_safety import RETRYABLE_ERROR_CAUSES

    assert "task-timeout" in RETRYABLE_ERROR_CAUSES
    assert (
        "evaluator-or-agent" not in RETRYABLE_ERROR_CAUSES
    )  # scored attempts stay unretried


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
        i for i, line in enumerate(source_lines) if i > start and line.strip() == ")"
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


def _coordinator_env(tmp_path, *, fleetlib_case: str = "") -> dict[str, str]:
    env = _runner_env(tmp_path, task_timeout=1)
    env.update(
        PARALLEL_CONCURRENCY="1",
        AGENT_RETRY_ATTEMPTS="1",
        AGENT_START_STAGGER_SECONDS="0",
        REQUIRE_NO_MODEL_COVERAGE="0",
        FLEET_STOPPED_FILE=str(tmp_path / "fleets-stopped"),
    )
    uv = tmp_path / "bin/uv"
    uv.write_text(
        uv.read_text().replace(
            'case "$*" in',
            f"""case "$*" in
{fleetlib_case}
  *hostmap_proxy.py*) exec sleep 60 ;;
  *stop.py*) touch "$FLEET_STOPPED_FILE"; exit 0 ;;""",
        )
    )
    return env


def _wait_for_file(path: Path, timeout: float = 20) -> int:
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
    raise AssertionError(f"process {pid} survived coordinator cleanup")


@needs_free_hostmap_port
def test_coordinator_cancellation_reaps_workers_before_stopping_fleets(tmp_path):
    # Cancelling a campaign must terminate every worker before the fleets are
    # stopped. `$!` has to BE the worker, not an intermediate shell: an orphaned
    # agent_runner.py holds a guest sandbox open and keeps writing into a run the
    # operator has abandoned. The fake stop.py fails if the worker is still alive.
    env = _runner_env(tmp_path, task_timeout=60)
    env.update(
        PARALLEL_CONCURRENCY="1",
        AGENT_RETRY_ATTEMPTS="0",
        AGENT_START_STAGGER_SECONDS="0",
        REQUIRE_NO_MODEL_COVERAGE="0",
        TEARDOWN_FLEETS_ON_EXIT="1",
        FLEET_STOPPED_FILE=str(tmp_path / "fleets-stopped"),
        WORKER_PID_FILE=str(tmp_path / "worker-pid"),
    )
    uv = tmp_path / "bin/uv"
    uv.write_text(
        uv.read_text().replace(
            'case "$*" in',
            """case "$*" in
  *fleetlib.py*) exit 0 ;;
  *hostmap_proxy.py*) exec sleep 60 ;;
  *agent_runner.py*) echo $$ > "$WORKER_PID_FILE"; exec sleep 120 ;;
  *stop.py*)
    if kill -0 "$(cat "$WORKER_PID_FILE")" 2>/dev/null; then exit 1; fi
    touch "$FLEET_STOPPED_FILE"
    exit 0
    ;;""",
        )
    )
    process = subprocess.Popen(
        ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        worker_pid = _wait_for_file(Path(env["WORKER_PID_FILE"]))
        process.terminate()
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 143, (stdout, stderr)
        _assert_process_gone(worker_pid)
        assert Path(env["FLEET_STOPPED_FILE"]).exists(), (stdout, stderr)
    finally:
        try:
            os.kill(
                int(Path(env["WORKER_PID_FILE"]).read_text().strip()), signal.SIGKILL
            )
        except (OSError, ValueError):
            pass
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_coordinator_rejected_before_admission_leaves_fleets_running(tmp_path):
    # A lifetime rejection tells the operator to reuse the fleets with a
    # smaller manifest; tearing them down in the EXIT trap made that impossible.
    env = _coordinator_env(tmp_path, fleetlib_case="  *fleetlib.py*) exit 1 ;;")
    result = subprocess.run(
        ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "left running" in result.stderr
    assert not Path(env["FLEET_STOPPED_FILE"]).exists()


def test_occupied_hostmap_port_rejects_the_run_but_leaves_fleets_running(tmp_path):
    # The 8090 occupancy probe runs after the fleet lifetime check but before
    # the first batch launches; a purely local failure there must still leave
    # the fleets running, exactly like a rejection at the lifetime gate.
    import socket

    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 8090))
    except OSError:
        pytest.skip("127.0.0.1:8090 is already in use on this machine")
    blocker.listen(1)
    try:
        env = _coordinator_env(tmp_path)
        result = subprocess.run(
            ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    finally:
        blocker.close()
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "already occupied" in result.stderr
    assert "left running" in result.stderr
    assert not Path(env["FLEET_STOPPED_FILE"]).exists()


@needs_free_hostmap_port
def test_retry_wave_is_skipped_when_fleets_cannot_outlast_it(tmp_path):
    # The first wave is admitted; the retry wave is budgeted separately against
    # the tasks that actually failed and skipped (not fatal) when it cannot fit.
    env = _coordinator_env(
        tmp_path,
        fleetlib_case="  *--task-id*) exit 1 ;;\n  *fleetlib.py*) exit 0 ;;",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    workers = Path(env["RAW_DIR"], "workers")
    assert (workers / "task_001.log").exists(), (result.stdout, result.stderr)
    assert not (workers / "task_001_retry_1.log").exists()
    assert "skipping retry" in result.stdout + result.stderr
    assert result.returncode == 1  # the failed task is still a failure
    assert Path(env["FLEET_STOPPED_FILE"]).exists()  # admitted runs tear down


@needs_free_hostmap_port
def test_retry_wave_writes_retries_json_from_its_own_task_list(tmp_path):
    # The coordinator writes the wave's own task list to retries.json, not a
    # glob over receipt files aggregate_agent.py would otherwise have to infer
    # retries from. The fleet lifetime check must pass here (opposite of
    # test_retry_wave_is_skipped_when_fleets_cannot_outlast_it) so the retry
    # wave actually runs.
    env = _coordinator_env(tmp_path, fleetlib_case="  *fleetlib.py*) exit 0 ;;")
    result = subprocess.run(
        ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    workers = Path(env["RAW_DIR"], "workers")
    retries = json.loads((workers / "retries.json").read_text())
    assert retries == [{"attempt": 1, "task_ids": ["001"]}], (
        result.stdout,
        result.stderr,
    )


def _coordinator_env_recording_agent_args(
    tmp_path: Path,
) -> tuple[dict[str, str], Path]:
    """Run run_agent_parallel.sh past admission with a fake uv that records the
    argv agent_runner.py would have received, then exits 0."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "agent-argv.txt"
    env_file = tmp_path / "agent-env.txt"
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *agent_runner.py*) echo "$*" > "$AGENT_ARGS_FILE"; '
        'env | grep -E "^(OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS|OSWORLD_EVAL_MODEL_RETRY_DELAY)=" '
        '> "$AGENT_ENV_FILE"; exit 0 ;;\n'
        "  *hostmap_proxy.py*) exec sleep 60 ;;\n"  # readiness loop needs a live proxy pid
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (fake_bin / "uv").chmod(0o755)
    (fake_bin / "python3").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  */preflight.py|*/prepare_agent_run.py|*/aggregate_agent.py)\n"
        '    [ "${1##*/}" = prepare_agent_run.py ] && '
        '{ echo "$*" > "$PREPARE_ARGS_FILE"; echo nonce; }\n'
        '    [ "${1##*/}" = aggregate_agent.py ] && echo "$*" > "$AGGREGATE_ARGS_FILE"\n'
        "    exit 0 ;;\n"
        '  *) exec "$REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    (fake_bin / "python3").chmod(0o755)
    (fake_bin / "curl").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "curl").chmod(0o755)
    osworld = tmp_path / "OSWorld-V2"
    osworld.mkdir()
    tasks = tmp_path / "tasks"
    (tasks / "assets").mkdir(parents=True)
    (tasks / "task_001.py").write_text("TASK = {}\n")
    services = tmp_path / "services"
    services.mkdir()
    (services / ".runtime.json").write_text(
        json.dumps(
            {
                "websites": {
                    "public_host_suffix": "127.0.0.1.nip.io:8090",
                    "campaign_id": "c",
                },
                "gitlab": {
                    "url": "http://gitlab.127.0.0.1.nip.io:8090",
                    "campaign_id": "c",
                },
            }
        )
    )
    (services / ".gitlab-token").write_text("tok\n")
    (services / "hostmap_proxy.py").write_text("import time\ntime.sleep(5)\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "template": IMMUTABLE_GUEST,
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "001", "domain": "t"}],
            }
        )
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "AGENT_ARGS_FILE": str(args_file),
        "AGENT_ENV_FILE": str(env_file),
        "PREPARE_ARGS_FILE": str(tmp_path / "prepare-argv.txt"),
        "AGGREGATE_ARGS_FILE": str(tmp_path / "aggregate-argv.txt"),
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": "c",
        "E2B_API_KEY": "dummy",
        "OSWORLD_ROOT": str(osworld),
        "OSWORLD_TASKS_DIR": str(tasks),
        "OSWORLD_SERVICES_DIR": str(services),
        "AGENT_MANIFEST": str(manifest),
        "RAW_DIR": str(tmp_path / "raw"),
        "OUTPUT": str(tmp_path / "agent.json"),
        "MODEL_API_KEY": "dummy",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "test-model",
        "AGENT_KIND": "m3",
        "M3_THINKING_BUDGET": "2048",
        "M3_MAX_LLM_RETRIES": "0",
        "TEARDOWN_FLEETS_ON_EXIT": "0",
        "AGENT_TASK_TIMEOUT_SECONDS": "30",
    }
    for name in (
        "MAX_TOKENS",
        "TEMPERATURE",
        "TOP_P",
        "MAX_TRAJECTORY_LENGTH",
        "ENABLE_RECORDING",
        "SLEEP_AFTER_EXECUTION",
        "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS",
        "OSWORLD_EVAL_MODEL_RETRY_DELAY",
    ):
        env.pop(name, None)
    return env, args_file


def _run_coordinator_recording(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "runner" / "run_agent_parallel.sh")],
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_agent_kind_is_required_and_fails_closed(tmp_path):
    # AGENT_KIND used to default to "prompt", silently burning a full-budget
    # 500-step campaign on the wrong agent if the operator forgot to set it.
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    env.pop("AGENT_KIND", None)
    result = _run_coordinator_recording(env)
    assert result.returncode != 0
    assert "AGENT_KIND" in result.stderr


@needs_free_hostmap_port
def test_agent_task_timeout_defaults_to_eight_hours(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    env.pop("AGENT_TASK_TIMEOUT_SECONDS", None)
    _run_coordinator_recording(env)
    assert "--deadline-seconds 28800" in args_file.read_text()


@needs_free_hostmap_port
def test_coordinator_sets_default_judge_and_simulator_retry_budget(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    _run_coordinator_recording(env)
    assert args_file.exists()  # the worker did launch
    dumped = Path(env["AGENT_ENV_FILE"]).read_text()
    assert "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS=8" in dumped
    assert "OSWORLD_EVAL_MODEL_RETRY_DELAY=10" in dumped


@needs_free_hostmap_port
def test_coordinator_respects_an_operator_override_of_the_retry_budget(tmp_path):
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    env["OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS"] = "3"
    env["OSWORLD_EVAL_MODEL_RETRY_DELAY"] = "1.5"
    _run_coordinator_recording(env)
    dumped = Path(env["AGENT_ENV_FILE"]).read_text()
    assert "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS=3" in dumped
    assert "OSWORLD_EVAL_MODEL_RETRY_DELAY=1.5" in dumped


@needs_free_hostmap_port
def test_coordinator_defaults_claude_sleep_after_execution_to_zero(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    env["AGENT_KIND"] = "claude"
    env.pop("SLEEP_AFTER_EXECUTION", None)
    _run_coordinator_recording(env)
    assert "--sleep-after-execution 0" in args_file.read_text()


@needs_free_hostmap_port
def test_coordinator_forwards_generation_settings_and_deadline_to_the_runner(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    env.update(
        MAX_TOKENS="4096",
        TEMPERATURE="0.2",
        TOP_P="0.95",
        MAX_TRAJECTORY_LENGTH="5",
        SLEEP_AFTER_EXECUTION="0",
    )
    _run_coordinator_recording(env)
    argv = args_file.read_text()
    for expected in (
        "--max-tokens 4096",
        "--temperature 0.2",
        "--top-p 0.95",
        "--max-trajectory-length 5",
        "--sleep-after-execution 0",
        "--deadline-seconds 30",
        "--agent-kind m3",
    ):
        assert expected in argv, (expected, argv)
    assert "PORT_BASE" not in argv and "--port-base" not in argv


@needs_free_hostmap_port
def test_coordinator_leaves_generation_settings_to_agent_defaults_when_unset(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    for name in (
        "MAX_TOKENS",
        "TEMPERATURE",
        "TOP_P",
        "MAX_TRAJECTORY_LENGTH",
        "SLEEP_AFTER_EXECUTION",
    ):
        env.pop(name, None)
    _run_coordinator_recording(env)
    argv = args_file.read_text()
    for flag in (
        "--max-tokens",
        "--temperature",
        "--top-p",
        "--max-trajectory-length",
        "--sleep-after-execution",
    ):
        assert flag not in argv, flag


@needs_free_hostmap_port
def test_coordinator_forwards_recording_opt_in_only_when_set(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    env["ENABLE_RECORDING"] = "1"
    _run_coordinator_recording(env)
    assert "--enable-recording" in args_file.read_text()
    env.pop("ENABLE_RECORDING")
    args_file.unlink()
    _run_coordinator_recording(env)
    assert "--enable-recording" not in args_file.read_text()


def test_coordinator_rejects_non_boolean_recording_value(tmp_path):
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    env["ENABLE_RECORDING"] = "yes"
    result = _run_coordinator_recording(env)
    assert result.returncode == 2
    assert "ENABLE_RECORDING must be 0 or 1" in result.stderr


@needs_free_hostmap_port
def test_task_082_worker_gets_the_literal_service_port(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    manifest = Path(env["AGENT_MANIFEST"])
    data = json.loads(manifest.read_text())
    data["tasks"] = [{"id": "082", "domain": "t"}]
    manifest.write_text(json.dumps(data))
    (Path(env["OSWORLD_TASKS_DIR"]) / "task_082.py").write_text("TASK = {}\n")
    (tmp_path / "bin" / "uv").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *agent_runner.py*) echo "OSWORLD_TASK_SERVICE_PORTS=$OSWORLD_TASK_SERVICE_PORTS" > "$AGENT_ARGS_FILE"; exit 0 ;;\n'
        "  *hostmap_proxy.py*) exec sleep 60 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    _run_coordinator_recording(env)
    assert args_file.read_text().strip() == "OSWORLD_TASK_SERVICE_PORTS=3000:3000"


POOL_UV = r"""#!/bin/sh
case "$*" in
  *agent_runner.py*)
    prev=""
    tid=""
    for a in "$@"; do
      if [ "$prev" = "--task-id" ]; then tid="$a"; fi
      prev="$a"
    done
    "$REAL_PYTHON" -c 'import sys,time; open(sys.argv[1],"w").write(str(time.time()))' "$MARKS/start_$tid"
    sleep "$(cat "$MARKS/sleep_$tid")"
    "$REAL_PYTHON" -c 'import sys,time; open(sys.argv[1],"w").write(str(time.time()))' "$MARKS/end_$tid"
    exit 0 ;;
  *hostmap_proxy.py*) exec sleep 60 ;;
  *) exit 0 ;;
esac
"""


def _set_manifest_tasks(env: dict[str, str], task_ids: list[str]) -> None:
    manifest = Path(env["AGENT_MANIFEST"])
    data = json.loads(manifest.read_text())
    data["tasks"] = [{"id": task_id, "domain": "t"} for task_id in task_ids]
    manifest.write_text(json.dumps(data))
    for task_id in task_ids:
        (Path(env["OSWORLD_TASKS_DIR"]) / f"task_{task_id}.py").write_text(
            "TASK = {}\n"
        )


@needs_free_hostmap_port
def test_rolling_pool_starts_the_next_task_as_soon_as_a_slot_frees(tmp_path):
    # A fixed-batch scheduler leaves the whole run waiting on its slowest task
    # before launching anything else; over 108 tasks at 500 steps that is hours
    # of idle paid capacity. The pool must refill a slot the moment one frees,
    # and must never exceed PARALLEL_CONCURRENCY while doing it.
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    _set_manifest_tasks(env, ["001", "002", "003"])
    marks = tmp_path / "marks"
    marks.mkdir()
    for task_id, seconds in (("001", 1), ("002", 8), ("003", 0)):
        (marks / f"sleep_{task_id}").write_text(str(seconds))
    _write_executable(tmp_path / "bin" / "uv", POOL_UV)
    env.update(
        MARKS=str(marks),
        PARALLEL_CONCURRENCY="2",
        POOL_POLL_SECONDS="1",
        AGENT_START_STAGGER_SECONDS="0",
    )

    result = _run_coordinator_recording(env)

    stamps = {
        p.name: float(p.read_text())
        for p in marks.iterdir()
        if p.name.startswith(("start_", "end_"))
    }
    assert set(stamps) == {
        "start_001",
        "end_001",
        "start_002",
        "end_002",
        "start_003",
        "end_003",
    }, (stamps, result.stdout, result.stderr)
    # Rolling: 003 started while the slow 002 was still running.
    assert stamps["start_003"] < stamps["end_002"]
    # Bounded: it waited for 001's slot rather than running three at once.
    assert stamps["start_003"] >= stamps["end_001"]


@needs_free_hostmap_port
@pytest.mark.parametrize(
    "receipt,expected",
    [
        ('{"score": 0.5, "error_cause": null}', "score=0.5 cause=-"),
        ('{"error_cause": "task-timeout"}', "score=none cause=task-timeout"),
        ("not json at all", "score=none cause=-"),
    ],
)
def test_coordinator_prints_a_progress_line_per_finished_worker(
    tmp_path, receipt, expected
):
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    (tmp_path / "receipt-body").write_text(receipt)
    _write_executable(
        tmp_path / "bin" / "uv",
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *agent_runner.py*) "
        'cat "$RECEIPT_BODY" > "$RAW_DIR/workers/task_001.json"; exit 3 ;;\n'
        "  *hostmap_proxy.py*) exec sleep 60 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    env.update(RECEIPT_BODY=str(tmp_path / "receipt-body"), POOL_POLL_SECONDS="1")

    result = _run_coordinator_recording(env)

    pattern = (
        r"^task 001 exit=3 "
        + expected.replace(".", r"\.")
        + r" done=1/1 running=0 elapsed=\d\d:\d\d:\d\d$"
    )
    assert re.search(pattern, result.stdout, re.M), (pattern, result.stdout)


def test_parallel_concurrency_above_the_hard_cap_is_rejected(tmp_path):
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    env["PARALLEL_CONCURRENCY"] = "121"
    result = _run_coordinator_recording(env)
    assert result.returncode == 2
    assert "must not exceed 120" in result.stderr


@needs_free_hostmap_port
def test_parallel_concurrency_of_120_is_admitted(tmp_path):
    # The account admitted 201 sandboxes in the live capacity probe, so an
    # operator who has confirmed the org quota may raise the default of 80.
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    env["PARALLEL_CONCURRENCY"] = "120"
    result = _run_coordinator_recording(env)
    assert "must not exceed" not in result.stderr
    assert Path(env["AGENT_ARGS_FILE"]).exists(), (result.stdout, result.stderr)


@needs_free_hostmap_port
def test_resume_skips_scored_tasks_and_asks_prepare_to_keep_them(tmp_path):
    # Re-buying a finished 500-step rollout is the expensive failure mode of a
    # coordinator crash; a resumed run must launch only what is still unscored.
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    _set_manifest_tasks(env, ["001", "002"])
    workers = Path(env["RAW_DIR"]) / "workers"
    (workers / "task_001").mkdir(parents=True)
    (workers / "task_001" / "result.txt").write_text("1.0")
    _write_executable(
        tmp_path / "bin" / "uv",
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *agent_runner.py*) echo "$*" >> "$AGENT_ARGS_FILE"; exit 0 ;;\n'
        "  *hostmap_proxy.py*) exec sleep 60 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    env.update(RESUME_RUN_ID="an-earlier-run", AGENT_START_STAGGER_SECONDS="0")

    result = _run_coordinator_recording(env)

    assert "--resume" in Path(env["PREPARE_ARGS_FILE"]).read_text()
    launched = args_file.read_text()
    assert "--task-id 002" in launched, (launched, result.stdout, result.stderr)
    assert "--task-id 001" not in launched


@needs_free_hostmap_port
def test_interrupted_run_still_writes_a_campaign_receipt(tmp_path):
    # An operator who cancels (or a coordinator that dies) after hours of
    # rollouts used to get no aggregate at all, leaving the receipts unread.
    env = _runner_env(tmp_path, task_timeout=60)
    env.update(
        PARALLEL_CONCURRENCY="1",
        AGENT_RETRY_ATTEMPTS="0",
        AGENT_START_STAGGER_SECONDS="0",
        POOL_POLL_SECONDS="1",
        REQUIRE_NO_MODEL_COVERAGE="0",
        TEARDOWN_FLEETS_ON_EXIT="1",
        FLEET_STOPPED_FILE=str(tmp_path / "fleets-stopped"),
        WORKER_PID_FILE=str(tmp_path / "worker-pid"),
    )
    uv = tmp_path / "bin/uv"
    uv.write_text(
        uv.read_text().replace(
            'case "$*" in',
            """case "$*" in
  *fleetlib.py*) exit 0 ;;
  *hostmap_proxy.py*) exec sleep 60 ;;
  *agent_runner.py*) echo $$ > "$WORKER_PID_FILE"; exec sleep 120 ;;
  *stop.py*) touch "$FLEET_STOPPED_FILE"; exit 0 ;;""",
        )
    )
    process = subprocess.Popen(
        ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_file(Path(env["WORKER_PID_FILE"]))
        process.terminate()
        stdout, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 143, (stdout, stderr)
    receipt = json.loads(Path(env["OUTPUT"]).read_text())
    assert receipt["summary"]["expected_tasks"] == 1
    assert receipt["summary"]["attested_records"] == 0
    assert Path(env["FLEET_STOPPED_FILE"]).exists()


PROGRESS_LINE = re.compile(
    r"^task (?P<id>\S+) exit=\S+ score=\S+ cause=\S+ "
    r"done=(?P<done>\d+)/(?P<total>\d+) running=(?P<running>\d+) "
    r"elapsed=(?P<h>\d\d):(?P<m>\d\d):(?P<s>\d\d)$",
    re.M,
)


@needs_free_hostmap_port
def test_progress_counter_spans_every_pool_of_one_run(tmp_path):
    # done/N and elapsed describe the campaign, not whichever pool is running.
    # Per-pool counters restarted at done=1/1 for the solo-082 pool and for
    # every retry wave, which reads as a run that keeps starting over.
    env, _ = _coordinator_env_recording_agent_args(tmp_path)
    _set_manifest_tasks(env, ["001", "082"])
    marks = tmp_path / "marks"
    marks.mkdir()
    (marks / "sleep_001").write_text("2")
    (marks / "sleep_082").write_text("0")
    _write_executable(tmp_path / "bin" / "uv", POOL_UV)
    env.update(
        MARKS=str(marks),
        RUN_TASK_082_CONCURRENT="0",  # 082 runs alone after the pool drains
        POOL_POLL_SECONDS="1",
        AGENT_START_STAGGER_SECONDS="0",
    )

    result = _run_coordinator_recording(env)

    progress = {m.group("id"): m for m in PROGRESS_LINE.finditer(result.stdout)}
    assert set(progress) == {"001", "082"}, (result.stdout, result.stderr)
    assert progress["001"].group("done", "total") == ("1", "2")
    # The solo pool is the run's second: it continues the count, not restarts it.
    assert progress["082"].group("done", "total") == ("2", "2")
    seconds = int(progress["082"].group("s")) + 60 * int(progress["082"].group("m"))
    assert seconds >= 2, (result.stdout,)  # the clock started with the first pool


def _watchdog_env(tmp_path: Path, proxy_case: str) -> dict[str, str]:
    """A coordinator run whose workers idle, so the watchdog gets time to act."""
    env = _runner_env(tmp_path, task_timeout=60)
    env.update(
        PARALLEL_CONCURRENCY="1",
        AGENT_RETRY_ATTEMPTS="0",
        AGENT_START_STAGGER_SECONDS="0",
        POOL_POLL_SECONDS="1",
        PROXY_WATCHDOG_SECONDS="1",
        FLEET_LIVENESS_SECONDS="1",
        REQUIRE_NO_MODEL_COVERAGE="0",
        TEARDOWN_FLEETS_ON_EXIT="1",
        FLEET_STOPPED_FILE=str(tmp_path / "fleets-stopped"),
        WORKER_PID_FILE=str(tmp_path / "worker-pid"),
        PROXY_STARTS_FILE=str(tmp_path / "proxy-starts"),
    )
    uv = tmp_path / "bin/uv"
    uv.write_text(
        uv.read_text().replace(
            'case "$*" in',
            f"""case "$*" in
  *fleetlib.py*) exit 0 ;;
{proxy_case}
  *agent_runner.py*) echo $$ > "$WORKER_PID_FILE"; exec sleep 120 ;;
  *stop.py*) touch "$FLEET_STOPPED_FILE"; exit 0 ;;""",
        )
    )
    # The liveness probe reads GITLAB_URL and the campaign CA out of the
    # runtime file, so this one needs the `tls` section a real campaign writes.
    runtime_path = Path(env["OSWORLD_SERVICES_DIR"]) / ".runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["tls"] = {
        key: str(tmp_path / f"{key}.pem")
        for key in ("ca_cert", "bundle", "leaf_cert", "leaf_key")
    }
    runtime_path.write_text(json.dumps(runtime))
    return env


def _run_until(env: dict[str, str], ready, timeout: float = 45):
    """Drive the coordinator until `ready()` holds, then cancel it."""
    process = subprocess.Popen(
        ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    satisfied = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            if ready():
                satisfied = True
                break
            time.sleep(0.2)
        process.terminate()
        stdout, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        for pid_file in (
            Path(env["WORKER_PID_FILE"]),
            Path(env["RAW_DIR"]) / "hostmap-proxy.pid",
        ):
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
    return satisfied, stdout, stderr


@needs_free_hostmap_port
def test_proxy_watchdog_restarts_a_dead_hostmap_proxy(tmp_path):
    # Every worker's setup and evaluate traffic goes through this one proxy: if
    # it dies mid-campaign, every remaining task fails while still buying model
    # tokens. The watchdog notices and starts a replacement.
    # `exec` makes $$ the pid of the proxy itself, so the file records exactly
    # which processes were started as proxies, in order.
    env = _watchdog_env(
        tmp_path,
        '  *hostmap_proxy.py*) echo $$ >> "$PROXY_STARTS_FILE"; exec sleep 1 ;;',
    )
    log = Path(env["RAW_DIR"]) / "hostmap-proxy.log"
    starts = Path(env["PROXY_STARTS_FILE"])

    def restarted():
        return (
            log.exists()
            and "hostmap proxy died; restarting" in log.read_text()
            and starts.exists()
            and len(starts.read_text().split()) >= 2
        )

    satisfied, stdout, stderr = _run_until(env, restarted)

    assert satisfied, (stdout, stderr)
    assert "hostmap proxy died; restarting" in stderr
    started_pids = starts.read_text().split()
    assert started_pids[0] != started_pids[-1], (started_pids, stdout, stderr)
    # The restarted proxy -- not the dead one cleanup would otherwise chase --
    # is the pid left on disk.
    pid_file = Path(env["RAW_DIR"]) / "hostmap-proxy.pid"
    assert pid_file.read_text().strip() != started_pids[0], (stdout, stderr)


@needs_free_hostmap_port
def test_refused_proxy_restart_empties_the_pid_file_and_backs_off(tmp_path):
    # A restart the occupancy check refuses leaves no proxy of this run alive,
    # so the pid file must not keep pointing at the dead one: cleanup would
    # later kill whatever process has inherited that pid. The watchdog also
    # stops hammering 8090 -- one line per backoff window, not a 40-line proxy
    # log tail every cycle -- while the liveness probes keep running.
    env = _watchdog_env(
        tmp_path,
        '  *hostmap_proxy.py*) echo $$ >> "$PROXY_STARTS_FILE"; exec sleep 2 ;;',
    )
    pid_file = Path(env["RAW_DIR"]) / "hostmap-proxy.pid"
    liveness = Path(env["RAW_DIR"]) / "fleet-liveness.log"
    bystander = None
    state: dict[str, int | None] = {"lines_at_failure": None}

    def liveness_lines() -> int:
        return len(liveness.read_text().splitlines()) if liveness.exists() else 0

    def recorded_pid() -> str:
        return pid_file.read_text().strip() if pid_file.exists() else ""

    def backed_off():
        nonlocal bystander
        if bystander is None:
            # Only once the run's own proxy is up: occupying 8090 any earlier
            # would fail admission instead of the watchdog's restart.
            if not recorded_pid():
                return False
            bystander = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import socket, time\n"
                    "s = socket.socket()\n"
                    "s.bind(('127.0.0.1', 8090))\n"
                    "s.listen(1)\n"
                    "print('bound', flush=True)\n"
                    "time.sleep(120)\n",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert bystander.stdout.readline().strip() == "bound"
            return False
        if state["lines_at_failure"] is None:
            if recorded_pid():
                return False
            state["lines_at_failure"] = liveness_lines()
            return False
        # Probes keep going through the backoff window.
        return liveness_lines() >= state["lines_at_failure"] + 3

    try:
        satisfied, stdout, stderr = _run_until(env, backed_off)
    finally:
        if bystander is not None:
            bystander.kill()
            bystander.wait()

    assert satisfied, (stdout, stderr)
    assert pid_file.read_text().strip() == "", (stdout, stderr)
    assert bystander.returncode is None or bystander.returncode < 0, (
        # killed by this test's own teardown, never by the coordinator's cleanup
        bystander.returncode,
        stderr,
    )
    assert stderr.count("127.0.0.1:8090 is already occupied") == 1, stderr
    # PROXY_WATCHDOG_SECONDS=1, so five skipped cycles is five seconds.
    assert stderr.count("hostmap proxy restart failed; retrying in 5 s") == 1, stderr


@needs_free_hostmap_port
def test_fleet_liveness_probes_are_logged_and_escalated_once(tmp_path):
    # A fleet that stops answering is invisible until every worker has failed.
    # The watchdog probes both fleets and says so once per outage. GitLab's API
    # answers 401 to an unauthenticated caller, so this fake curl succeeds on
    # that URL only when the probe carries the campaign's PRIVATE-TOKEN: a
    # tokenless probe would report an outage on a healthy fleet, every probe.
    env = _watchdog_env(tmp_path, "  *hostmap_proxy.py*) exec sleep 120 ;;")
    curl_count = tmp_path / "curl-count"
    gitlab_probes = tmp_path / "gitlab-probes"
    curl = tmp_path / "bin" / "curl"
    _write_executable(
        curl,
        # The readiness probe must succeed or the run never starts; every
        # website liveness probe after it fails.
        f"""#!/bin/sh
case "$*" in
  *gitlab.example.test*)
    case "$*" in
      *"PRIVATE-TOKEN: private-token"*)
        echo authorized >> "{gitlab_probes}"; exit 0 ;;
      *)
        echo unauthenticated >> "{gitlab_probes}"; exit 22 ;;
    esac ;;
esac
count=$(cat "{curl_count}" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "{curl_count}"
[ "$count" -le 1 ]
""",
    )
    liveness = Path(env["RAW_DIR"]) / "fleet-liveness.log"

    def escalated():
        return liveness.exists() and liveness.read_text().count("websites=fail") >= 4

    satisfied, stdout, stderr = _run_until(env, escalated)

    assert satisfied, (stdout, stderr, liveness.exists() and liveness.read_text())
    lines = liveness.read_text().splitlines()
    assert all(
        re.match(r"^\S+Z websites=(ok|fail) gitlab=(ok|fail|skip)$", line)
        for line in lines
    ), lines
    # The authenticated probe reaches a healthy GitLab; only websites is down.
    assert all(line.endswith("gitlab=ok") for line in lines), lines
    assert gitlab_probes.read_text().split() == ["authorized"] * len(lines), (
        gitlab_probes.read_text(),
        lines,
    )
    websites_outage = (
        "FLEET LIVENESS: websites unreachable for 3 probes; see fleet-liveness.log"
    )
    assert stderr.count(websites_outage) == 1, stderr
    assert "FLEET LIVENESS: gitlab" not in stderr, stderr
    # The same probe without the header is exactly what the coordinator used to
    # send, and this fake GitLab rejects it -- so gitlab=ok is not free.
    unauthenticated = subprocess.run(
        [str(curl), "-fsS", "https://gitlab.example.test/api/v4/version"],
        capture_output=True,
        text=True,
    )
    assert unauthenticated.returncode != 0, unauthenticated


def test_stale_proxy_pid_file_is_never_killed(tmp_path):
    # RUN_ID reuses RAW_DIR on a resume, so hostmap-proxy.pid can already hold
    # a pid from an earlier run -- by now some unrelated process. The EXIT trap
    # is armed before the pre-admission gates, so a rejection there used to run
    # cleanup with nothing but that stale pid to go on and killed a bystander.
    import socket

    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 8090))
    except OSError:
        pytest.skip("127.0.0.1:8090 is already in use on this machine")
    blocker.listen(1)
    bystander = subprocess.Popen(["sleep", "120"])
    env = _coordinator_env(tmp_path)
    raw_dir = Path(env["RAW_DIR"])
    raw_dir.mkdir(parents=True, exist_ok=True)
    pid_file = raw_dir / "hostmap-proxy.pid"
    pid_file.write_text(f"{bystander.pid}\n")
    try:
        result = subprocess.run(
            ["bash", str(ROOT / "runner/run_agent_parallel.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "already occupied" in result.stderr
        assert bystander.poll() is None, (result.stdout, result.stderr)
        assert pid_file.read_text().strip() == ""
    finally:
        blocker.close()
        bystander.kill()
        bystander.wait()
