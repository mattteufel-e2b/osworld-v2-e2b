from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_GUEST = "osworld-v2-gnome:11111111-2222-3333-4444-555555555555"


def _minimal_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    osworld = tmp_path / "OSWorld-V2"
    osworld.mkdir()
    tasks = tmp_path / "tasks"
    (tasks / "assets").mkdir(parents=True)
    (tasks / "task_001.py").write_text("TASK = {}\n")
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
    services = tmp_path / "services"
    services.mkdir()
    return osworld, tasks, manifest, services


def test_all_runners_fail_before_launching_when_service_runtime_is_missing(tmp_path):
    osworld, tasks, manifest, services = _minimal_inputs(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_called = tmp_path / "uv-called"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(f'#!/bin/sh\ntouch "{uv_called}"\nexit 0\n')
    fake_uv.chmod(0o755)

    base_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": "test-campaign",
        "E2B_API_KEY": "dummy",
        "OSWORLD_ROOT": str(osworld),
        "OSWORLD_TASKS_DIR": str(tasks),
        "OSWORLD_SERVICES_DIR": str(services),
        "VALIDATION_MANIFEST": str(manifest),
        "AGENT_MANIFEST": str(manifest),
        "TASK_ID": "001",
        "DOMAIN": "test",
        "PORT_BASE": "500",
        "OUTPUT": str(tmp_path / "worker.json"),
        "RAW_DIR": str(tmp_path / "raw"),
        "EVIDENCE_DIR": str(tmp_path / "evidence"),
        "MODEL_API_KEY": "dummy",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "test-model",
        "REQUIRE_NO_MODEL_COVERAGE": "0",
    }

    for relative in (
        "maintainer/validate.sh",
        "maintainer/validate_parallel.sh",
        "maintainer/run_path_task.sh",
        "runner/run_agent.sh",
        "runner/run_agent_parallel.sh",
    ):
        uv_called.unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", str(ROOT / relative)],
            cwd=ROOT,
            env=base_env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 2, (relative, result.stdout, result.stderr)
        assert "required service runtime file missing" in result.stderr, relative
        # No coordinator may touch the fleets (stop.py runs under uv) before
        # the run has been admitted.
        assert not uv_called.exists(), relative


def test_agent_coordinator_requires_explicit_m3_retry_policy(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_called = tmp_path / "uv-called"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(f'#!/bin/sh\ntouch "{uv_called}"\nexit 0\n')
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": "test-campaign",
        "E2B_API_KEY": "dummy",
        "MODEL_API_KEY": "dummy",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "test-model",
        "AGENT_KIND": "m3",
        "M3_THINKING_BUDGET": "2048",
    }
    env.pop("M3_MAX_LLM_RETRIES", None)

    result = subprocess.run(
        ["bash", str(ROOT / "runner" / "run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "M3_MAX_LLM_RETRIES must be explicit and non-negative" in result.stderr
    assert not uv_called.exists()


def test_preflight_rejects_busy_canonical_task_082_host_port(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "runner"))
    import preflight

    osworld, tasks, manifest, services = _minimal_inputs(tmp_path)
    (osworld / "e2b_relay.py").write_text("# test relay\n")
    (tasks / "task_001.py").unlink()
    (tasks / "task_082.py").write_text("TASK = {}\n")
    manifest.write_text(
        json.dumps(
            {
                "template": IMMUTABLE_GUEST,
                "osworld_commit": "a" * 40,
                "tasks": [{"id": "082", "domain": "test"}],
            }
        )
    )
    private_token = "test-private-token"
    runtime = {
        "websites": {
            "sandbox_id": "website-sandbox",
            "campaign_id": "test-campaign",
            "template": "fleet:11111111-2222-3333-4444-555555555555",
            "traffic_token": "website-traffic-token",
            "public_host_suffix": "127.0.0.1.nip.io:8090",
            "sites": {"mailhub": {}},
        },
        "gitlab": {
            "sandbox_id": "gitlab-sandbox",
            "campaign_id": "test-campaign",
            "template": "fleet:11111111-2222-3333-4444-555555555555",
            "traffic_token": "gitlab-traffic-token",
            "url": "http://gitlab.127.0.0.1.nip.io:8090",
            "private_token": private_token,
        },
    }
    runtime_path = services / ".runtime.json"
    runtime_path.write_text(json.dumps(runtime))
    runtime_path.chmod(0o600)
    token_path = services / ".gitlab-token"
    token_path.write_text(private_token)
    token_path.chmod(0o600)

    monkeypatch.setenv("GUEST_TEMPLATE", IMMUTABLE_GUEST)
    monkeypatch.setenv("OSWORLD_CAMPAIGN_ID", "test-campaign")
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_API_KEY", "judge-key")
    monkeypatch.setattr(
        preflight, "validate_release_lock", lambda _path: {"tasks_data": {}}
    )
    monkeypatch.setattr(preflight, "verify_task_snapshot", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight.py",
            "--osworld-root",
            str(osworld),
            "--tasks-dir",
            str(tasks),
            "--services-dir",
            str(services),
            "--manifest",
            str(manifest),
        ],
    )

    listener = socket.socket()
    try:
        try:
            listener.bind(("127.0.0.1", 3000))
            listener.listen()
        except OSError:
            # Another local process already holding the canonical port is an
            # equivalent preflight condition.
            listener.close()
        with pytest.raises(SystemExit, match="task 082 requires host port 3000"):
            preflight.main()
    finally:
        listener.close()


def _admissible_inputs(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "runner"))
    import preflight

    osworld, tasks, manifest, services = _minimal_inputs(tmp_path)
    (osworld / "e2b_relay.py").write_text("# test relay\n")
    runtime = {
        "websites": {
            "sandbox_id": "website-sandbox",
            "campaign_id": "test-campaign",
            "template": "fleet:11111111-2222-3333-4444-555555555555",
            "traffic_token": "website-traffic-token",
            "public_host_suffix": "127.0.0.1.nip.io:8090",
            "sites": {"mailhub": {}},
        },
        "gitlab": {
            "sandbox_id": "gitlab-sandbox",
            "campaign_id": "test-campaign",
            "template": "fleet:11111111-2222-3333-4444-555555555555",
            "traffic_token": "gitlab-traffic-token",
            "url": "http://gitlab.127.0.0.1.nip.io:8090",
            "private_token": "test-private-token",
        },
    }
    (services / ".runtime.json").write_text(json.dumps(runtime))
    (services / ".runtime.json").chmod(0o600)
    (services / ".gitlab-token").write_text("test-private-token")
    (services / ".gitlab-token").chmod(0o600)
    for name in list(os.environ):
        if name.startswith(("OSWORLD_EVAL_MODEL", "OSWORLD_USER_SIM")) or name in {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("GUEST_TEMPLATE", IMMUTABLE_GUEST)
    monkeypatch.setenv("OSWORLD_CAMPAIGN_ID", "test-campaign")
    monkeypatch.setattr(
        preflight,
        "validate_release_lock",
        lambda _path: {"tasks_data": {}, "code": {"commit": "a" * 40}},
    )
    monkeypatch.setattr(preflight, "verify_task_snapshot", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight.py",
            "--osworld-root",
            str(osworld),
            "--tasks-dir",
            str(tasks),
            "--services-dir",
            str(services),
            "--manifest",
            str(manifest),
        ],
    )
    return preflight


def test_preflight_requires_resolvable_judge_and_simulator_credentials(
    tmp_path, monkeypatch
):
    # Upstream resolves the judge key only inside the first evaluator call, and
    # llm_metrics converts that failure to a 0.0 score; without this check a
    # missing key surfaces hours later as an unretryable receipt on every task.
    preflight = _admissible_inputs(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="judge model.*OPENAI_API_KEY"):
        preflight.main()

    # Upstream's default key variable is OPENAI_API_KEY for EVERY provider
    # (model_client._build_config consults the caller default before its
    # per-provider table); a provider-named key alone is not enough.
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    with pytest.raises(SystemExit, match="judge model.*OPENAI_API_KEY"):
        preflight.main()
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_API_KEY_ENV", "JUDGE_KEY")
    with pytest.raises(SystemExit, match="judge model.*JUDGE_KEY"):
        preflight.main()
    monkeypatch.setenv("JUDGE_KEY", "judge-key")
    assert preflight.main() == 0
    monkeypatch.delenv("OSWORLD_EVAL_MODEL_API_KEY_ENV")
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_PROVIDER", "bedrock")  # no key needed
    assert preflight.main() == 0
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "judge-key")
    assert preflight.main() == 0

    # The simulator inherits judge settings it does not override (as upstream
    # does); only an explicit simulator key variable of its own is checked.
    monkeypatch.setenv("OSWORLD_USER_SIM_MODEL", "sim-model")
    assert preflight.main() == 0
    monkeypatch.setenv("OSWORLD_USER_SIM_API_KEY_ENV", "SIM_KEY")
    with pytest.raises(SystemExit, match="user simulator.*SIM_KEY"):
        preflight.main()
    monkeypatch.setenv("OSWORLD_USER_SIM_API_KEY", "sim-key")
    assert preflight.main() == 0


def test_preflight_skips_credentials_for_no_model_validation(tmp_path, monkeypatch):
    preflight = _admissible_inputs(tmp_path, monkeypatch)
    monkeypatch.setenv("OSWORLD_EVAL_MODEL_MODE", "stub")
    assert preflight.main() == 0


def _coordinator_inputs_past_preflight(tmp_path, *, agents_check_case: str):
    """Fake python3 passes preflight; fake uv answers the agents.py kind check
    with ``agents_check_case`` and records, then rejects, the lifetime check."""
    osworld, tasks, manifest, services = _minimal_inputs(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").write_text(
        """#!/bin/sh
case "$1" in
  */preflight.py) exit 0 ;;
  *) exec "$REAL_PYTHON" "$@" ;;
esac
"""
    )
    (fake_bin / "python3").chmod(0o755)
    (fake_bin / "uv").write_text(
        f"""#!/bin/sh
case "$*" in
  *agents.py*) {agents_check_case} ;;
  *--check-lifetime*) touch "$LIFETIME_CHECKED"; exit 1 ;;
  *) exit 0 ;;
esac
"""
    )
    (fake_bin / "uv").chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "LIFETIME_CHECKED": str(tmp_path / "lifetime-checked"),
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": "test-campaign",
        "E2B_API_KEY": "dummy",
        "MODEL_API_KEY": "dummy",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "test-model",
        "AGENT_KIND": "custom",
        "OSWORLD_ROOT": str(osworld),
        "OSWORLD_TASKS_DIR": str(tasks),
        "OSWORLD_SERVICES_DIR": str(services),
        "AGENT_MANIFEST": str(manifest),
        "RAW_DIR": str(tmp_path / "raw"),
        "OUTPUT": str(tmp_path / "out.json"),
    }


def _run_coordinator(env):
    return subprocess.run(
        ["bash", str(ROOT / "runner" / "run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_agent_coordinator_rejects_unknown_agent_kind_before_admission(tmp_path):
    # A typo in AGENT_KIND must cost nothing: reject it before the fleet
    # lifetime gate admits the run and any guest sandbox is created.
    env = _coordinator_inputs_past_preflight(
        tmp_path,
        agents_check_case="echo \"AGENT_KIND 'custom' is not defined\" >&2; exit 1",
    )
    result = _run_coordinator(env)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "AGENT_KIND" in result.stderr
    assert not Path(env["LIFETIME_CHECKED"]).exists()


def test_agent_coordinator_admits_agent_kinds_the_module_knows(tmp_path):
    env = _coordinator_inputs_past_preflight(tmp_path, agents_check_case="exit 0")
    result = _run_coordinator(env)
    assert result.returncode == 2, (
        result.stdout,
        result.stderr,
    )  # lifetime fake rejects
    assert Path(env["LIFETIME_CHECKED"]).exists()
    assert "AGENT_KIND" not in result.stderr
