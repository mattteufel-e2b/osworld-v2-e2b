#!/usr/bin/env python3
"""Reduce a no-model ladder, agent campaign or browser-probe receipt to a
committable summary.

The full aggregate receipt is thousands of lines of per-task detail; only its
identity, its gate fields and each task's path status (or score, for an agent
campaign) belong in the repository as evidence. A browser-probe record
(maintainer/browser_probe.py) keeps its acceptance booleans, per-origin probe
values and per-origin request tallies, and drops the verbatim request, event
and console lists behind them. The full receipt stays under the gitignored
out/osworld-v2-raw/.
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


# Browser-probe record shape (maintainer/browser_probe.py): top-level blocks
# dropped outright, per-origin lists reduced to their length, and the
# per-origin URL lists that are the probe's negative findings (expected empty)
# kept verbatim so an empty list stays checkable.
PROBE_DROP_TOP = ("bridge", "host", "origins", "cdp", "sanitized")
PROBE_ORIGIN_DROP = ("raw_probe", "probe_expression", "navigate")
PROBE_ORIGIN_COUNT = ("console_entries", "log_findings")
PROBE_NETWORK_COUNT = (
    "requests",
    "failures",
    "insecure_requests",
    "mixed_content_blocked",
)
PROBE_NETWORK_VERBATIM = (
    "total_requests",
    "schemes",
    "requests_truncated",
    "insecure_urls",
)


def _is_browser_probe(receipt: dict) -> bool:
    return isinstance(receipt.get("acceptance"), dict) and isinstance(
        receipt.get("origins"), list
    )


def _summarize_origin(origin: dict) -> dict:
    reduced = {
        key: value
        for key, value in origin.items()
        if key not in PROBE_ORIGIN_DROP
        and key not in PROBE_ORIGIN_COUNT
        and key not in ("network", "security")
    }
    for key in PROBE_ORIGIN_COUNT:
        if isinstance(origin.get(key), list):
            reduced[f"{key}_count"] = len(origin[key])
    security = origin.get("security")
    if isinstance(security, dict):
        reduced["security"] = {
            key: security[key] for key in ("source", "value") if key in security
        }
    network = origin.get("network")
    if isinstance(network, dict):
        reduced["network"] = {
            key: network[key] for key in PROBE_NETWORK_VERBATIM if key in network
        }
        for key in PROBE_NETWORK_COUNT:
            if isinstance(network.get(key), list):
                reduced["network"][f"{key}_count"] = len(network[key])
    return reduced


def _summarize_browser_probe(record: dict, source_name: str) -> dict:
    summary: dict = {"kind": "browser-probe-summary", "source_receipt": source_name}
    summary.update(
        {key: value for key, value in record.items() if key not in PROBE_DROP_TOP}
    )
    cdp = record.get("cdp")
    if isinstance(cdp, dict):
        browser = cdp.get("browser_version")
        summary["cdp"] = {
            "browser": browser.get("Browser") if isinstance(browser, dict) else None,
            "enable_errors": cdp.get("enable_errors"),
            "event_count": cdp.get("event_count"),
        }
    summary["origins"] = [
        _summarize_origin(origin)
        for origin in record.get("origins", [])
        if isinstance(origin, dict)
    ]
    summary["reduction_note"] = (
        "Committed reduction of the raw probe record under out/osworld-v2-raw/: "
        "every acceptance value and per-origin probe value is verbatim; the "
        "per-origin request, security-event and console lists are reduced to "
        "counts and per-scheme tallies (insecure_urls and blocked_urls stay "
        "verbatim, so an empty list remains the checkable negative result)."
    )
    return summary


def summarize(receipt: dict, source_name: str) -> dict:
    if _is_browser_probe(receipt):
        return _summarize_browser_probe(receipt, source_name)
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
