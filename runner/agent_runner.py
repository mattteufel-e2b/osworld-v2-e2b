#!/usr/bin/env python3
"""Single-task REAL-agent rollout for the OSWorld-V2 -> E2B conversion (rung 6).

One invocation drives exactly one manifest task end to end: it builds a
``DesktopEnv`` on the ``e2b`` provider (the provider owns its in-process bridge
and picks free loopback ports), runs the pinned checkout's own
``lib_run_single.run_single_example`` agent loop with the agent selected by
``--agent-kind``, and records a redacted per-task receipt on every exit path:
success, any exception, the ``--deadline-seconds`` alarm (exit 124) and
SIGTERM (exit 143). Trajectories, screenshots and model IO stay under the
gitignored raw dir; the receipt carries ids, path booleans, timings and the
evaluator's partial-credit score only -- never task or evaluator text.

Agent construction and generation defaults live in ``agents.py`` (the file to
edit for a custom agent); this runner only asks it for an agent. Credentials
reach the agent through ``MODEL_BASE_URL`` / ``MODEL_API_KEY``. No secrets are
logged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


sys.path.insert(0, os.getcwd())
from lazy_import import lazy_module  # noqa: E402

# easyocr (and torch behind it) serves one OCR metric no release task uses;
# load it on first use instead of in every one of 80 workers at startup.
lazy_module("easyocr")
import lib_run_single  # noqa: E402
import task_loader  # noqa: E402  (checkout-local; cwd is the pinned checkout)
from agents import AGENT_KINDS, agent_settings, build_agent  # noqa: E402
from desktop_env.desktop_env import DesktopEnv  # noqa: E402
from evaluator_model_calls import EvaluatorModelCallTracker  # noqa: E402
from receipt_safety import (  # noqa: E402
    atomic_write_json,
    base_receipt,
    classify_failure,
    public_error,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class AgentTaskTimeout(BaseException):
    """--deadline-seconds elapsed; raised from SIGALRM in the main thread."""


class AgentInterrupted(BaseException):
    """SIGTERM received; the coordinator or operator cancelled this rollout."""


def _install_signal_handlers(deadline_seconds: int | None) -> None:
    def on_alarm(_signum, _frame):
        raise AgentTaskTimeout(f"deadline of {deadline_seconds}s exceeded")

    def on_term(_signum, _frame):
        raise AgentInterrupted("SIGTERM")

    signal.signal(signal.SIGTERM, on_term)
    if deadline_seconds:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(deadline_seconds)


class _Args:
    """Minimal args object for lib_run_single (only the attributes it reads).

    ``result_dir`` is the base directory lib_results_logger.log_task_completion
    appends its summary/results.json bookkeeping under; point it at this task's
    raw result dir so the post-evaluate logging call succeeds.
    """

    def __init__(self, sleep_after_execution: float, result_dir: str):
        self.sleep_after_execution = sleep_after_execution
        self.checkpoint_eval_mode = "off"
        self.checkpoint_steps = ""
        self.trace_guest = False
        self.result_dir = result_dir


def _read_score(result_dir: Path) -> tuple[float | None, bool | None]:
    """Read the final float score and a best-effort judge-component flag from the
    checkout's own result artifacts. Never returns evaluator text."""
    score = None
    judge_used = None
    result_txt = result_dir / "result.txt"
    if result_txt.exists():
        try:
            score = float(result_txt.read_text().strip())
            if not math.isfinite(score):
                score = None
        except ValueError:
            score = None
    # A dict-returning evaluator writes result.json; inspect KEYS only (not
    # values) for a judge/model component so no evaluator text is surfaced.
    result_json = result_dir / "result.json"
    if result_json.exists():
        try:
            data = json.loads(result_json.read_text())
            judge_used = _keys_mention_judge(data)
        except Exception:
            judge_used = None
    phase_json = result_dir / "phase_results.json"
    if judge_used is None and phase_json.exists():
        judge_used = False  # multi-phase result present but no dict-eval judge signal
    return score, judge_used


def _keys_mention_judge(obj, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()
            if any(tok in k for tok in ("judge", "vlm", "llm", "model")):
                return True
            if _keys_mention_judge(value, depth + 1):
                return True
    elif isinstance(obj, list):
        for item in obj[:20]:
            if _keys_mention_judge(item, depth + 1):
                return True
    return False


def _count_steps(result_dir: Path) -> int:
    traj = result_dir / "traj.jsonl"
    if not traj.exists():
        return 0
    steps = set()
    with traj.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("action") in (None, "ASK_USER"):
                continue
            if "step_num" in row:
                steps.add((row.get("phase_index", 1), row["step_num"]))
    return len(steps)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument(
        "--result-dir", type=Path, required=True, help="raw trajectory dir (gitignored)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="per-task receipt json (gitignored raw)",
    )
    parser.add_argument("--agent-kind", choices=sorted(AGENT_KINDS), default="prompt")
    parser.add_argument("--model", default="openai/gpt-4o")
    parser.add_argument("--max-steps", type=int, default=75)
    # Generation settings mirror upstream run.py; unset means the agent kind's
    # upstream default (see agents.py).
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--max-trajectory-length", type=int, default=None)
    parser.add_argument("--sleep-after-execution", type=float, default=3.0)
    # Upstream's --enable_recording: the guest records the screen with ffmpeg
    # for the whole rollout and the mp4 lands in the raw result dir. Off by
    # default there and here.
    parser.add_argument("--enable-recording", action="store_true")
    parser.add_argument("--client-password", default="osworld-public-evaluation")
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=None,
        help="wall-clock limit for this rollout; on expiry the receipt records task-timeout and the process exits 124",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_nonce = os.environ.get("OSWORLD_RUN_NONCE")
    if not run_nonce:
        raise RuntimeError("OSWORLD_RUN_NONCE is required")
    evaluator_model_calls = EvaluatorModelCallTracker()
    evaluator_model_calls.install()
    exit_code = 0
    result_dir = args.result_dir.resolve()
    result_dir.mkdir(parents=True, exist_ok=True)

    receipt = base_receipt(
        task_id=args.task_id,
        domain=args.domain,
        agent_kind=args.agent_kind,
        model=args.model,
        max_steps=args.max_steps,
        recording_enabled=args.enable_recording,
    )
    receipt.update(
        multiphase=False,
        agent_settings=None,
        started_at=utc_now(),
        transport_ok=False,
        evaluator_ran=False,
        path_status=None,
        steps_taken=0,
        score=None,
        judge_used=None,
        eval_model_call_attempts=0,
        eval_model_successes=0,
        sandbox_id=None,
        sandbox_generation=None,
        wall_clock_s=None,
    )
    env = None
    multiphase = False
    scores: list[float] = []
    start = time.monotonic()
    # Armed as the last statement before the try, with the receipt already
    # seeded above: a task file or an agent constructor that hangs or raises is
    # inside the deadline and inside the try below, not ahead of them.
    _install_signal_handlers(args.deadline_seconds)
    try:
        task_path = (args.tasks_dir / f"task_{args.task_id}.py").resolve()
        example = task_loader.load_task_from_file(str(task_path))
        instruction = example["instruction"]
        phases = getattr(example, "get_phases", None)
        multiphase = callable(phases) and bool(phases())
        receipt["multiphase"] = multiphase
        settings = agent_settings(
            args.agent_kind,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            max_trajectory_length=args.max_trajectory_length,
        )
        receipt["agent_settings"] = settings
        agent = build_agent(
            args.agent_kind,
            model=args.model,
            settings=settings,
            client_password=args.client_password,
        )
        env = DesktopEnv(
            provider_name="e2b",
            os_type="Ubuntu",
            action_space="pyautogui",
            client_password=args.client_password,
            require_a11y_tree=False,
            require_terminal=False,
            screen_size=(1920, 1080),
            headless=True,
            enable_proxy=False,
            force_disable_recording=not args.enable_recording,
        )
        lib_run_single.run_single_example(
            agent,
            env,
            example,
            args.max_steps,
            instruction,
            _Args(args.sleep_after_execution, str(result_dir)),
            str(result_dir),
            scores,
        )
        receipt["path_status"] = "OK"
    except BaseException as error:  # noqa: BLE001 -- record and classify, never leak text
        # Classification globs the result dir; a deadline firing here would
        # escape this handler uncaught and cost the receipt.
        signal.alarm(0)
        if isinstance(error, AgentTaskTimeout):
            exit_code = 124
        elif isinstance(error, AgentInterrupted):
            exit_code = 143
        cause, transport_ok, evaluator_ran = classify_failure(
            result_dir,
            error,
            timeout_types=(AgentTaskTimeout,),
            interrupt_types=(AgentInterrupted,),
        )
        receipt["path_status"] = "ERROR"
        receipt["error_cause"] = cause
        receipt.update(public_error(error))
        receipt["transport_ok"] = transport_ok
        receipt["evaluator_ran"] = evaluator_ran
        import traceback

        (result_dir / "FAIL.trace.txt").write_text(traceback.format_exc())
    finally:
        # Teardown is uninterruptible: bridge.state() and env.close() can take
        # minutes, and a second SIGTERM landing here would abort the close
        # mid-flight -- leaking the guest sandbox and losing the receipt, which
        # is written after this block.
        signal.alarm(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Capture the sandbox this rollout ran against (one reset == one sandbox)
        # before tearing anything down.
        bridge = getattr(getattr(env, "provider", None), "bridge", None)
        if bridge is not None:
            try:
                state = bridge.state()
                receipt["sandbox_id"] = state.get("sandbox_id")
                receipt["sandbox_generation"] = state.get("generation")
                receipt["restricted_ingress"] = state.get("restricted_ingress")
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] could not read bridge state: {exc}", file=sys.stderr)
        if env is not None:
            try:
                env.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] env.close warning: {exc}", file=sys.stderr)

    receipt["wall_clock_s"] = round(time.monotonic() - start, 1)
    receipt["finished_at"] = utc_now()
    receipt["steps_taken"] = _count_steps(result_dir)
    score, judge_used = _read_score(result_dir)
    if score is not None:
        receipt["score"] = score
    if receipt["path_status"] == "OK":
        receipt["transport_ok"] = True
        receipt["evaluator_ran"] = (result_dir / "result.txt").exists() or multiphase
    receipt["judge_used"] = judge_used
    receipt["eval_model_call_attempts"] = evaluator_model_calls.call_attempts
    receipt["eval_model_successes"] = evaluator_model_calls.successes
    receipt["user_sim_call_attempts"] = evaluator_model_calls.user_sim_call_attempts
    receipt["user_sim_successes"] = evaluator_model_calls.user_sim_successes
    if receipt["path_status"] == "OK" and (
        evaluator_model_calls.call_attempts != evaluator_model_calls.successes
        or evaluator_model_calls.user_sim_call_attempts
        != evaluator_model_calls.user_sim_successes
    ):
        # Upstream metrics may convert a transport exception to zero. Keep the
        # original score artifact, but do not attest or resample that rollout.
        # Only an otherwise-clean rollout is reclassified here: a rollout that
        # already failed keeps the cause classify_failure gave it.
        receipt["path_status"] = "ERROR"
        receipt["error_cause"] = "evaluator-or-agent"

    atomic_write_json(args.output, receipt)
    # Redacted one-liner to stderr (safe: ids + booleans + score only).
    print(
        json.dumps(
            {
                k: receipt[k]
                for k in (
                    "id",
                    "domain",
                    "path_status",
                    "transport_ok",
                    "evaluator_ran",
                    "steps_taken",
                    "score",
                    "sandbox_id",
                    "wall_clock_s",
                )
            }
        ),
        file=sys.stderr,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
