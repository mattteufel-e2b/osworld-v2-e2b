#!/usr/bin/env python3
"""Reduce a no-model ladder or agent campaign receipt to a committable summary.

The full aggregate receipt is thousands of lines of per-task detail; only its
identity, its gate fields and each task's path status (or score, for an agent
campaign) belong in the repository as evidence. The full receipt stays under
the gitignored out/osworld-v2-raw/.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


# Sections the aggregate receipt nests these fields under, in lookup order.
SECTIONS = ("execution", "verification", "summary")
IDENTITY_FIELDS = (
    "template",
    "osworld_commit",
    "release",
    "campaign_id",
    "started_at",
    "finished_at",
)
GATE_FIELDS = (
    "evaluation_mode",
    "path_passes",
    "path_failures",
    "model_boundary_passes",
    "unique_sandboxes",
    "all_recorded_sandboxes_unique",
    "external_model_calls",
    "eval_model_call_attempts",
    "host_proxy_owned_for_campaign",
    "attested_records",
    "scored_tasks",
    "mean_score",
    "binary_accuracy",
    "retried_task_ids",
    "implicit_retries",
    "model_usage",
)
PER_TASK_FIELDS = ("path_status", "score", "error_cause", "steps_taken")


def _lookup(receipt: dict, field: str):
    """Return (found, value) for a field at the top level or in a section."""
    if field in receipt:
        return True, receipt[field]
    for section in SECTIONS:
        block = receipt.get(section)
        if isinstance(block, dict) and field in block:
            return True, block[field]
    return False, None


def _is_no_model(receipt: dict) -> bool:
    """Decide whether this receipt came from a no-model ladder run.

    Detects two shapes: an evaluation_mode key in the receipt's own summary
    block, or every record stamped evaluation_mode == "no-model-stub" (the
    stamp is written by maintainer/harness.py and runner/model_coverage.py).
    An agent campaign receipt (runner/aggregate_agent.py) carries neither
    marker. Older no-model receipts that predate the per-record stamp also
    carry neither marker, so they fall through and are summarized with the
    agent-run shape instead.
    """
    summary_block = receipt.get("summary")
    if isinstance(summary_block, dict) and "evaluation_mode" in summary_block:
        return True
    records = receipt.get("records")
    return (
        isinstance(records, list)
        and bool(records)
        and all(
            isinstance(r, dict) and r.get("evaluation_mode") == "no-model-stub"
            for r in records
        )
    )


def summarize(receipt: dict, source_name: str) -> dict:
    # One flag drives both the receipt's overall kind and each record's
    # shape below, so a no-model record can never end up with a dict shape
    # (or vice versa) just because it happens to carry a "score" key.
    no_model = _is_no_model(receipt)
    kind = "no-model-ladder-summary" if no_model else "agent-run-summary"
    summary: dict = {"kind": kind, "source_receipt": source_name}
    for field in IDENTITY_FIELDS + GATE_FIELDS:
        found, value = _lookup(receipt, field)
        if found:
            summary[field] = value
    records = receipt.get("records")
    tasks: dict = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            continue
        if no_model:
            tasks[record["id"]] = record.get("path_status")
        else:
            tasks[record["id"]] = {
                key: record[key] for key in PER_TASK_FIELDS if key in record
            }
    summary["task_count"] = len(tasks)
    # Not "tasks": the source receipt uses that key for an integer count.
    summary["task_statuses"] = dict(sorted(tasks.items()))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads(args.input.read_text())
    summary = summarize(receipt, args.input.name)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
