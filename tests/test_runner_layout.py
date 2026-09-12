"""runner/ is the benchmark path a customer runs, and nothing else.

The maintainer validation ladder lives in maintainer/ and the one-off spikes
that shaped the port live in tools/spikes/. This test keeps the split from
eroding: a new file in runner/ must be part of running the agent.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RUNNER_FILES = {
    "__init__.py",
    "agent_runner.py",  # one task: DesktopEnv(e2b) + upstream run_single_example
    "agents.py",  # the file customers edit
    "aggregate_agent.py",  # campaign receipt + gate
    "evaluator_model_calls.py",  # judge/simulator call accounting
    "gated_data.py",  # gated task/asset download + verification
    "lazy_import.py",
    "model_coverage.py",  # optional no-model coverage gate used by the coordinator
    "preflight.py",
    "prepare_agent_run.py",
    "receipt_safety.py",
    "render_manifest.py",
    "requirements-e2b.txt",  # relay/provider pins layered onto the checkout env
    "retry_candidates.py",
    "run_agent.sh",  # one worker
    "run_agent_parallel.sh",  # the coordinator
    "setup.sh",  # pinned checkout + provider patches
    "worker_env.sh",
    "worker_lib.sh",
    "write_timeout_receipt.py",
}
MAINTAINER_FILES = {
    "README.md",
    "harness.py",
    "no_model.py",
    "profile_resources.sh",
    "readiness.py",
    "run_path_task.sh",
    "validate.sh",
    "validate_parallel.sh",
}
SPIKE_FILES = {
    "smoke.py",
    "snapshot_probe.py",
    "spike_audio.py",
    "spike_ingress.py",
}


def _files(directory: Path) -> set[str]:
    return {
        p.name for p in directory.iterdir() if p.is_file() and p.name != ".DS_Store"
    }


def test_runner_holds_only_the_benchmark_path():
    assert _files(ROOT / "runner") == RUNNER_FILES


def test_maintainer_validation_ladder_is_separate_and_marked():
    assert _files(ROOT / "maintainer") == MAINTAINER_FILES
    readme = (ROOT / "maintainer" / "README.md").read_text()
    assert "maintainer" in readme.lower() and "not required" in readme.lower()


def test_one_off_spikes_are_out_of_the_runner():
    assert _files(ROOT / "tools" / "spikes") == SPIKE_FILES
