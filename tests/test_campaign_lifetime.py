from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import fleetlib  # noqa: E402
from services.fleetlib import campaign_budget_seconds, require_campaign_lifetime  # noqa: E402

# Every knob the coordinator exports before the admission gate. The Python side
# deliberately has no defaults of its own so it can never drift from the shell.
ENV = {
    "PARALLEL_CONCURRENCY": "80",
    "RUN_TASK_082_CONCURRENT": "1",
    "AGENT_START_STAGGER_SECONDS": "0.25",
    "AGENT_TASK_TIMEOUT_SECONDS": "14400",
    "PROCESS_TERMINATION_GRACE_SECONDS": "10",
    "RELAY_STOP_REQUEST_TIMEOUT_SECONDS": "10",
    "RELAY_READY_TIMEOUT_SECONDS": "1050",
    "AGENT_WATCHDOG_POLL_SECONDS": "5",
}
TASKS = [{"id": f"{i:03}"} for i in range(1, 109)]


def test_budget_counts_only_the_waves_of_the_tasks_given():
    base = campaign_budget_seconds(TASKS, ENV)
    assert 2 * 14400 < base < 3 * 14400  # 108 tasks at 80 workers = 2 waves
    assert (
        campaign_budget_seconds(TASKS, {**ENV, "PARALLEL_CONCURRENCY": "1"})
        > 108 * 14400
    )
    assert (
        campaign_budget_seconds(TASKS, {**ENV, "RUN_TASK_082_CONCURRENT": "0"}) > base
    )
    # A retry wave is budgeted right before it runs, against the tasks that
    # actually failed, so a small failure set never costs a full 108-task wave.
    retry = campaign_budget_seconds(TASKS[:3], {**ENV, "PARALLEL_CONCURRENCY": "4"})
    assert retry < 2 * 14400


def test_readme_full_run_fits_the_default_fleet_lifetime():
    readme_env = {**ENV, "AGENT_TASK_TIMEOUT_SECONDS": "28800"}
    assert campaign_budget_seconds(TASKS, readme_env) <= fleetlib.SANDBOX_TIMEOUT_S


def test_budget_requires_every_knob_from_the_coordinator():
    for missing in ENV:
        with pytest.raises(KeyError, match=missing):
            campaign_budget_seconds(
                TASKS, {k: v for k, v in ENV.items() if k != missing}
            )
    with pytest.raises(ValueError):
        campaign_budget_seconds(TASKS, {**ENV, "AGENT_TASK_TIMEOUT_SECONDS": "0"})


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


def test_check_lifetime_cli_budgets_only_the_selected_task_ids(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": TASKS}))
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                name: {"sandbox_id": name, "campaign_id": "campaign"}
                for name in ("websites", "gitlab")
            }
        )
    )
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OSWORLD_CAMPAIGN_ID", "campaign")
    monkeypatch.setattr(fleetlib, "load_e2b_key", lambda: "key")
    monkeypatch.setattr(
        fleetlib.Sandbox,
        "get_info",
        lambda *, sandbox_id: SimpleNamespace(
            end_at=datetime.now(timezone.utc) + timedelta(hours=24)
        ),
    )
    common = ["--check-lifetime", str(manifest), "--runtime", str(runtime)]
    full = fleetlib.main(common)
    selected = fleetlib.main([*common, "--task-id", "001", "--task-id", "082"])
    assert selected == campaign_budget_seconds(TASKS[:1] + [TASKS[81]], ENV)
    assert selected < full
    with pytest.raises(ValueError, match="999"):
        fleetlib.main([*common, "--task-id", "999"])
