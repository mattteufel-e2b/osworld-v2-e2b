from __future__ import annotations

import contextlib
import base64
import http.client
import io
import importlib.util
import json
import shutil
import socket
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

requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI required"
)


@pytest.fixture(scope="module")
def tls_cert(tmp_path_factory):
    """A throwaway localhost leaf cert/key pair for the TLS listener tests."""
    tmp_path = tmp_path_factory.mktemp("hostmap-tls")
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
    return crt, key


@contextlib.contextmanager
def _serving(*servers):
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


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


def test_chunked_request_body_and_trailers_leave_next_request_intact():
    route = {"ingress_host": "gitlab.example", "traffic_token": None}
    bodies = []

    def upstream(request):
        bodies.append(request.data)
        return hostmap_proxy._DirectResponse(
            {"status": 200, "headers": [], "body_base64": "e30="}
        )

    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(
            hostmap_proxy, "_load_rules", return_value=({"gitlab": route}, {})
        ),
        patch.object(hostmap_proxy, "_open_upstream", side_effect=upstream),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.putrequest("POST", "/repo.git/git-receive-pack", skip_host=True)
        conn.putheader("Host", "gitlab")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"4;ext=yes\r\nPACK\r\n4\r\ndata\r\n0\r\nX-Checksum: ignored\r\n\r\n")
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.request("POST", "/next", body=b"next", headers={"Host": "gitlab"})
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.close()
    assert bodies == [b"PACKdata", b"next"]


@pytest.mark.parametrize("body", [b"nope\r\n", b"-1\r\n", b"3\r\nabcXX", b"4\r\nab"])
def test_malformed_chunked_request_is_rejected(body):
    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(
            hostmap_proxy,
            "_load_rules",
            return_value=({"gitlab": {"ingress_host": "unused"}}, {}),
        ),
    ):
        conn = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        conn.sendall(
            b"POST / HTTP/1.1\r\nHost: gitlab\r\nTransfer-Encoding: chunked\r\n\r\n"
            + body
        )
        conn.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(conn)
        response.begin()
        assert response.status == 400
        conn.close()


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
        rules, asset_map = hostmap_proxy._load_rules()

    assert asset_map == {}  # absent websites.asset_url_map
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
        "pages_suffix": "gitlab.127.0.0.1.nip.io",
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
        rules, asset_map = hostmap_proxy._load_rules()

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


@requires_openssl
def test_tls_listener_terminates_tls_and_plain_listener_redirects(tls_cert):
    crt, key = tls_cert
    tls_server = hostmap_proxy.make_server(0, tls=(str(crt), str(key)))
    plain_server = hostmap_proxy.make_server(0, tls=None, redirect_to_https=True)
    with _serving(tls_server, plain_server):
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
        err.value.close()


@requires_openssl
def test_tls_handshakes_run_off_the_accept_loop_and_survive_bad_clients(tls_cert):
    """One silent TCP connection must not wedge the single accept thread.

    Chrome preconnects constantly, so a TLS handshake performed inside
    `get_request()` blocks every other client on that listener forever.
    """
    crt, key = tls_cert
    tls_server = hostmap_proxy.make_server(0, tls=(str(crt), str(key)))
    port = tls_server.server_address[1]
    ctx = ssl.create_default_context(cafile=str(crt))

    def get_state():
        return urllib.request.urlopen(
            urllib.request.Request(
                f"https://localhost:{port}/api/state",
                headers={"Host": "nowhere.127.0.0.1.nip.io"},
            ),
            context=ctx,
            timeout=5,
        )

    with _serving(tls_server):
        idle = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            with pytest.raises(urllib.error.HTTPError) as err:
                get_state()
            assert err.value.code == 502

            # Plain HTTP bytes on the TLS port fail cleanly (a TLS alert and
            # a closed or reset socket -- never an HTTP response)...
            with socket.create_connection(("127.0.0.1", port), timeout=5) as plain:
                plain.sendall(b"GET / HTTP/1.1\r\nHost: mailhub\r\n\r\n")
                try:
                    assert not plain.recv(4096).startswith(b"HTTP/")
                except ConnectionResetError:
                    pass

            # ...and the listener still answers afterwards.
            with pytest.raises(urllib.error.HTTPError) as err:
                get_state()
            assert err.value.code == 502
        finally:
            idle.close()


def test_head_keeps_upstreams_content_length_and_sends_no_body():
    """A HEAD must describe the body a GET would return, not the empty one."""

    class FakeResponse:
        status = 200
        headers = hostmap_proxy._HeaderList(
            [("Content-Type", "application/pdf"), ("Content-Length", "4096")]
        )

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    server = hostmap_proxy.make_server(0, tls=None)
    route = {"ingress_host": "13030-w.e2b.app", "traffic_token": None}
    with (
        _serving(server),
        patch.object(
            hostmap_proxy,
            "_load_rules",
            return_value=({"files.127.0.0.1.nip.io": route}, {}),
        ),
        patch.object(hostmap_proxy, "_open_upstream", return_value=FakeResponse()),
    ):
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        headers = {"Host": "files.127.0.0.1.nip.io"}
        conn.request("HEAD", "/a.pdf", headers=headers)
        head = conn.getresponse()
        head.read()
        conn.request("GET", "/a.pdf", headers=headers)
        get = conn.getresponse()
        body = get.read()
        conn.close()

    assert head.getheader("Content-Length") == "4096"
    assert get.getheader("Content-Length") == "0"
    assert body == b""


def test_upstream_failure_502_names_the_request_body_size():
    """An oversized guest-originated /api/state write is otherwise invisible."""
    server = hostmap_proxy.make_server(0, tls=None)
    route = {"ingress_host": "13030-w.e2b.app", "traffic_token": None}
    with (
        _serving(server),
        patch.object(
            hostmap_proxy,
            "_load_rules",
            return_value=({"s.127.0.0.1.nip.io": route}, {}),
        ),
        patch.object(
            hostmap_proxy, "_open_upstream", side_effect=OSError("connection reset")
        ),
    ):
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        conn.request(
            "POST",
            "/api/state",
            body=b"x" * 4096,
            headers={"Host": "s.127.0.0.1.nip.io"},
        )
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()

    assert response.status == 502
    assert "4096" in body
    assert "connection reset" in body


@pytest.fixture
def pages_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "pages-runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "gitlab": {
                    "host": "gitlab.127.0.0.1.nip.io",
                    "aliases": ["54.174.16.65.sslip.io"],
                    "ingress_host": "8929-g.e2b.app",
                    "traffic_token": "trusted-token",
                    "sandbox_id": "g",
                    "port": 8929,
                },
                "websites": {"sites": {"mailhub": {"ingress_host": "mail.e2b.app"}}},
            }
        )
    )
    monkeypatch.setattr(hostmap_proxy, "RUNTIME_FILE", runtime)


@pytest.mark.parametrize("direct", [False, True])
def test_pages_routes_are_bounded_and_client_cannot_supply_routing_header(
    pages_runtime, direct
):
    def reply(headers):
        body = json.dumps({k.lower(): v for k, v in headers.items()}).encode()
        return hostmap_proxy._DirectResponse(
            {
                "status": 200,
                "headers": [],
                "body_base64": base64.b64encode(body).decode(),
            }
        )

    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(hostmap_proxy, "_DIRECT_BODY_THRESHOLD", 0 if direct else 900_000),
        patch.object(hostmap_proxy, "_E2BSandbox", object()),
        patch.object(
            hostmap_proxy,
            "_open_upstream",
            side_effect=lambda req: reply(dict(req.header_items())),
        ),
        patch.object(
            hostmap_proxy,
            "_open_direct",
            side_effect=lambda target, method, path, headers, body: reply(headers),
        ),
    ):
        for hostname, expected in [
            (
                "typesql-ab12.gitlab.127.0.0.1.nip.io",
                "typesql-ab12.gitlab.127.0.0.1.nip.io",
            ),
            ("X.gitlab.127.0.0.1.nip.io", "x.gitlab.127.0.0.1.nip.io"),
            ("gitlab.127.0.0.1.nip.io", None),
            ("54.174.16.65.sslip.io", None),
            ("mailhub.127.0.0.1.nip.io", None),
            ("a.b.gitlab.127.0.0.1.nip.io", False),
            ("-a.gitlab.127.0.0.1.nip.io", False),
            ("a-.gitlab.127.0.0.1.nip.io", False),
            ("a_b.gitlab.127.0.0.1.nip.io", False),
            ("a" * 64 + ".gitlab.127.0.0.1.nip.io", False),
            ("a.54.174.16.65.sslip.io", False),
            ("a.gitlab.127.0.0.1.nip.io.evil.test", False),
        ]:
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            conn.request(
                "POST",
                "/",
                body=b"x",
                headers={
                    "Host": hostname,
                    "x-OSWorld-Pages-HOST": "attacker.example",
                },
            )
            response = conn.getresponse()
            body = response.read()
            conn.close()
            assert response.status == (502 if expected is False else 200), hostname
            if expected is not False:
                assert json.loads(body).get("x-osworld-pages-host") == expected, (
                    hostname
                )


@pytest.mark.parametrize(
    "authority,status,direct",
    [
        ("gitlab.127.0.0.1.nip.io:8090", 200, False),
        ("54.174.16.65.sslip.io:8090", 302, False),
        ("54.174.16.65.sslip.io", 404, False),
        ("typesql-ab12.gitlab.127.0.0.1.nip.io", 200, False),
        ("typesql-ab12.gitlab.127.0.0.1.nip.io:8090", 404, True),
        ("54.174.16.65.sslip.io:8090", 302, True),
    ],
)
def test_pages_links_keep_canonical_hostname_and_client_port_in_all_responses(
    pages_runtime, authority, status, direct
):
    original = b'{"http":"http://typesql-ab12.gitlab.127.0.0.1.nip.io/","https":"https://typesql-ab12.gitlab.127.0.0.1.nip.io?preview=1","other":"http://a.b.gitlab.127.0.0.1.nip.io/"}'
    location = "http://typesql-ab12.gitlab.127.0.0.1.nip.io/#preview"

    def upstream(_request):
        if status != 200:
            raise urllib.error.HTTPError(
                "https://ingress/",
                status,
                "error",
                {"Location": location},
                io.BytesIO(original),
            )
        return response()

    def response():
        return hostmap_proxy._DirectResponse(
            {
                "status": status,
                "headers": [["Location", location]],
                "body_base64": base64.b64encode(original).decode(),
            }
        )

    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(hostmap_proxy, "_DIRECT_BODY_THRESHOLD", 0 if direct else 900_000),
        patch.object(hostmap_proxy, "_E2BSandbox", object()),
        patch.object(hostmap_proxy, "_open_upstream", side_effect=upstream),
        patch.object(
            hostmap_proxy, "_open_direct", side_effect=lambda *args: response()
        ),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/", body=b"x", headers={"Host": authority})
        result = conn.getresponse()
        raw_body = result.read()
        conn.close()
    assert result.status == status
    body = json.loads(raw_body)
    port = ":8090" if authority.endswith(":8090") else ""
    wanted = "https://typesql-ab12.gitlab.127.0.0.1.nip.io" + port
    assert result.getheader("Location") == wanted + "/#preview"
    assert body == {
        "http": wanted + "/",
        "https": wanted + "?preview=1",
        "other": "http://a.b.gitlab.127.0.0.1.nip.io/",
    }


def test_unroutable_host_is_logged_to_stderr(capsys):
    # log_message is a no-op, so a proxy that starts refusing every request
    # mid-campaign used to answer 502 in total silence: the campaign log showed
    # only the workers' downstream failures.
    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(hostmap_proxy, "_load_rules", return_value=({}, {})),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/api/state?cookie=x", headers={"Host": "nope.test"})
        response = conn.getresponse()
        response.read()
        conn.close()

    assert response.status == 502
    assert (
        "[hostmap_proxy] 502 GET nope.test/api/state?cookie=x: "
        "no fleet route for Host 'nope.test'" in capsys.readouterr().err
    )


def test_failed_upstream_relay_is_logged_to_stderr(capsys):
    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(
            hostmap_proxy,
            "_load_rules",
            return_value=({"gitlab": {"ingress_host": "ingress.test"}}, {}),
        ),
        patch.object(
            hostmap_proxy, "_open_upstream", side_effect=OSError("connection reset")
        ),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("POST", "/api/v4/version", body=b"x", headers={"Host": "gitlab"})
        response = conn.getresponse()
        response.read()
        conn.close()

    err = capsys.readouterr().err
    assert "[hostmap_proxy] 502 POST gitlab/api/v4/version:" in err
    assert "connection reset" in err


def test_relayed_upstream_5xx_is_logged_but_success_stays_quiet(capsys):
    def upstream(_request):
        if _request.selector == "/quiet":
            return hostmap_proxy._DirectResponse(
                {"status": 200, "headers": [], "body_base64": ""}
            )
        raise urllib.error.HTTPError(
            "https://ingress.test/slow", 504, "Gateway Timeout", {}, io.BytesIO(b"")
        )

    server = hostmap_proxy.make_server(0, tls=None)
    with (
        _serving(server),
        patch.object(
            hostmap_proxy,
            "_load_rules",
            return_value=({"gitlab": {"ingress_host": "ingress.test"}}, {}),
        ),
        patch.object(hostmap_proxy, "_open_upstream", side_effect=upstream),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/slow", headers={"Host": "gitlab"})
        failed = conn.getresponse()
        failed.read()
        conn.request("GET", "/quiet", headers={"Host": "gitlab"})
        ok = conn.getresponse()
        ok.read()
        conn.close()

    err = capsys.readouterr().err
    assert failed.status == 504 and ok.status == 200
    assert "[hostmap_proxy] 504 GET gitlab/slow: " in err
    assert "/quiet" not in err
    assert err.count("[hostmap_proxy]") == 1
