"""Coordinator behaviour: runner/run_agent_parallel.sh launches one
agent_runner.py per task and owns admission, cancellation and the retry wave.

Every test here drives the real shell script with a fake `uv`/`python3`/`curl`
on PATH, so nothing reaches E2B and each run is bounded.
"""

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
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *agent_runner.py*) echo "$*" > "$AGENT_ARGS_FILE"; exit 0 ;;\n'
        "  *hostmap_proxy.py*) exec sleep 60 ;;\n"  # readiness loop needs a live proxy pid
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (fake_bin / "uv").chmod(0o755)
    (fake_bin / "python3").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  */preflight.py|*/prepare_agent_run.py|*/aggregate_agent.py) "
        '[ "${1##*/}" = prepare_agent_run.py ] && echo nonce; exit 0 ;;\n'
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


@needs_free_hostmap_port
def test_coordinator_forwards_generation_settings_and_deadline_to_the_runner(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    env.update(
        MAX_TOKENS="4096", TEMPERATURE="0.2", TOP_P="0.95", MAX_TRAJECTORY_LENGTH="5"
    )
    _run_coordinator_recording(env)
    argv = args_file.read_text()
    for expected in (
        "--max-tokens 4096",
        "--temperature 0.2",
        "--top-p 0.95",
        "--max-trajectory-length 5",
        "--deadline-seconds 30",
        "--agent-kind m3",
    ):
        assert expected in argv, (expected, argv)
    assert "PORT_BASE" not in argv and "--port-base" not in argv


@needs_free_hostmap_port
def test_coordinator_leaves_generation_settings_to_agent_defaults_when_unset(tmp_path):
    env, args_file = _coordinator_env_recording_agent_args(tmp_path)
    for name in ("MAX_TOKENS", "TEMPERATURE", "TOP_P", "MAX_TRAJECTORY_LENGTH"):
        env.pop(name, None)
    _run_coordinator_recording(env)
    argv = args_file.read_text()
    for flag in ("--max-tokens", "--temperature", "--top-p", "--max-trajectory-length"):
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
