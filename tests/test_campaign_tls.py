import importlib.util
import json
import shutil
import ssl
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI required"
)


def load(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fleetlib_t", ROOT / "services" / "fleetlib.py"
    )
    fleetlib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleetlib)
    spec2 = importlib.util.spec_from_file_location(
        "campaign_tls_t", ROOT / "services" / "campaign_tls.py"
    )
    with patch.dict("sys.modules", {"fleetlib": fleetlib}):
        tls = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(tls)
    fleetlib.RUNTIME_FILE = tmp_path / ".runtime.json"
    tls.TLS_DIR = tmp_path / ".campaign-tls"
    return tls, fleetlib


def san_list(cert_path: Path) -> set[str]:
    out = subprocess.check_output(
        ["openssl", "x509", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"],
        text=True,
    )
    return {p.strip().removeprefix("DNS:") for p in out.split("\n")[-2].split(",")}


def test_ensure_creates_ca_leaf_bundle_and_runtime_section(tmp_path):
    tls, fleetlib = load(tmp_path)
    info = tls.ensure_campaign_tls(
        "camp-a", ["mailhub.127.0.0.1.nip.io", "gitlab.127.0.0.1.nip.io"]
    )

    for key in ("ca_cert", "leaf_cert", "leaf_key", "bundle"):
        assert Path(info[key]).is_file(), key
    assert oct(Path(info["leaf_key"]).stat().st_mode & 0o777) == "0o600"
    assert oct(Path(info["ca_key"]).stat().st_mode & 0o777) == "0o600"
    sans = san_list(Path(info["leaf_cert"]))
    assert {
        "mailhub.127.0.0.1.nip.io",
        "gitlab.127.0.0.1.nip.io",
        tls.TASK_041_GITLAB_ALIAS,
    } <= sans
    ctx = ssl.create_default_context(cafile=info["ca_cert"])
    ctx.load_verify_locations(cafile=info["bundle"])  # bundle parses as a CA file
    section = json.loads(fleetlib.RUNTIME_FILE.read_text())["tls"]
    assert section["campaign_id"] == "camp-a"
    assert "ca_key" not in section
    assert section["leaf_cert"] == info["leaf_cert"]


def test_ensure_reissues_leaf_when_new_hosts_appear_but_keeps_the_ca(tmp_path):
    tls, _ = load(tmp_path)
    first = tls.ensure_campaign_tls("camp-a", ["mailhub.127.0.0.1.nip.io"])
    ca_before = Path(first["ca_cert"]).read_bytes()
    second = tls.ensure_campaign_tls(
        "camp-a", ["mailhub.127.0.0.1.nip.io", "studio.streamview.127.0.0.1.nip.io"]
    )

    assert Path(second["ca_cert"]).read_bytes() == ca_before
    assert "studio.streamview.127.0.0.1.nip.io" in san_list(Path(second["leaf_cert"]))


def test_ensure_with_a_subset_of_hosts_keeps_covering_the_full_history(tmp_path):
    # Regression: services/gitlab/launch.py only ever names its own host in its
    # ensure_campaign_tls call. When the websites launcher ran first (the
    # documented order), that single-host call must not drop every website SAN
    # the leaf already covers.
    tls, _ = load(tmp_path)
    first = tls.ensure_campaign_tls(
        "camp-a", ["mailhub.127.0.0.1.nip.io", "files.127.0.0.1.nip.io"]
    )
    assert "mailhub.127.0.0.1.nip.io" in san_list(Path(first["leaf_cert"]))

    second = tls.ensure_campaign_tls("camp-a", ["gitlab.127.0.0.1.nip.io"])

    sans = san_list(Path(second["leaf_cert"]))
    assert {
        "mailhub.127.0.0.1.nip.io",
        "files.127.0.0.1.nip.io",
        "gitlab.127.0.0.1.nip.io",
        tls.TASK_041_GITLAB_ALIAS,
    } <= sans


def test_ensure_with_a_subset_of_hosts_does_not_reissue_the_leaf(tmp_path):
    tls, _ = load(tmp_path)
    first = tls.ensure_campaign_tls(
        "camp-a", ["mailhub.127.0.0.1.nip.io", "gitlab.127.0.0.1.nip.io"]
    )
    leaf_bytes = Path(first["leaf_cert"]).read_bytes()

    # Every host in this call is already covered -- must be a no-op reissue.
    second = tls.ensure_campaign_tls("camp-a", ["gitlab.127.0.0.1.nip.io"])

    assert Path(second["leaf_cert"]).read_bytes() == leaf_bytes


def test_ensure_for_a_different_campaign_starts_fresh(tmp_path):
    tls, _ = load(tmp_path)
    first = tls.ensure_campaign_tls("camp-a", ["mailhub.127.0.0.1.nip.io"])
    first_ca_bytes = Path(
        first["ca_cert"]
    ).read_bytes()  # read before camp-b reuses the same path
    second = tls.ensure_campaign_tls("camp-b", ["mailhub.127.0.0.1.nip.io"])
    assert Path(second["ca_cert"]).read_bytes() != first_ca_bytes


def test_ensure_is_idempotent_for_same_campaign_and_hosts(tmp_path):
    tls, _ = load(tmp_path)
    hosts = ["mailhub.127.0.0.1.nip.io"]
    first = tls.ensure_campaign_tls("camp-a", hosts)
    ca_bytes = Path(first["ca_cert"]).read_bytes()
    leaf_bytes = Path(first["leaf_cert"]).read_bytes()
    second = tls.ensure_campaign_tls("camp-a", hosts)

    assert Path(second["ca_cert"]).read_bytes() == ca_bytes
    assert Path(second["leaf_cert"]).read_bytes() == leaf_bytes


def test_ensure_reissues_leaf_when_leaf_material_is_missing(tmp_path):
    tls, _ = load(tmp_path)
    hosts = ["mailhub.127.0.0.1.nip.io"]
    first = tls.ensure_campaign_tls("camp-a", hosts)
    ca_bytes = Path(first["ca_cert"]).read_bytes()
    Path(first["leaf_cert"]).unlink()
    Path(first["leaf_key"]).unlink()

    second = tls.ensure_campaign_tls("camp-a", hosts)

    assert Path(second["leaf_cert"]).is_file()
    assert Path(second["leaf_key"]).is_file()
    assert set(hosts) <= san_list(Path(second["leaf_cert"]))
    assert Path(second["ca_cert"]).read_bytes() == ca_bytes


def test_remove_deletes_material_and_section(tmp_path):
    tls, fleetlib = load(tmp_path)
    tls.ensure_campaign_tls("camp-a", ["mailhub.127.0.0.1.nip.io"])
    tls.remove_campaign_tls()
    assert not tls.TLS_DIR.exists()
    assert "tls" not in json.loads(fleetlib.RUNTIME_FILE.read_text())
