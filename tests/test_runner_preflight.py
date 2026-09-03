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
        "EVAL_MODEL_BASE_URL": "https://example.test/v1",
        "EVAL_MODEL": "test-evaluator",
        "REQUIRE_NO_MODEL_COVERAGE": "0",
    }

    for relative in (
        "runner/validate.sh",
        "runner/validate_parallel.sh",
        "runner/run_path_task.sh",
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
        if relative in {
            "runner/validate.sh",
            "runner/validate_parallel.sh",
            "runner/run_agent_parallel.sh",
        }:
            assert uv_called.exists(), relative
        else:
            assert not uv_called.exists(), relative


def test_agent_coordinator_returns_failure_when_early_teardown_fails(tmp_path):
    osworld, tasks, manifest, services = _minimal_inputs(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 97\n")
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GUEST_TEMPLATE": IMMUTABLE_GUEST,
        "OSWORLD_CAMPAIGN_ID": "test-campaign",
        "E2B_API_KEY": "dummy",
        "OSWORLD_ROOT": str(osworld),
        "OSWORLD_TASKS_DIR": str(tasks),
        "OSWORLD_SERVICES_DIR": str(services),
        "AGENT_MANIFEST": str(manifest),
        "RAW_DIR": str(tmp_path / "raw"),
        "OUTPUT": str(tmp_path / "receipt.json"),
        "MODEL_API_KEY": "dummy",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "test-model",
        "EVAL_MODEL_BASE_URL": "https://example.test/v1",
        "EVAL_MODEL": "test-evaluator",
        "REQUIRE_NO_MODEL_COVERAGE": "0",
    }

    result = subprocess.run(
        ["bash", str(ROOT / "runner" / "run_agent_parallel.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "required service runtime file missing" in result.stderr
    assert "service fleet cleanup failed" in result.stderr


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
        "EVAL_MODEL_BASE_URL": "https://example.test/v1",
        "EVAL_MODEL": "test-evaluator",
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
