"""GitLab startup must provision a usable runner without exposing its token."""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location(
        "gitlab_runner_launcher", ROOT / "services/gitlab/launch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(data=None, status=200):
    return SimpleNamespace(ok=status < 400, status_code=status, json=lambda: data)


def sandbox(outputs):
    return SimpleNamespace(
        traffic_access_token="traffic-secret",
        get_host=lambda port: f"{port}-fleet.example.test",
        commands=SimpleNamespace(
            run=Mock(
                side_effect=[SimpleNamespace(stdout=s, exit_code=0) for s in outputs]
            )
        ),
    )


def test_fresh_runner_uses_internal_url_actual_network_and_private_environment(
    launcher, monkeypatch
):
    sbx = sandbox(["NONE\n", "campaign_default\n", "", ""])
    api = Mock(
        side_effect=[
            response({"id": 7, "token": "glrt-registration-secret"}, 201),
            response(
                {
                    "id": 7,
                    "status": "online",
                    "online": True,
                    "paused": False,
                    "run_untagged": True,
                }
            ),
        ]
    )
    monkeypatch.setattr(launcher.requests, "request", api)

    result = launcher.ensure_runner(sbx, "pat-secret")

    assert result == {"id": 7, "status": "online", "online": True}
    registration = sbx.commands.run.call_args_list[2]
    command = registration.args[0]
    assert "--url http://fleet_fanout:8929" in command
    assert "--clone-url http://fleet_fanout:8929" in command
    assert "--docker-network-mode campaign_default" in command
    assert "--executor docker" in command
    assert registration.kwargs["envs"] == {"RUNNER_TOKEN": "glrt-registration-secret"}
    for call in sbx.commands.run.call_args_list:
        assert "glrt-registration-secret" not in call.args[0]
        assert "pat-secret" not in call.args[0]
    assert api.call_args_list[0].args == (
        "POST",
        "https://8929-fleet.example.test/api/v4/user/runners",
    )
    assert api.call_args_list[0].kwargs["json"]["run_untagged"] is True
    assert api.call_args_list[0].kwargs["json"]["runner_type"] == "instance_type"
    assert "secret" not in json.dumps(result)


def test_existing_runner_is_reused_without_creating_or_registering_another(
    launcher, monkeypatch
):
    sbx = sandbox(["7\n"])
    api = Mock(
        return_value=response(
            {
                "id": 7,
                "status": "online",
                "online": True,
                "paused": False,
                "run_untagged": True,
            }
        )
    )
    monkeypatch.setattr(launcher.requests, "request", api)
    assert launcher.ensure_runner(sbx, "pat-secret")["id"] == 7
    assert sbx.commands.run.call_count == 1
    assert api.call_args.args[0] == "GET"


def test_failed_registration_deletes_only_new_runner_and_restores_config(
    launcher, monkeypatch
):
    sbx = sandbox(["NONE\n", "campaign_default\n"])
    sbx.commands.run.side_effect = [
        SimpleNamespace(stdout="NONE\n", exit_code=0),
        SimpleNamespace(stdout="campaign_default\n", exit_code=0),
        RuntimeError("SDK error containing glrt-registration-secret"),
        SimpleNamespace(stdout="", exit_code=0),
    ]
    api = Mock(
        side_effect=[
            response({"id": 7, "token": "glrt-registration-secret"}, 201),
            response(status=204),
        ]
    )
    monkeypatch.setattr(launcher.requests, "request", api)
    with pytest.raises(RuntimeError, match="runner command failed") as caught:
        launcher.ensure_runner(sbx, "pat-secret")
    assert "secret" not in str(caught.value)
    assert api.call_args.args == (
        "DELETE",
        "https://8929-fleet.example.test/api/v4/runners/7",
    )
    assert "config.toml.osworld-backup" in sbx.commands.run.call_args.args[0]


def test_offline_existing_runner_fails_gate_without_deleting_it(launcher, monkeypatch):
    sbx = sandbox(["7\n"])
    api = Mock(return_value=response({"id": 7, "status": "offline", "online": False}))
    monkeypatch.setattr(launcher.requests, "request", api)
    monkeypatch.setattr(launcher.time, "monotonic", Mock(side_effect=[0, 0, 121]))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="runner"):
        launcher.ensure_runner(sbx, "pat-secret")
    assert all(call.args[0] == "GET" for call in api.call_args_list)


def test_pages_fanout_preserves_only_validated_single_label_hosts(launcher):
    writes = {}
    sbx = SimpleNamespace(
        files=SimpleNamespace(write=lambda path, body: writes.update({path: body}))
    )
    launcher.write_fanout(sbx)
    conf = writes[f"{launcher.REPO_DIR}/fanout.conf"]
    assert "map $http_x_osworld_pages_host $gitlab_upstream_host" in conf
    assert "default gitlab.127.0.0.1.nip.io;" in conf
    assert r"\.gitlab\.127\.0\.0\.1\.nip\.io$" in conf
    assert "proxy_set_header Host $gitlab_upstream_host;" in conf
    expression = re.search(r'"~(\^.+\$)"', conf).group(1)
    for name in ("root.gitlab.127.0.0.1.nip.io", "site-abcdef.gitlab.127.0.0.1.nip.io"):
        assert re.fullmatch(expression, name)
    for name in (
        "gitlab.127.0.0.1.nip.io",
        "nested.root.gitlab.127.0.0.1.nip.io",
        "-root.gitlab.127.0.0.1.nip.io",
        "root-.gitlab.127.0.0.1.nip.io",
        "root.gitlab.127.0.0.1.nip.io.attacker.test",
        "root.gitlab.127.0.0.1.nip.io:8090",
    ):
        assert not re.fullmatch(expression, name)


def test_ambiguous_network_fails_before_creating_a_runner(launcher, monkeypatch):
    sbx = sandbox(["NONE\n", "first_network\nsecond_network\n"])
    api = Mock()
    monkeypatch.setattr(launcher.requests, "request", api)
    with pytest.raises(RuntimeError, match="one Compose network"):
        launcher.ensure_runner(sbx, "pat-secret")
    api.assert_not_called()
