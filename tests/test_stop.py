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
            "list_campaign_sandbox_ids",
            side_effect=[{"website-id", "gitlab-id"}, {"gitlab-id"}],
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
            "list_campaign_sandbox_ids",
            side_effect=[{"website-id", "gitlab-id", "orphan-id"}, set()],
        ),
        patch.object(stop.Sandbox, "kill", side_effect=kill),
        patch.object(stop.fl, "delete_runtime_section") as delete,
    ):
        stopped = stop.stop_campaign("campaign")

    assert stopped == ["gitlab-id", "orphan-id", "website-id"]
    assert not token.exists()
    assert delete.call_count == 2
