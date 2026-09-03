from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


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
    fake_uv.write_text(f'#!/bin/sh\ntouch "{uv_called}"\nexit 97\n')
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
        "MODEL_API_KEY": "dummy",
        "MODEL_BASE_URL": "https://example.test/v1",
        "MODEL": "test-model",
        "EVAL_MODEL_BASE_URL": "https://example.test/v1",
        "EVAL_MODEL": "test-evaluator",
    }

    for relative in (
        "runner/validate.sh",
        "runner/validate_parallel.sh",
        "runner/run_path_task.sh",
        "runner/run_agent.sh",
        "runner/run_agent_parallel.sh",
    ):
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
        assert not uv_called.exists(), relative
