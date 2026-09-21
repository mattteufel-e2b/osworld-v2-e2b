import json
import os
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
