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


def list_campaign_sandbox_ids(campaign: str) -> set[str]:
    """List every live service sandbox carrying this campaign's metadata."""
    paginator = Sandbox.list(
        query=SandboxQuery(
            metadata={
                "workload": "osworld-v2-services",
                "campaign_id": campaign,
            }
        ),
        limit=100,
    )
    sandbox_ids: set[str] = set()
    while paginator.has_next:
        for sandbox in paginator.next_items():
            metadata = getattr(sandbox, "metadata", None) or {}
            sandbox_id = getattr(sandbox, "sandbox_id", None)
            if (
                sandbox_id
                and metadata.get("workload") == "osworld-v2-services"
                and metadata.get("campaign_id") == campaign
            ):
                sandbox_ids.add(sandbox_id)
    return sandbox_ids


def stop_campaign(campaign: str) -> list[str]:
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
        listed_ids = list_campaign_sandbox_ids(campaign)
    except Exception as exc:  # noqa: BLE001
        fl.stop_host_proxy()
        raise RuntimeError(
            f"could not enumerate service sandboxes for campaign {campaign}: {exc}"
        ) from exc

    targets = runtime_ids | listed_ids
    ambiguous_runtime_ids: set[str] = set()
    for sandbox_id in sorted(targets):
        try:
            Sandbox.kill(sandbox_id)
        except Exception as exc:  # noqa: BLE001
            fl.log(f"could not stop campaign sandbox {sandbox_id}: {exc}")
            if sandbox_id in runtime_ids and sandbox_id not in listed_ids:
                ambiguous_runtime_ids.add(sandbox_id)

    try:
        remaining = list_campaign_sandbox_ids(campaign)
    except Exception as exc:  # noqa: BLE001
        fl.stop_host_proxy()
        raise RuntimeError(
            f"could not verify service teardown for campaign {campaign}: {exc}"
        ) from exc

    fl.stop_host_proxy()
    if remaining or ambiguous_runtime_ids:
        unresolved = remaining | ambiguous_runtime_ids
        raise RuntimeError(
            "service teardown incomplete; preserving recovery state for: "
            + ", ".join(sorted(unresolved))
        )

    for section, value in sections.items():
        sandbox_id = value.get("sandbox_id")
        if sandbox_id:
            fl.delete_runtime_section(section, sandbox_id)
    (fl.SERVICES_DIR / ".gitlab-token").unlink(missing_ok=True)
    return sorted(targets)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-id", default=os.environ.get("OSWORLD_CAMPAIGN_ID"))
    args = parser.parse_args()
    campaign = require_campaign_id(args.campaign_id)
    fl.load_e2b_key()
    stopped = stop_campaign(campaign)
    print(f"stopped_service_sandboxes={len(stopped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
