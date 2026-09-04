from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from no_model import (  # noqa: E402
    NoModelEvaluationBoundary,
    NoModelGuard,
    model_boundary_result,
)


def test_no_model_guard_returns_deterministic_negative_without_backend_creation():
    def forbidden_backend(*_args, **_kwargs):
        raise AssertionError("backend creation must be unreachable")

    model_client = SimpleNamespace(
        generate_text=lambda *_args, **_kwargs: "live",
        generate_chat=lambda *_args, **_kwargs: "live",
        create_backend=forbidden_backend,
    )
    llm_metrics = SimpleNamespace(generate_text=lambda *_args, **_kwargs: "live")
    guard = NoModelGuard()

    guard.install(model_client=model_client, llm_metrics=llm_metrics)

    with pytest.raises(NoModelEvaluationBoundary):
        model_client.generate_text("secret prompt")
    with pytest.raises(NoModelEvaluationBoundary):
        model_client.generate_chat([{"content": "secret"}])
    with pytest.raises(NoModelEvaluationBoundary):
        llm_metrics.generate_text("secret prompt")
    with pytest.raises(NoModelEvaluationBoundary):
        model_client.create_backend(object())
    assert guard.call_attempts == 4


def test_model_boundary_result_is_explicit_and_only_applies_during_evaluation():
    boundary = NoModelEvaluationBoundary("model disabled")
    wrapped = RuntimeError("evaluator wrapped the model boundary")
    wrapped.__cause__ = boundary

    assert model_boundary_result("evaluate", wrapped) == {
        "path_status": "MODEL_BOUNDARY_PASS",
        "stage": "model-boundary",
        "evaluator_ran": True,
        "score": 0.0,
        "error_type": "RuntimeError",
    }
    assert model_boundary_result("reset", wrapped) is None
    assert model_boundary_result("evaluate", RuntimeError("unrelated")) is None


def test_unrelated_crash_with_boundary_context_is_not_a_pass():
    # An evaluator catches the boundary broadly, then crashes on its own bug.
    # The boundary rides along only in __context__ — that is a PATH_FAIL.
    try:
        try:
            raise NoModelEvaluationBoundary("disabled")
        except Exception:
            raise TypeError("broken fallback scoring")
    except TypeError as error:
        assert model_boundary_result("evaluate", error) is None


def test_explicitly_chained_boundary_is_a_pass():
    try:
        try:
            raise NoModelEvaluationBoundary("disabled")
        except NoModelEvaluationBoundary as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as error:
        result = model_boundary_result("evaluate", error)
        assert result is not None and result["path_status"] == "MODEL_BOUNDARY_PASS"


def _validate_parallel_heredoc() -> str:
    # The file has an earlier `python3 - "$MANIFEST" <<'PY'` heredoc (task id
    # listing); anchor on the full aggregation-gate invocation's argv so this
    # matches that heredoc specifically, not the first "$MANIFEST" heredoc.
    source = (ROOT / "runner" / "validate_parallel.sh").read_text()
    match = re.search(
        r"python3 - \"\$MANIFEST\" \"\$RAW_DIR/workers\" \"\$OUTPUT\" "
        r"\"\$PARALLEL_CONCURRENCY\" <<'PY'.*?\n(.*?)\nPY\n",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_validation_gate_rejects_missing_counter(tmp_path):
    # A record missing external_model_calls must invalidate the run — it must
    # never default to -1 and cancel another record's genuine call count.
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "template": "t:1", "osworld_commit": "a" * 40,
        "tasks": [{"id": "001", "domain": "d"}, {"id": "002", "domain": "d"}],
    }))
    workers = tmp_path / "workers"
    workers.mkdir()
    base = {
        "path_status": "PATH_PASS", "evaluator_ran": True,
        "eval_model_call_attempts": 0,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
    }
    missing_counter = {**base, "id": "001", "sandbox": {"id": "sb-1"}}
    leaky = {**base, "id": "002", "sandbox": {"id": "sb-2"}, "external_model_calls": 1}
    (workers / "task_001.json").write_text(json.dumps({"records": [missing_counter]}))
    (workers / "task_002.json").write_text(json.dumps({"records": [leaky]}))
    output = tmp_path / "receipt.json"
    proc = subprocess.run(
        [sys.executable, "-", str(manifest), str(workers), str(output), "2"],
        input=_validate_parallel_heredoc(), capture_output=True, text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "VALIDATION GATE: FAIL" in proc.stdout
