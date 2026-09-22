import contextlib
import json
import os
import signal
import subprocess
from pathlib import Path


def test_fleet_wiring_preserves_tls_paths_with_spaces(tmp_path):
    services = tmp_path / "services with spaces"
    services.mkdir()
    tls = {
        key: str(services / filename)
        for key, filename in (
            ("ca_cert", "ca.crt"),
            ("bundle", "bundle.crt"),
            ("leaf_cert", "leaf.crt"),
            ("leaf_key", "leaf.key"),
        )
    }
    (services / ".runtime.json").write_text(
        json.dumps(
            {
                "websites": {"public_host_suffix": "sites.test:8090"},
                "gitlab": {"url": "https://gitlab.test:8090"},
                "tls": tls,
            }
        )
    )
    (services / ".gitlab-token").write_text("test-token")
    result = subprocess.run(
        [
            "bash",
            "-c",
            """source runner/common.sh && export_fleet_wiring &&
            python3 -c 'import os,json; print(json.dumps({k:os.environ[k] for k in
            ["OSWORLD_CA_CERT","REQUESTS_CA_BUNDLE","SSL_CERT_FILE","HOSTMAP_TLS_CERT","HOSTMAP_TLS_KEY"]}))'
        """,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "OSWORLD_SERVICES_DIR": str(services)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "OSWORLD_CA_CERT": tls["ca_cert"],
        "REQUESTS_CA_BUNDLE": tls["bundle"],
        "SSL_CERT_FILE": tls["bundle"],
        "HOSTMAP_TLS_CERT": tls["leaf_cert"],
        "HOSTMAP_TLS_KEY": tls["leaf_key"],
    }


def test_start_host_proxy_records_its_pid_and_appends_to_the_log(tmp_path):
    # The watchdog restarts the proxy, so the pid the caller must later kill is
    # whichever one started last: it lives in the pid file, not in the caller's
    # first copy of $proxy_pid. The log is appended to for the same reason --
    # a restart must not erase what the dead proxy said on its way out.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "curl").chmod(0o755)
    (bin_dir / "fake-uv").write_text('#!/bin/sh\necho "started $*"\nexec sleep 30\n')
    (bin_dir / "fake-uv").chmod(0o755)
    services = tmp_path / "services"
    services.mkdir()
    (services / ".runtime.json").write_text("{}")
    log = tmp_path / "proxy.log"
    log.write_text("first proxy said goodbye\n")
    pid_file = tmp_path / "proxy.pid"

    result = subprocess.run(
        [
            "bash",
            "-c",
            """set -uo pipefail
            source runner/common.sh
            UV="fake-uv"
            start_host_proxy restart-cookie "$LOG"
            echo "rc=$? pid=$proxy_pid"
            """,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "OSWORLD_SERVICES_DIR": str(services),
            "HOSTMAP_TLS_CERT": str(tmp_path / "leaf.crt"),
            "HOSTMAP_TLS_KEY": str(tmp_path / "leaf.key"),
            "OSWORLD_CA_CERT": str(tmp_path / "ca.crt"),
            "HOSTMAP_PROXY_PID_FILE": str(pid_file),
            "LOG": str(log),
        },
        capture_output=True,
        text=True,
    )

    assert "rc=0 pid=" in result.stdout, (result.stdout, result.stderr)
    pid = int(result.stdout.split("pid=")[1].strip())
    try:
        assert pid_file.read_text().strip() == str(pid)
        contents = log.read_text()
        assert "first proxy said goodbye" in contents  # appended, not truncated
        assert "hostmap_proxy.py" in contents  # the proxy this helper started
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
