import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def model_env(**overrides):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("OSWORLD_EVAL_", "OSWORLD_USER_SIM_", "EVAL_MODEL", "USER_SIM_")
        )
    }
    result = subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            'source "$1"; python3 -c \'import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith("OSWORLD_USER_SIM_") or k == "OSWORLD_EVAL_MODEL_NAME"}))\'',
            "bash",
            str(ROOT / "runner/model_env.sh"),
        ],
        env={**env, **overrides},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_judge_override_does_not_override_task_simulator():
    assert model_env(
        EVAL_MODEL="judge", EVAL_MODEL_BASE_URL="https://judge.test/v1"
    ) == {"OSWORLD_EVAL_MODEL_NAME": "judge"}


def test_explicit_simulator_overrides_are_independent():
    env = model_env(
        USER_SIM_MODEL="simulator",
        USER_SIM_BASE_URL="https://sim.test/v1",
        OSWORLD_USER_SIM_PROVIDER="anthropic",
    )
    assert env["OSWORLD_USER_SIM_MODEL"] == "simulator"
    assert env["OSWORLD_USER_SIM_BASE_URL"] == "https://sim.test/v1"
    assert env["OSWORLD_USER_SIM_PROVIDER"] == "anthropic"
