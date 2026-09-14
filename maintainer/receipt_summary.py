#!/usr/bin/env python3
"""Reduce a no-model ladder receipt to the committable summary.

The full aggregate receipt is thousands of lines of per-task detail; only its
identity, its gate fields and each task's path status belong in the repository
as evidence. The full receipt stays under the gitignored out/osworld-v2-raw/.
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
    "path_passes",
    "model_boundary_passes",
    "unique_sandboxes",
    "all_recorded_sandboxes_unique",
    "external_model_calls",
    "eval_model_call_attempts",
    "host_proxy_owned_for_campaign",
)


def _lookup(receipt: dict, field: str):
    """Return (found, value) for a field at the top level or in a section."""
    if field in receipt:
        return True, receipt[field]
    for section in SECTIONS:
        block = receipt.get(section)
        if isinstance(block, dict) and field in block:
            return True, block[field]
    return False, None


def summarize(receipt: dict, source_name: str) -> dict:
    summary: dict = {"kind": "no-model-ladder-summary", "source_receipt": source_name}
    for field in IDENTITY_FIELDS + GATE_FIELDS:
        found, value = _lookup(receipt, field)
        if found:
            summary[field] = value
    records = receipt.get("records")
    tasks = {
        record["id"]: record.get("path_status")
        for record in (records if isinstance(records, list) else [])
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    summary["task_count"] = len(tasks)
    summary["tasks"] = dict(sorted(tasks.items()))
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
