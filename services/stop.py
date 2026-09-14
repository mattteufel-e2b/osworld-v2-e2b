#!/usr/bin/env python3
"""Stop the exact OSWorld V2 service fleet owned by one campaign."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from e2b import Sandbox, SandboxQuery

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleetlib as fl  # noqa: E402
from e2b_policy import require_campaign_id  # noqa: E402

# Guests (agent_runner / provider/bridge.py's GuestManager._create) carry this
# workload; service fleets carry fl.WORKLOAD. The sweep below is restricted to
# exactly these two workloads and always exact-matches campaign_id -- never a
# wider or prefix query, never a fallback to "all sandboxes".
GUEST_WORKLOAD = "osworld"


def list_campaign_sandbox_ids(campaign: str, workload: str) -> set[str]:
    """List every live sandbox of `workload` carrying this campaign's metadata."""
    paginator = Sandbox.list(
        query=SandboxQuery(metadata={"workload": workload, "campaign_id": campaign}),
        limit=100,
    )
    sandbox_ids: set[str] = set()
    while paginator.has_next:
        for sandbox in paginator.next_items():
            metadata = getattr(sandbox, "metadata", None) or {}
            sandbox_id = getattr(sandbox, "sandbox_id", None)
            if (
                sandbox_id
                and metadata.get("workload") == workload
                and metadata.get("campaign_id") == campaign
            ):
                sandbox_ids.add(sandbox_id)
    return sandbox_ids


def list_campaign_targets(campaign: str) -> dict[str, str]:
    """Return {sandbox_id: workload} for every live sandbox -- service fleet or
    guest -- exactly matching this campaign, across both known workloads."""
    targets: dict[str, str] = {}
    for workload in (fl.WORKLOAD, GUEST_WORKLOAD):
        for sandbox_id in list_campaign_sandbox_ids(campaign, workload):
            targets[sandbox_id] = workload
    return targets


def stop_campaign(campaign: str, dry_run: bool = False) -> list[str]:
    runtime = fl.read_runtime()
    sections = {
        section: value
        for section, value in runtime.items()
        if section in {"websites", "gitlab"} and isinstance(value, dict)
    }
    mismatched = [
        section
        for section, value in sections.items()
        if value.get("campaign_id") != campaign
    ]
    if mismatched:
        raise RuntimeError(
            "refusing to stop service runtime owned by another campaign: "
            + ", ".join(sorted(mismatched))
        )

    runtime_ids = {
        value["sandbox_id"]
        for value in sections.values()
        if isinstance(value.get("sandbox_id"), str) and value["sandbox_id"]
    }
    try:
        listed_targets = list_campaign_targets(campaign)
    except Exception as exc:  # noqa: BLE001
        fl.stop_host_proxy()
        raise RuntimeError(
            f"could not enumerate campaign sandboxes for campaign {campaign}: {exc}"
        ) from exc

    targets = dict(listed_targets)
    for sandbox_id in runtime_ids:
        targets.setdefault(sandbox_id, fl.WORKLOAD)

    for sandbox_id in sorted(targets):
        print(
            f"campaign={campaign} workload={targets[sandbox_id]} sandbox={sandbox_id}"
        )

    service_count = sum(1 for workload in targets.values() if workload == fl.WORKLOAD)
    guest_count = len(targets) - service_count

    if dry_run:
        print(
            f"stopped_service_sandboxes={service_count} "
            f"stopped_guest_sandboxes={guest_count}"
        )
        return sorted(targets)

    ambiguous_runtime_ids: set[str] = set()
    for sandbox_id in sorted(targets):
        try:
            Sandbox.kill(sandbox_id)
        except Exception as exc:  # noqa: BLE001
            fl.log(f"could not stop campaign sandbox {sandbox_id}: {exc}")
            if sandbox_id in runtime_ids and sandbox_id not in listed_targets:
                ambiguous_runtime_ids.add(sandbox_id)

    try:
        remaining = set(list_campaign_targets(campaign))
    except Exception as exc:  # noqa: BLE001
        fl.stop_host_proxy()
        raise RuntimeError(
            f"could not verify campaign teardown for campaign {campaign}: {exc}"
        ) from exc

    fl.stop_host_proxy()
    if remaining or ambiguous_runtime_ids:
        unresolved = remaining | ambiguous_runtime_ids
        raise RuntimeError(
            "campaign teardown incomplete; preserving recovery state for: "
            + ", ".join(sorted(unresolved))
        )

    for section, value in sections.items():
        sandbox_id = value.get("sandbox_id")
        if sandbox_id:
            fl.delete_runtime_section(section, sandbox_id)
    (fl.SERVICES_DIR / ".gitlab-token").unlink(missing_ok=True)
    print(
        f"stopped_service_sandboxes={service_count} "
        f"stopped_guest_sandboxes={guest_count}"
    )
    return sorted(targets)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-id", default=os.environ.get("OSWORLD_CAMPAIGN_ID"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list this campaign's targets without stopping anything",
    )
    args = parser.parse_args()
    campaign = require_campaign_id(args.campaign_id)
    fl.load_e2b_key()
    stop_campaign(campaign, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
