from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
spec = importlib.util.spec_from_file_location(
    "stop_under_test", ROOT / "services" / "stop.py"
)
stop = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(stop)


def _runtime() -> dict:
    return {
        "websites": {"campaign_id": "campaign", "sandbox_id": "website-id"},
        "gitlab": {"campaign_id": "campaign", "sandbox_id": "gitlab-id"},
    }


def test_stop_preserves_recovery_state_when_a_campaign_sandbox_remains(tmp_path):
    token = tmp_path / ".gitlab-token"
    token.write_text("secret")
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value=_runtime()),
        patch.object(
            stop,
            "list_campaign_targets",
            side_effect=[
                {"website-id": stop.fl.WORKLOAD, "gitlab-id": stop.fl.WORKLOAD},
                {"gitlab-id": stop.fl.WORKLOAD},
            ],
        ),
        patch.object(stop.Sandbox, "kill") as kill,
        patch.object(stop.fl, "delete_runtime_section") as delete,
    ):

        def kill_by_id(sandbox_id: str):
            if sandbox_id == "gitlab-id":
                raise RuntimeError("transient")
            return True

        kill.side_effect = kill_by_id
        with pytest.raises(RuntimeError, match="gitlab-id"):
            stop.stop_campaign("campaign")

    assert token.is_file()
    delete.assert_not_called()


def test_stop_reconciles_orphaned_campaign_sandboxes_before_deleting_state(tmp_path):
    token = tmp_path / ".gitlab-token"
    token.write_text("secret")
    killed: list[str] = []

    def kill(sandbox_id: str):
        killed.append(sandbox_id)
        return True

    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value=_runtime()),
        patch.object(
            stop,
            "list_campaign_targets",
            side_effect=[
                {
                    "website-id": stop.fl.WORKLOAD,
                    "gitlab-id": stop.fl.WORKLOAD,
                    "orphan-id": stop.fl.WORKLOAD,
                },
                {},
            ],
        ),
        patch.object(stop.Sandbox, "kill", side_effect=kill),
        patch.object(stop.fl, "delete_runtime_section") as delete,
    ):
        stopped = stop.stop_campaign("campaign")

    assert stopped == ["gitlab-id", "orphan-id", "website-id"]
    assert not token.exists()
    assert delete.call_count == 2


# --- campaign-scoped guest sweep -------------------------------------------


class _FakePaginator:
    """A single-page paginator mimicking e2b's Sandbox.list() result."""

    def __init__(self, items):
        self._items = list(items)
        self._served = False

    @property
    def has_next(self):
        return not self._served

    def next_items(self):
        self._served = True
        return self._items


class _FakeSandboxRecord:
    def __init__(self, sandbox_id, metadata):
        self.sandbox_id = sandbox_id
        self.metadata = metadata


class FakeSandbox:
    """Fake `e2b.Sandbox` applying the metadata query filter server-side, the
    way the real API does, so tests exercise both the query and the
    client-side re-check in stop.py."""

    def __init__(self, inventory):
        self.inventory = list(inventory)
        self.killed: list[str] = []
        self.kill_raises: set[str] = set()

    def list(self, query=None, limit=100):
        wanted = dict(getattr(query, "metadata", None) or {})
        matched = [
            record
            for record in self.inventory
            if record.metadata
            and all(record.metadata.get(k) == v for k, v in wanted.items())
        ]
        return _FakePaginator(matched)

    def kill(self, sandbox_id):
        if sandbox_id in self.kill_raises:
            raise RuntimeError(f"could not kill {sandbox_id}")
        self.killed.append(sandbox_id)
        self.inventory = [r for r in self.inventory if r.sandbox_id != sandbox_id]
        return True


def _mixed_inventory():
    return [
        _FakeSandboxRecord(
            "A1", {"workload": "osworld", "campaign_id": "A", "generation": "1"}
        ),
        _FakeSandboxRecord(
            "A2", {"workload": "osworld", "campaign_id": "A", "generation": "2"}
        ),
        _FakeSandboxRecord(
            "A-web",
            {
                "workload": "osworld-v2-services",
                "section": "websites",
                "campaign_id": "A",
            },
        ),
        _FakeSandboxRecord("B1", {"workload": "osworld", "campaign_id": "B"}),
        _FakeSandboxRecord("no-metadata", {}),
        _FakeSandboxRecord("no-campaign", {"workload": "osworld"}),
        _FakeSandboxRecord("other-workload", {"workload": "other", "campaign_id": "A"}),
    ]


def test_sweep_kills_only_exact_campaign_matches_across_both_workloads(tmp_path):
    fake = FakeSandbox(_mixed_inventory())
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value={}),
        patch.object(stop.fl, "stop_host_proxy"),
        patch.object(stop.fl, "delete_runtime_section") as delete,
        patch.object(stop, "Sandbox", fake),
    ):
        stopped = stop.stop_campaign("A")

    assert set(stopped) == {"A1", "A2", "A-web"}
    assert set(fake.killed) == {"A1", "A2", "A-web"}
    assert not (
        {"B1", "no-metadata", "no-campaign", "other-workload"} & set(fake.killed)
    )
    delete.assert_not_called()


def test_dry_run_lists_targets_and_kills_nothing(tmp_path, monkeypatch, capsys):
    fake = FakeSandbox(_mixed_inventory())
    monkeypatch.setattr(sys, "argv", ["stop.py", "--campaign-id", "A", "--dry-run"])
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value={}),
        patch.object(stop.fl, "stop_host_proxy"),
        patch.object(stop.fl, "load_e2b_key"),
        patch.object(stop.fl, "delete_runtime_section") as delete,
        patch.object(stop, "Sandbox", fake),
    ):
        exit_code = stop.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert fake.killed == []
    delete.assert_not_called()
    for sandbox_id, workload in (
        ("A1", "osworld"),
        ("A2", "osworld"),
        ("A-web", "osworld-v2-services"),
    ):
        assert f"campaign=A workload={workload} sandbox={sandbox_id}" in out
    assert "sandbox=B1" not in out
    assert "would_stop_service_sandboxes=1 would_stop_guest_sandboxes=2" in out


def test_empty_campaign_id_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("OSWORLD_CAMPAIGN_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["stop.py", "--campaign-id", ""])
    fake = FakeSandbox(_mixed_inventory())
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "load_e2b_key") as load_key,
        patch.object(stop, "Sandbox", fake),
    ):
        with pytest.raises(ValueError, match="OSWORLD_CAMPAIGN_ID"):
            stop.main()

    load_key.assert_not_called()
    assert fake.killed == []


def test_incomplete_sweep_exits_non_zero(tmp_path, monkeypatch):
    fake = FakeSandbox(_mixed_inventory())
    fake.kill_raises = {"A-web"}
    monkeypatch.setattr(sys, "argv", ["stop.py", "--campaign-id", "A"])
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value={}),
        patch.object(stop.fl, "stop_host_proxy"),
        patch.object(stop.fl, "load_e2b_key"),
        patch.object(stop, "Sandbox", fake),
    ):
        with pytest.raises(RuntimeError, match="A-web"):
            stop.main()

    assert set(fake.killed) == {"A1", "A2"}


class _RaisingListSandbox:
    """A Sandbox stand-in whose list() always fails, to exercise the
    listing-error path without ever reaching a kill call."""

    def list(self, query=None, limit=100):
        raise RuntimeError("listing failed")

    def kill(self, sandbox_id):
        raise AssertionError("kill must not be called on the listing-error path")


def test_dry_run_listing_error_never_stops_host_proxy(tmp_path):
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value={}),
        patch.object(stop.fl, "stop_host_proxy") as stop_proxy,
        patch.object(stop, "Sandbox", _RaisingListSandbox()),
    ):
        with pytest.raises(RuntimeError, match="could not enumerate"):
            stop.stop_campaign("A", dry_run=True)

    stop_proxy.assert_not_called()


def test_real_run_listing_error_stops_host_proxy(tmp_path):
    with (
        patch.object(stop.fl, "SERVICES_DIR", tmp_path),
        patch.object(stop.fl, "read_runtime", return_value={}),
        patch.object(stop.fl, "stop_host_proxy") as stop_proxy,
        patch.object(stop, "Sandbox", _RaisingListSandbox()),
    ):
        with pytest.raises(RuntimeError, match="could not enumerate"):
            stop.stop_campaign("A", dry_run=False)

    stop_proxy.assert_called_once()


class _PermissiveListSandbox:
    """A Sandbox stand-in whose list() ignores the query entirely and always
    returns the full inventory, so only stop.py's client-side re-check can
    be responsible for narrowing the result."""

    def __init__(self, inventory):
        self.inventory = list(inventory)

    def list(self, query=None, limit=100):
        return _FakePaginator(self.inventory)


def test_list_campaign_sandbox_ids_rechecks_client_side_against_a_permissive_server():
    fake = _PermissiveListSandbox(_mixed_inventory())
    with patch.object(stop, "Sandbox", fake):
        result = stop.list_campaign_sandbox_ids("A", "osworld")

    assert result == {"A1", "A2"}
