#!/usr/bin/env python3
"""No-agent OSWorld-V2 environment-path validation for the E2B provider.

Ported from hark's OSWorld 1.0 realkit/harness.py. Same shape: per task
reset/setup -> screenshot + accessibility observation -> one scripted pyautogui
action -> the task's own evaluator -> record. A PATH_PASS is strictly an
environment-path result (reset ok, observation ok, action executed, evaluator
RAN without an infrastructure error). It is NOT a benchmark task pass: the
evaluator's partial-credit score is recorded verbatim and never asserted.

V2 differences from hark (OSWorld 1.0):
  * Tasks are gated Python classes (``task_NNN.py``) loaded through the
    checkout's own ``task_loader.load_task_from_file`` -- not JSON configs.
  * ``DesktopEnv`` is instantiated with provider ``e2b`` and the V2 client
    password; ``env.evaluate()`` may return a float (legacy) or a dict whose
    ``score`` field is the canonical float.
  * Website/GitLab tasks import their controllers at module-load time, so the
    fleet env vars (WEBSITE_HOST_SUFFIX / GITLAB_URL / GITLAB_PRIVATE_TOKEN) and
    the local asset base (OSWORLD_FILE_BASE_URL) must be set before this runs;
    validate.sh exports them.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path


# task_loader.py is a top-level module in the pinned checkout root (not part of
# the installed ``desktop_env`` package). validate.sh launches us with cwd set to
# that root, but Python seeds sys.path[0] with this script's directory, so add the
# checkout root explicitly before importing it.
sys.path.insert(0, os.getcwd())
# Shared runner helpers (lazy_import) live in runner/; this ladder lives in maintainer/.
sys.path.insert(1, str(Path(__file__).resolve().parents[1] / "runner"))
from lazy_import import lazy_module  # noqa: E402

lazy_module("easyocr")  # torch only if an OCR metric actually runs
import task_loader  # noqa: E402  (checkout-local; cwd is the pinned checkout)
from desktop_env.desktop_env import DesktopEnv  # noqa: E402
from no_model import NoModelGuard, model_boundary_result  # noqa: E402
from readiness import wait_for_nonempty  # noqa: E402
from receipt_safety import classify_failure  # noqa: E402

NO_MODEL_GUARD = NoModelGuard()


class TaskTimeout(BaseException):
    pass


def _alarm_handler(_signum, _frame):
    raise TaskTimeout("per-task wall-clock limit exceeded")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def osworld_commit(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()


def classify(task_dir: Path, error: BaseException) -> tuple[str, str]:
    """One public cause bucket plus the full detail text for local use only.

    The bucket comes from receipt_safety.classify_failure -- the same classifier
    the agent runner uses -- reading this task's own artifact directory. The
    stage the failure happened at stays in the record's own ``stage`` field.
    """
    cause, _transport_ok, _evaluator_ran = classify_failure(
        task_dir, error, timeout_types=(TaskTimeout,)
    )
    return cause, f"{type(error).__name__}: {error}"


def _score_of(result) -> float:
    """V2 evaluate() returns a float (legacy) or a dict with a 'score' field."""
    if isinstance(result, dict):
        try:
            return float(result.get("score", 0.0))
        except (TypeError, ValueError):
            return 0.0
    return float(result)


def load_tasks(
    tasks_dir: Path, manifest_path: Path
) -> tuple[dict, list[tuple[dict, Path]]]:
    manifest = json.loads(manifest_path.read_text())
    tasks = []
    for item in manifest["tasks"]:
        path = tasks_dir / f"task_{item['id']}.py"
        if not path.is_file():
            raise FileNotFoundError(
                f"manifest task does not exist in gated snapshot: {path}"
            )
        tasks.append((item, path))
    return manifest, tasks


def run_task(
    env: DesktopEnv, item: dict, task_path: Path, raw_dir: Path, run_id: str
) -> dict:
    model_calls_before = NO_MODEL_GUARD.call_attempts
    task = task_loader.load_task_from_file(str(task_path))
    # Each task owns an artifact directory under the raw dir, so the shared
    # classifier sees this task's frames and no other task's.
    task_dir = raw_dir / f"{run_id}-task_{item['id']}"
    task_dir.mkdir(parents=True, exist_ok=True)
    phases = getattr(task, "get_phases", None)
    record = {
        "id": getattr(task, "id", item["id"]),
        "domain": item["domain"],
        "related_apps": list(getattr(task, "related_apps", []) or []),
        "task_class": task.__class__.__name__,
        "multiphase": callable(phases),
        "started_at": utc_now(),
        "stage": "reset",
        "path_status": None,
        "evaluator_ran": False,
        "score": None,
        "evaluation_mode": "no-model-stub",
        "external_model_calls": 0,
    }
    start = time.monotonic()
    try:
        observation = env.reset(task_config=task)
        state = env.provider.bridge.state()
        record["sandbox"] = {
            "id": state["sandbox_id"],
            "generation": state["generation"],
            "restricted_ingress": state["restricted_ingress"],
            "template": state["template"],
        }
        screenshot = observation.get("screenshot")
        accessibility = wait_for_nonempty(env.controller.get_accessibility_tree)
        if not screenshot:
            raise RuntimeError("OSWorld returned an empty screenshot observation")
        # Raw screenshot is evidence; it lives under the gitignored raw dir.
        (task_dir / "reset.png").write_bytes(screenshot)
        record["observation"] = {
            "screenshot_bytes": len(screenshot),
            "accessibility_characters": len(accessibility),
        }

        record["stage"] = "step"
        env.step(
            "import pyautogui; pyautogui.moveTo(300, 300); pyautogui.press('esc')",
            pause=1,
        )

        record["stage"] = "evaluate"
        result = env.evaluate()
        record["evaluator_ran"] = True
        record["score"] = _score_of(result)
        record["path_status"] = "PATH_PASS"
        record["stage"] = "complete"
    except BaseException as error:
        # classify() reads full text only to choose a public cause bucket. The
        # shareable receipt never copies exception text; the traceback remains
        # under the gitignored raw directory.
        boundary = model_boundary_result(record["stage"], error)
        if boundary is not None:
            record.update(boundary)
        else:
            cause, _full = classify(task_dir, error)
            record.update(
                path_status="PATH_FAIL",
                cause=cause,
                error_type=type(error).__name__,
            )
        import traceback

        (task_dir / "FAIL.trace.txt").write_text(traceback.format_exc())
    record["eval_model_call_attempts"] = (
        NO_MODEL_GUARD.call_attempts - model_calls_before
    )
    record["duration_seconds"] = round(time.monotonic() - start, 1)
    record["finished_at"] = utc_now()
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--osworld-root", type=Path, default=Path.cwd())
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument(
        "--task-timeout-seconds",
        type=int,
        default=int(os.environ.get("TASK_TIMEOUT_SECONDS", "900")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.environ.get("OSWORLD_EVAL_MODEL_MODE") != "stub":
        raise RuntimeError("harness requires OSWORLD_EVAL_MODEL_MODE=stub")
    NO_MODEL_GUARD.install()
    root = args.osworld_root.resolve()
    tasks_dir = args.tasks_dir.resolve()
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest, tasks = load_tasks(tasks_dir, args.manifest.resolve())

    expected_commit = manifest["osworld_commit"]
    actual_commit = osworld_commit(root)
    if actual_commit != expected_commit:
        raise RuntimeError(
            f"OSWorld-V2 commit mismatch: expected {expected_commit}, got {actual_commit}"
        )

    guest_template = os.environ["GUEST_TEMPLATE"]
    if manifest["template"] != guest_template:
        raise RuntimeError(
            f"template mismatch: manifest {manifest['template']} vs GUEST_TEMPLATE {guest_template}"
        )

    # Several upstream getters/evaluators resolve assets and repo-relative paths
    # from the checkout root. validate.sh also chdirs before launch; keep this for
    # direct harness use.
    os.chdir(root)

    run_id = str(uuid.uuid4())
    run = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": utc_now(),
        "purpose": "environment-path validation; not an agent benchmark score",
        "release": manifest.get("release"),
        "campaign_id": os.environ.get("OSWORLD_CAMPAIGN_ID"),
        "template": manifest["template"],
        "osworld_commit": actual_commit,
        "manifest": manifest,
        "host": {"python": platform.python_version(), "platform": platform.platform()},
        "dependencies": {
            name: importlib.metadata.version(name) for name in ("e2b", "aiohttp")
        },
        "bridge": {"template": guest_template},
        "records": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.output.with_suffix(".jsonl")
    jsonl_path.write_text("")

    signal.signal(signal.SIGALRM, _alarm_handler)
    env = None
    try:
        env = DesktopEnv(
            provider_name="e2b",
            os_type="Ubuntu",
            action_space="pyautogui",
            client_password="osworld-public-evaluation",
            require_a11y_tree=False,
            require_terminal=False,
            screen_size=(1920, 1080),
            headless=True,
            enable_proxy=False,
        )
        run["bridge"] = env.provider.bridge.state()
        sandbox_ids: set[str] = set()
        for item, task_path in tasks:
            signal.alarm(args.task_timeout_seconds)
            try:
                record = run_task(env, item, task_path, raw_dir, run_id)
            finally:
                signal.alarm(0)
            run["records"].append(record)
            if record.get("sandbox"):
                sandbox_id = record["sandbox"]["id"]
                record["sandbox"]["unique_in_run"] = sandbox_id not in sandbox_ids
                sandbox_ids.add(sandbox_id)
            with jsonl_path.open("a") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
            print(json.dumps(record, sort_keys=True), file=sys.stderr)
    finally:
        if env is not None:
            env.close()

    run["finished_at"] = utc_now()
    run["summary"] = {
        "tasks": len(run["records"]),
        "path_passes": sum(r["path_status"] == "PATH_PASS" for r in run["records"]),
        "model_boundary_passes": sum(
            r["path_status"] == "MODEL_BOUNDARY_PASS" for r in run["records"]
        ),
        "validated_tasks": sum(
            r["path_status"] in {"PATH_PASS", "MODEL_BOUNDARY_PASS"}
            for r in run["records"]
        ),
        "path_failures": sum(r["path_status"] == "PATH_FAIL" for r in run["records"]),
        "evaluator_ran_count": sum(
            bool(r.get("evaluator_ran")) for r in run["records"]
        ),
        "unique_sandboxes": len(
            {r.get("sandbox", {}).get("id") for r in run["records"] if r.get("sandbox")}
        ),
        "all_recorded_sandboxes_unique": all(
            r.get("sandbox", {}).get("unique_in_run", False) for r in run["records"]
        ),
        "score_min": min(
            (r["score"] for r in run["records"] if r.get("score") is not None),
            default=None,
        ),
        "score_max": max(
            (r["score"] for r in run["records"] if r.get("score") is not None),
            default=None,
        ),
    }
    args.output.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    print(json.dumps(run["summary"], sort_keys=True))
    return 0 if run["summary"]["path_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
