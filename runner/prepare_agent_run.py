#!/usr/bin/env python3
"""Prepare fresh per-task receipt slots and emit a coordinator run nonce."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from retry_candidates import _scored

# Every artefact a fresh attempt at one task must not inherit. Result
# directories are left alone: they hold the prior evidence a resume keeps.
STALE_PATTERNS = (
    "task_{task_id}.json",
    "task_{task_id}.log",
    "task_{task_id}_before_retry_*.json",
    "task_{task_id}_retry_*.log",
)


class ResumeError(Exception):
    """A resume that cannot be trusted to continue the earlier run."""


def _task_ids(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text())
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("agent manifest must contain a tasks list")
    task_ids = [task.get("id") for task in tasks if isinstance(task, dict)]
    if len(task_ids) != len(tasks) or any(
        not isinstance(task_id, str) or not task_id for task_id in task_ids
    ):
        raise ValueError("agent manifest contains an invalid task id")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("agent manifest contains duplicate task ids")
    return task_ids


def _clear(worker_dir: Path, task_id: str) -> None:
    for pattern in STALE_PATTERNS:
        for stale in worker_dir.glob(pattern.format(task_id=task_id)):
            stale.unlink()


def _clear_all(worker_dir: Path, task_ids: list[str]) -> None:
    for task_id in task_ids:
        _clear(worker_dir, task_id)
    (worker_dir / "retries.json").unlink(missing_ok=True)


def prepare(manifest_path: Path, worker_dir: Path) -> str:
    task_ids = _task_ids(manifest_path)
    worker_dir.mkdir(parents=True, exist_ok=True)
    _clear_all(worker_dir, task_ids)
    return str(uuid.uuid4())


def resume(manifest_path: Path, worker_dir: Path) -> str:
    """Keep every already-scored task and continue the run that produced them.

    A scored rollout is the expensive artefact of the campaign, so a resume
    re-runs only what is still unscored and rejoins the earlier run's nonce
    rather than minting one the kept receipts would then fail the gate against.
    """
    task_ids = _task_ids(manifest_path)
    kept = [task_id for task_id in task_ids if _scored(worker_dir, task_id)]
    if not kept:
        # No task has scored yet -- the interrupt-then-resume common case.
        # There is no earlier run's nonce to rejoin, so this worker dir is
        # cleared exactly as a fresh `prepare()` would and started over.
        _clear_all(worker_dir, task_ids)
        nonce = str(uuid.uuid4())
        print(
            f"resuming {worker_dir}: no scored tasks yet; starting a fresh nonce",
            file=sys.stderr,
        )
        return nonce
    nonces: set[str] = set()
    for task_id in kept:
        try:
            record = json.loads((worker_dir / f"task_{task_id}.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue  # _scored tolerates a lost receipt; the result stands
        nonce = record.get("run_nonce") if isinstance(record, dict) else None
        if isinstance(nonce, str) and nonce:
            nonces.add(nonce)
    # Nothing is deleted until the resume is known to be sound: a rejected
    # resume must leave the operator exactly the directory they started with.
    if len(nonces) > 1:
        raise ResumeError(
            "kept receipts disagree on run_nonce: " + ", ".join(sorted(nonces))
        )
    if not nonces:
        raise ResumeError(
            f"no scored receipt under {worker_dir} carries a run_nonce; "
            "nothing to resume"
        )
    for task_id in task_ids:
        if task_id not in kept:
            _clear(worker_dir, task_id)
    nonce = nonces.pop()
    print(
        f"resuming {nonce}: skipping {len(kept)} scored tasks: {' '.join(kept)}",
        file=sys.stderr,
    )
    return nonce


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--worker-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.resume:
        try:
            nonce = resume(args.manifest, args.worker_dir)
        except ResumeError as error:
            print(error, file=sys.stderr)
            return 2
    else:
        nonce = prepare(args.manifest, args.worker_dir)
    print(nonce)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
