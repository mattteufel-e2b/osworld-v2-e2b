#!/usr/bin/env python3
"""Stop the exact OSWorld V2 service fleet owned by one campaign."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from e2b import Sandbox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleetlib as fl  # noqa: E402
from e2b_policy import require_campaign_id  # noqa: E402


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

    stopped: list[str] = []
    for section, value in sections.items():
        sandbox_id = value.get("sandbox_id")
        if not sandbox_id:
            continue
        try:
            Sandbox.connect(sandbox_id).kill()
            stopped.append(sandbox_id)
        except Exception as exc:  # noqa: BLE001
            fl.log(f"sandbox {sandbox_id} already gone or could not be reached: {exc}")
        fl.delete_runtime_section(section, sandbox_id)
    (fl.SERVICES_DIR / ".gitlab-token").unlink(missing_ok=True)
    fl.stop_host_proxy()
    return stopped


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
