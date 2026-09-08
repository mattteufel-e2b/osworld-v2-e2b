from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.fleetlib import campaign_budget_seconds, require_campaign_lifetime  # noqa: E402


def test_budget_includes_all_waves_and_possible_retries():
    tasks = [{"id": f"{i:03}"} for i in range(1, 109)]
    base = campaign_budget_seconds(tasks, {})
    assert base > 2 * 14400
    assert campaign_budget_seconds(tasks, {"PARALLEL_CONCURRENCY": "1"}) > 108 * 14400
    assert (
        campaign_budget_seconds(tasks, {"AGENT_RETRY_ATTEMPTS": "1"})
        > base + 27 * 14400
    )
    assert campaign_budget_seconds(tasks, {"RUN_TASK_082_CONCURRENT": "0"}) > base


def test_both_fleets_must_outlast_campaign_without_extending_them(monkeypatch):
    runtime = {
        name: {"sandbox_id": name, "campaign_id": "campaign"}
        for name in ("websites", "gitlab")
    }
    now = datetime.now(timezone.utc)
    expirations = {
        "websites": now + timedelta(hours=24),
        "gitlab": now + timedelta(hours=1),
    }
    calls = []

    def get_info(*, sandbox_id):
        calls.append(sandbox_id)
        return SimpleNamespace(end_at=expirations[sandbox_id])

    monkeypatch.setenv("OSWORLD_CAMPAIGN_ID", "campaign")
    monkeypatch.setattr("services.fleetlib.Sandbox.get_info", get_info)
    require_campaign_lifetime(runtime, 1800)
    assert calls == ["websites", "gitlab"]
    with pytest.raises(RuntimeError, match="gitlab.*remaining"):
        require_campaign_lifetime(runtime, 7200)
    runtime["websites"]["campaign_id"] = "other"
    with pytest.raises(RuntimeError, match="different campaign"):
        require_campaign_lifetime(runtime, 1800)
