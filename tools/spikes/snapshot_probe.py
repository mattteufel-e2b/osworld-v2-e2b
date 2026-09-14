#!/usr/bin/env python3
"""Live probe: OSWorld-V2 save_state/revert_to_snapshot mapped to E2B snapshots.

Requires a running relay (relay/relay.py, vendored into the
checkout as e2b_relay.py). Verifies that a snapshot saved mid-run captures the
exact guest state (marker file present after revert), that a plain reset returns
to the immutable template base state (marker absent), and that restricted ingress
holds throughout. Ported from hark's runner/snapshot_probe.py; the only V2 change
is the guest /execute call shape (list command, matching the V2 control server).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

CONTROL = "http://127.0.0.1:14999"
GUEST = "http://127.0.0.1:15000"
MARKER = "/home/user/snapshot-probe-marker"
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "out" / "osworld-v2-evidence" / "snapshot-probe.json"


def call(url, payload=None, timeout=600):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    method = "POST" if data is not None else "GET"
    with urlopen(
        Request(url, data=data, method=method, headers=headers), timeout=timeout
    ) as r:
        return json.load(r)


def execute(command):
    # V2 control server expects a list command; run it through a login shell.
    return call(
        f"{GUEST}/execute", {"command": ["bash", "-lc", command], "shell": False}
    )


def marker_exists():
    result = execute(f"test -f {MARKER} && echo present || echo absent")
    return (result.get("output") or "").strip() == "present"


def main() -> int:
    report = {"tested_at": datetime.now(UTC).isoformat(), "checks": {}}

    state = call(f"{CONTROL}/health")
    report["template"] = state["template"]
    report["sandbox_a"] = state["sandbox_id"]
    report["checks"]["initial guest from template"] = state["source"] == "template"
    report["checks"]["restricted ingress at start"] = (
        state["restricted_ingress"] is True
    )

    execute(f"echo mid-task-state > {MARKER}")
    report["checks"]["marker written in sandbox A"] = marker_exists()

    saved = call(f"{CONTROL}/save", {"name": "probe_state"})
    report["snapshot_id"] = saved["snapshot_id"]
    report["checks"]["snapshot saved"] = bool(saved["snapshot_id"])
    report["checks"]["sandbox A resumed after snapshot"] = marker_exists()

    reverted = call(f"{CONTROL}/reset", {"snapshot": "probe_state"})
    report["sandbox_b"] = reverted["sandbox_id"]
    report["checks"]["revert created a new sandbox"] = (
        reverted["sandbox_id"] != state["sandbox_id"]
    )
    report["checks"]["revert source is the snapshot"] = (
        reverted["source"] == f"snapshot:{saved['snapshot_id']}"
    )
    report["checks"]["restricted ingress preserved on revert"] = (
        reverted["restricted_ingress"] is True
    )
    report["checks"]["marker survives snapshot revert"] = marker_exists()

    base = call(f"{CONTROL}/reset", {"snapshot": "init_state"})
    report["sandbox_c"] = base["sandbox_id"]
    report["checks"]["unsaved name falls back to template"] = (
        base["source"] == "template"
    )
    report["checks"]["restricted ingress preserved on template reset"] = (
        base["restricted_ingress"] is True
    )
    report["checks"]["marker absent after template reset"] = not marker_exists()

    report["all_checks_passed"] = all(report["checks"].values())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["all_checks_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
