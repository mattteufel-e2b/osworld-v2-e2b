import ast
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def probe():
    # The probe's pure acceptance/configuration code needs no DesktopEnv.
    path = ROOT / "maintainer/browser_probe.py"
    source = ast.parse(path.read_text())
    source.body = [
        node
        for node in source.body
        if not isinstance(node, ast.ImportFrom)
        or node.module != "desktop_env.desktop_env"
    ]
    namespace = {"__name__": "browser_probe_test", "__file__": str(path)}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


def test_probe_checks_the_task_port(probe, monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSITE_HOST_SUFFIX", "127.0.0.1.nip.io:8090")
    result = probe["_origin_literal_check"](tmp_path / "absent.json")
    assert result["host_suffix_checked"]
    assert all(
        ":8090" in url for label, url in probe["ORIGINS"] if label != "gitlab-041"
    )
    monkeypatch.setenv("WEBSITE_HOST_SUFFIX", "127.0.0.1.nip.io:9999")
    with pytest.raises(RuntimeError, match="wrong fleet origins"):
        probe["_origin_literal_check"](tmp_path / "absent.json")


def test_missing_write_probes_cannot_pass(probe):
    checks = probe["_acceptance"]({"origins": []})
    assert checks["streamview.upload_roundtrip"] is False
    assert checks["gitlab.push_roundtrip"] is False


@pytest.mark.parametrize("push_fails", [False, True])
def test_git_push_verifies_blob_and_cleans_project(probe, monkeypatch, push_fails):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.test:8090")
    monkeypatch.setenv("GITLAB_PRIVATE_TOKEN", "host-admin-secret")
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(probe["requests"], "Session", lambda: session)
    project, token, blob, deleted = [MagicMock() for _ in range(4)]
    project.json.return_value = {"id": 7, "path_with_namespace": "root/probe"}
    token.json.return_value = {"token": "project-token"}
    blob.headers = {"X-Gitlab-Blob-Id": "blob-sha"}
    session.request.side_effect = (
        [project, token, deleted] if push_fails else [project, token, blob, deleted]
    )
    controller = MagicMock()
    if push_fails:
        controller.run_bash_script.side_effect = RuntimeError("guest disconnected")
        with pytest.raises(RuntimeError, match="guest disconnected"):
            probe["_gitlab_push"](controller)
    else:
        controller.run_bash_script.return_value = {
            "returncode": 0,
            "output": "blob-sha\n",
        }
        assert probe["_gitlab_push"](controller)
    script = controller.run_bash_script.call_args.args[0]
    assert "project-token" in script
    assert "host-admin-secret" not in script
    token_request = session.request.call_args_list[1].kwargs["json"]
    assert date.fromisoformat(token_request["expires_at"]) > datetime.now(UTC).date()
    assert session.request.call_args.args == (
        "DELETE",
        "https://gitlab.test:8090/api/v4/projects/7",
    )
