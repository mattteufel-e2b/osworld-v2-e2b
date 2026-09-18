#!/usr/bin/env python3
"""Per-campaign CA and leaf certificate for the HTTPS fleet origins.

Generated on the coordinator with the openssl CLI at fleet launch. The CA key
never leaves this directory; the leaf key is uploaded to each guest because the
root-owned guest proxy terminates TLS there. `bundle` is certifi's roots plus
the campaign CA so REQUESTS_CA_BUNDLE/SSL_CERT_FILE keep public HTTPS working.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleetlib as fl  # noqa: E402

TLS_DIR = fl.SERVICES_DIR / ".campaign-tls"
TASK_041_GITLAB_ALIAS = "54.174.16.65.sslip.io"  # tasks/task_041.py:87 opens this host
_DAYS = "30"


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True, text=True)


def _leaf(hosts: list[str], ca_key: Path, ca_cert: Path) -> None:
    key, csr, crt, ext = (
        TLS_DIR / n for n in ("leaf.key", "leaf.csr", "leaf.crt", "leaf.ext")
    )
    _openssl("genrsa", "-out", str(key), "2048")
    key.chmod(0o600)
    _openssl(
        "req",
        "-new",
        "-key",
        str(key),
        "-subj",
        "/CN=osworld-campaign-fleet",
        "-out",
        str(csr),
    )
    ext.write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName="
        + ",".join(f"DNS:{h}" for h in hosts)
        + "\n"
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        _DAYS,
        "-sha256",
        "-extfile",
        str(ext),
        "-out",
        str(crt),
    )
    csr.unlink(missing_ok=True)


def _bundle(ca_cert: Path) -> Path:
    import certifi

    bundle = TLS_DIR / "bundle.crt"
    bundle.write_text(Path(certifi.where()).read_text() + "\n" + ca_cert.read_text())
    return bundle


def ensure_campaign_tls(campaign_id: str, hosts: list[str]) -> dict:
    """Create or extend the campaign CA/leaf so the leaf covers every host.

    `hosts` is unioned with whatever the persisted `tls` section already
    covers for THIS campaign -- callers only ever declare their own hosts
    (e.g. the gitlab launcher passes just its one host), so the leaf must
    keep every host an earlier call for the same campaign already put there,
    or a later, narrower call would silently drop coverage. A different
    campaign_id (or missing/incomplete CA material) resets everything, so a
    stale campaign's hostnames never leak into a fresh leaf.

    Returns {"campaign_id", "hosts": sorted list, "ca_cert", "ca_key",
    "leaf_cert", "leaf_key", "bundle"} with absolute path strings; writes the
    same dict minus "ca_key" as the `tls` section of .runtime.json.
    """
    persisted = fl.read_runtime().get("tls")
    current = dict(persisted) if isinstance(persisted, dict) else {}
    ca_key, ca_cert = TLS_DIR / "ca.key", TLS_DIR / "ca.crt"
    same_campaign = (
        current.get("campaign_id") == campaign_id
        and ca_key.is_file()
        and ca_cert.is_file()
    )
    persisted_hosts = set(current.get("hosts") or []) if same_campaign else set()
    wanted = sorted(set(hosts) | persisted_hosts | {TASK_041_GITLAB_ALIAS})
    if not same_campaign:
        shutil.rmtree(TLS_DIR, ignore_errors=True)
        TLS_DIR.mkdir(mode=0o700)
        _openssl("genrsa", "-out", str(ca_key), "3072")
        ca_key.chmod(0o600)
        _openssl(
            "req",
            "-x509",
            "-new",
            "-key",
            str(ca_key),
            "-sha256",
            "-days",
            _DAYS,
            "-subj",
            f"/CN=OSWorld campaign CA {campaign_id}",
            "-out",
            str(ca_cert),
        )
    leaf_cert, leaf_key = TLS_DIR / "leaf.crt", TLS_DIR / "leaf.key"
    if (
        not same_campaign
        or set(current.get("hosts") or []) != set(wanted)
        or not leaf_cert.is_file()
        or not leaf_key.is_file()
    ):
        _leaf(wanted, ca_key, ca_cert)
    bundle = _bundle(ca_cert)
    info = {
        "campaign_id": campaign_id,
        "hosts": wanted,
        "ca_cert": str(ca_cert),
        "leaf_cert": str(leaf_cert),
        "leaf_key": str(leaf_key),
        "bundle": str(bundle),
    }
    fl.write_runtime_section("tls", info)
    return {**info, "ca_key": str(ca_key)}


def remove_campaign_tls() -> None:
    """Delete TLS_DIR and the `tls` runtime section (verified teardown only)."""
    shutil.rmtree(TLS_DIR, ignore_errors=True)
    runtime = fl.read_runtime()
    if "tls" in runtime:
        runtime.pop("tls")
        fl.write_private_text(
            fl.RUNTIME_FILE, json.dumps(runtime, indent=2, sort_keys=True) + "\n"
        )
