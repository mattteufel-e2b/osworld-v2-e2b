from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

V2_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "hostmap_proxy_under_test", V2_ROOT / "services" / "hostmap_proxy.py"
)
hostmap_proxy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hostmap_proxy)


def test_upstream_open_retries_transient_transport_failure():
    response = object()
    with (
        patch.object(
            hostmap_proxy._URL_OPENER,
            "open",
            side_effect=[urllib.error.URLError(BrokenPipeError()), response],
        ) as open_request,
        patch.object(hostmap_proxy.time, "sleep") as sleep,
    ):
        actual = hostmap_proxy._open_upstream(object())

    assert actual is response
    assert open_request.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_upstream_redirects_are_returned_to_the_client_for_cookie_fidelity():
    redirect_handler = hostmap_proxy._NoRedirect()

    assert redirect_handler.redirect_request(None, None, 302, "Found", {}, "https://next") is None


def test_absolute_site_urls_are_rewritten_to_the_incoming_proxy_authority():
    body = b'{"setNewPasswordUrl":"http://overleaf.127.0.0.1.nip.io/user/activate?token=x"}'

    rewritten = hostmap_proxy._rewrite_absolute_site_urls(
        body,
        "overleaf.127.0.0.1.nip.io",
        "overleaf.127.0.0.1.nip.io:8090",
    )
    location = hostmap_proxy._rewrite_location(
        "https://overleaf.127.0.0.1.nip.io/project",
        "overleaf.127.0.0.1.nip.io",
        "overleaf.127.0.0.1.nip.io:8090",
    )

    assert b"http://overleaf.127.0.0.1.nip.io:8090/user/activate" in rewritten
    assert location == "http://overleaf.127.0.0.1.nip.io:8090/project"


def test_guest_routes_work_without_traffic_tokens_in_runtime_file(tmp_path):
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {
                "websites": {
                    "host_suffix": "127.0.0.1.nip.io",
                    "sites": {
                        "mailhub": {
                            "ingress_host": "13001-websites.e2b.app",
                            "port": 13001,
                        }
                    },
                },
                "gitlab": {
                    "host": "gitlab.127.0.0.1.nip.io",
                    "ingress_host": "8929-gitlab.e2b.app",
                    "port": 8929,
                },
            }
        )
    )

    with patch.object(hostmap_proxy, "RUNTIME_FILE", runtime_file):
        rules = hostmap_proxy._load_rules()

    assert rules["mailhub.127.0.0.1.nip.io"] == {
        "ingress_host": "13001-websites.e2b.app",
        "traffic_token": None,
        "sandbox_id": None,
        "port": 13001,
    }
    assert rules["gitlab.127.0.0.1.nip.io"] == {
        "ingress_host": "8929-gitlab.e2b.app",
        "traffic_token": None,
        "sandbox_id": None,
        "port": 8929,
    }


def test_large_body_can_use_authenticated_e2b_command_bridge():
    class Files:
        envelope = None

        @classmethod
        def write(cls, _path, value):
            cls.envelope = json.loads(value)

        @staticmethod
        def remove(_path):
            pass

    class Commands:
        @staticmethod
        def run(_command, timeout):
            assert timeout == 150
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "status": 200,
                            "headers": [["Content-Type", "application/json"]],
                            "body_base64": "e30=",
                        }
                    )
                },
            )()

    sandbox = type("Sandbox", (), {"files": Files(), "commands": Commands()})()
    fake_sdk = type("SDK", (), {"connect": staticmethod(lambda _sandbox_id: sandbox)})
    target = {"sandbox_id": "fleet-sandbox", "port": 13019}

    with patch.object(hostmap_proxy, "_E2BSandbox", fake_sdk):
        response = hostmap_proxy._open_direct(
            target,
            "PUT",
            "/api/state",
            {"Content-Type": "application/json", "Cookie": "user_id=test"},
            b'{"large":true}',
        )

    assert response.status == 200
    assert response.read() == b"{}"
    assert Files.envelope["port"] == 13019
    assert Files.envelope["method"] == "PUT"
    assert Files.envelope["body_base64"] == "eyJsYXJnZSI6dHJ1ZX0="
