"""_count_steps must key on (phase_index, step_num), not step_num alone.

Upstream's multiphase loop (OSWorld-V2/lib_run_single.py) resets step_idx to 0
at the start of every phase and stamps phase_index on each row it writes, so a
four-phase task like 069 can have four rows all sharing step_num == 1. Rows
from a single-phase run carry no phase_index at all and must still count
correctly as if every row belonged to phase 1.

agent_runner.py cannot be imported as-is outside the pinned checkout: its
top-level imports (lib_run_single, task_loader, agents, desktop_env.desktop_env)
only resolve when cwd is the checkout. This stubs those modules on
sys.modules the same way tests/test_agent_module.py stubs mm_agents for
runner/agents.py, then imports agent_runner for real.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner"


@pytest.fixture
def agent_runner(monkeypatch):
    lib_run_single = types.ModuleType("lib_run_single")
    lib_run_single.run_single_example = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "lib_run_single", lib_run_single)

    task_loader = types.ModuleType("task_loader")
    task_loader.load_task_from_file = lambda path: {"instruction": "do the thing"}
    monkeypatch.setitem(sys.modules, "task_loader", task_loader)

    agents = types.ModuleType("agents")
    agents.AGENT_KINDS = {"prompt"}
    agents.agent_settings = lambda kind, **kw: {}
    agents.build_agent = lambda kind, **kw: object()
    monkeypatch.setitem(sys.modules, "agents", agents)

    desktop_env_desktop_env = types.ModuleType("desktop_env.desktop_env")
    desktop_env_desktop_env.DesktopEnv = type("DesktopEnv", (), {})
    desktop_env_pkg = types.ModuleType("desktop_env")
    desktop_env_pkg.desktop_env = desktop_env_desktop_env
    monkeypatch.setitem(sys.modules, "desktop_env", desktop_env_pkg)
    monkeypatch.setitem(sys.modules, "desktop_env.desktop_env", desktop_env_desktop_env)

    monkeypatch.syspath_prepend(str(RUNNER))
    monkeypatch.delitem(sys.modules, "agent_runner", raising=False)
    module = importlib.import_module("agent_runner")
    yield module
    sys.modules.pop("agent_runner", None)


def test_multiphase_steps_are_not_collapsed_across_phases(agent_runner, tmp_path):
    rows = [
        {"phase_index": 1, "step_num": 1, "action": "click"},
        {"phase_index": 1, "step_num": 2, "action": "type"},
        {"phase_index": 2, "step_num": 1, "action": "click"},
        {"phase_index": 2, "step_num": 2, "action": "ASK_USER"},
        {"phase_index": 2, "step_num": 2, "action": "click"},
    ]
    (tmp_path / "traj.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert agent_runner._count_steps(tmp_path) == 4


def test_single_phase_rows_without_phase_index_still_count(agent_runner, tmp_path):
    rows = [{"step_num": 1, "action": "click"}, {"step_num": 1, "action": "click"}, {"step_num": 2, "action": "x"}]
    (tmp_path / "traj.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert agent_runner._count_steps(tmp_path) == 2
