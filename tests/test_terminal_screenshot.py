"""Exercise the installed upstream runner without live sandboxes or model calls."""

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ["bash", str(ROOT / "runner/setup.sh")]


@pytest.fixture(scope="module")
def checkout(tmp_path_factory):
    source = ROOT / "OSWorld-V2"
    if not (source / ".git").exists():
        pytest.skip("pinned upstream checkout not installed")
    dest = tmp_path_factory.mktemp("terminal-screenshot") / "upstream"
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(source), str(dest)], check=True
    )
    subprocess.run([*SETUP, str(dest)], check=True, capture_output=True)
    return dest


@pytest.fixture
def runner(checkout, monkeypatch):
    # Only isolate external timeout/logging dependencies and guest telemetry;
    # execute the actual installed task loop, trajectory writer and evaluator flow.
    monkeypatch.setitem(sys.modules, "wrapt_timeout_decorator", ModuleType("timeout"))
    result_logger = ModuleType("lib_results_logger")
    result_logger.log_task_completion = Mock()
    monkeypatch.setitem(sys.modules, "lib_results_logger", result_logger)
    spec = importlib.util.spec_from_file_location(
        "terminal_screenshot_runner", checkout / "lib_run_single.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "GuestMemoryTracer", Mock())
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(sleep=lambda _: None, perf_counter=time.perf_counter),
    )
    return module


class PhasedTask(dict):
    def get_phases(self):
        return [{"instruction": "test", "evaluate": lambda env: env.evaluate()}]


@pytest.mark.parametrize("task_type", [dict, PhasedTask], ids=["single", "multiphase"])
@pytest.mark.parametrize("action", ["DONE", "FAIL"])
@pytest.mark.parametrize(
    "screenshot", [None, b"final-frame"], ids=["missing", "present"]
)
def test_terminal_observation(runner, tmp_path, task_type, action, screenshot):
    agent = Mock()
    agent.predict.return_value = ("finished", [action])
    env = Mock(user_simulator=None)
    env._get_obs.return_value = {"screenshot": b"initial-frame"}
    env.step.return_value = ({"screenshot": screenshot}, 0, True, {"done": True})
    env.evaluate.return_value = 0.5
    args = SimpleNamespace(sleep_after_execution=0, checkpoint_eval_mode="off")
    scores = []

    def run():
        runner.run_single_example(
            agent, env, task_type(id="test"), 1, "test", args, str(tmp_path), scores
        )

    if screenshot is None:
        with pytest.raises(TypeError):
            run()
        env.evaluate.assert_not_called()
        assert scores == []
        assert not (tmp_path / "result.txt").exists()
    else:
        run()
        env.evaluate.assert_called_once_with()
        assert scores == [0.5]
        assert float((tmp_path / "result.txt").read_text()) == 0.5
        row = json.loads((tmp_path / "traj.jsonl").read_text())
        assert row["action"] == action
        assert (tmp_path / row["screenshot_file"]).read_bytes() == screenshot


def test_setup_migrates_only_exact_legacy_screenshot_patch(checkout):
    target = checkout / "lib_run_single.py"
    pristine = subprocess.check_output(
        ["git", "-C", str(checkout), "show", "HEAD:lib_run_single.py"]
    )
    target.write_bytes(pristine)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "apply",
            str(ROOT / "tests/fixtures/legacy_terminal_screenshot.patch"),
        ],
        check=True,
    )
    legacy = target.read_bytes()
    try:
        assert (
            subprocess.run(
                [*SETUP, "--verify", str(checkout)], capture_output=True
            ).returncode
            != 0
        )
        changed = legacy + b"\n# customer experiment\n"
        target.write_bytes(changed)
        failed = subprocess.run([*SETUP, str(checkout)], capture_output=True)
        assert failed.returncode != 0
        assert target.read_bytes() == changed
        target.write_bytes(legacy)
        subprocess.run([*SETUP, str(checkout)], check=True, capture_output=True)
        assert target.read_bytes() == pristine
        subprocess.run([*SETUP, str(checkout)], check=True, capture_output=True)
        assert target.read_bytes() == pristine
        subprocess.run(
            [*SETUP, "--verify", str(checkout)], check=True, capture_output=True
        )
    finally:
        target.write_bytes(pristine)
