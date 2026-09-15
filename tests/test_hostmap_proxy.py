from __future__ import annotations

import importlib.util
import json
import shutil
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

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

    assert (
        redirect_handler.redirect_request(None, None, 302, "Found", {}, "https://next")
        is None
    )


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


def test_guest_routes_load_traffic_tokens_for_authenticated_ingress(tmp_path):
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {
                "websites": {
                    "traffic_token": "websites-traffic-token",
                    "host_suffix": "127.0.0.1.nip.io",
                    "sites": {
                        "mailhub": {
                            "ingress_host": "13001-websites.e2b.app",
                            "port": 13001,
                        }
                    },
                },
                "gitlab": {
                    "traffic_token": "gitlab-traffic-token",
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
        "traffic_token": "websites-traffic-token",
        "sandbox_id": None,
        "port": 13001,
    }
    assert rules["gitlab.127.0.0.1.nip.io"] == {
        "ingress_host": "8929-gitlab.e2b.app",
        "traffic_token": "gitlab-traffic-token",
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


def test_absolute_site_urls_take_the_scheme_the_client_used():
    body = b'{"u":"http://gitlab.127.0.0.1.nip.io/x","v":"https://gitlab.127.0.0.1.nip.io/y"}'
    out = hostmap_proxy._rewrite_absolute_site_urls(
        body, "gitlab.127.0.0.1.nip.io", "gitlab.127.0.0.1.nip.io:8090", scheme="https"
    )
    assert (
        out
        == b'{"u":"https://gitlab.127.0.0.1.nip.io:8090/x","v":"https://gitlab.127.0.0.1.nip.io:8090/y"}'
    )
    # 041's alias keeps its own portless authority on the same listener
    assert (
        hostmap_proxy._rewrite_absolute_site_urls(
            b"http://gitlab.127.0.0.1.nip.io/",
            "gitlab.127.0.0.1.nip.io",
            "54.174.16.65.sslip.io",
            scheme="https",
        )
        == b"https://54.174.16.65.sslip.io/"
    )


def test_rules_include_gitlab_aliases_and_the_asset_url_map(tmp_path):
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {
                "websites": {
                    "host_suffix": "127.0.0.1.nip.io",
                    "traffic_token": "t",
                    "sandbox_id": "w",
                    "sites": {
                        "files": {"ingress_host": "13030-w.e2b.app", "port": 13030}
                    },
                    "asset_url_map": {
                        "https://huggingface.co/datasets/xlangai/osworld_v2_file_cache/resolve/main/task_098/AI-Assisted_Healthcare.zip": "https://files.127.0.0.1.nip.io/task_026/AI-Assisted_Healthcare.zip"
                    },
                },
                "gitlab": {
                    "host": "gitlab.127.0.0.1.nip.io",
                    "ingress_host": "8929-g.e2b.app",
                    "traffic_token": "g",
                    "sandbox_id": "g",
                    "port": 8929,
                    "aliases": ["54.174.16.65.sslip.io"],
                },
            }
        )
    )
    with patch.object(hostmap_proxy, "RUNTIME_FILE", runtime_file):
        rules = hostmap_proxy._load_rules()
        asset_map = hostmap_proxy._load_asset_url_map()

    assert rules["54.174.16.65.sslip.io"]["ingress_host"] == "8929-g.e2b.app"
    assert rules["54.174.16.65.sslip.io"]["canonical_host"] == "gitlab.127.0.0.1.nip.io"
    assert asset_map == {
        b"https://huggingface.co/datasets/xlangai/osworld_v2_file_cache/resolve/main/task_098/AI-Assisted_Healthcare.zip": b"https://files.127.0.0.1.nip.io/task_026/AI-Assisted_Healthcare.zip"
    }


def test_state_payloads_get_dead_asset_urls_mapped_only_on_state_endpoints():
    body = b'{"a":"https://huggingface.co/x.zip"}'
    mapping = {b"https://huggingface.co/x.zip": b"https://files.127.0.0.1.nip.io/x.zip"}
    assert (
        hostmap_proxy._map_asset_urls(body, "/api/state", mapping)
        == b'{"a":"https://files.127.0.0.1.nip.io/x.zip"}'
    )
    assert hostmap_proxy._map_asset_urls(body, "/api/other", mapping) == body


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI required")
def test_tls_listener_terminates_tls_and_plain_listener_redirects(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    tls_server = hostmap_proxy.make_server(0, tls=(str(crt), str(key)))
    plain_server = hostmap_proxy.make_server(0, tls=None, redirect_to_https=True)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True)
        for s in (tls_server, plain_server)
    ]
    for t in threads:
        t.start()
    try:
        ctx = ssl.create_default_context(cafile=str(crt))
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://localhost:{tls_server.server_address[1]}/api/state",
                    headers={"Host": "nowhere.127.0.0.1.nip.io"},
                ),
                context=ctx,
                timeout=5,
            )
        assert err.value.code == 502  # TLS handshake succeeded; no route for that host

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(
                urllib.request.Request(
                    f"http://127.0.0.1:{plain_server.server_address[1]}/x?y=1",
                    headers={"Host": "mailhub.127.0.0.1.nip.io"},
                ),
                timeout=5,
            )
        assert err.value.code == 301
        assert err.value.headers["Location"] == "https://mailhub.127.0.0.1.nip.io/x?y=1"
    finally:
        tls_server.shutdown()
        plain_server.shutdown()
        tls_server.server_close()
        plain_server.server_close()
