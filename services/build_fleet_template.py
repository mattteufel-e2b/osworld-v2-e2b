#!/usr/bin/env python3
"""Build the reusable OSWorld service-fleet template and emit its immutable ref."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from e2b import Sandbox, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from e2b_policy import sandbox_network_policy  # noqa: E402

SERVICES_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVICES_DIR.parent
RECEIPT = REPO_ROOT / "out" / "osworld-v2-evidence" / "fleet-template-build.json"

TEMPLATE_NAME = os.environ.get("FLEET_TEMPLATE_NAME", "osworld-v2-fleet-base")
CPU_COUNT = 4
MEMORY_MB = 8192
# The fleet does not receive OSWorld task volume_size requests. Its disk gate is
# instead operational headroom for cloning/building the website and GitLab
# service images after Docker itself has been installed.
MIN_ROOT_FREE_GB = 80
DEBIAN_IMAGE = (
    "debian:bookworm@sha256:6ebd97fa83deb272194a2cf015b3d26a4d538e9ad3a7a79d544c8af5b0a01443"
)
DOCKER_INSTALL_SHA256 = "2df5f9e0f201a967f454191726d9254625f0f08030af3812c9edcdedc78e9693"


def log(message: str) -> None:
    print(f"[fleet-build] {message}", file=sys.stderr, flush=True)


def main() -> int:
    template = (
        Template()
        .from_image(DEBIAN_IMAGE)
        .apt_install(["git", "curl", "ca-certificates"])
        .run_cmd(
            "curl -fsSL -o /tmp/get-docker.sh https://get.docker.com"
            f" && echo '{DOCKER_INSTALL_SHA256}  /tmp/get-docker.sh' | sha256sum -c -"
            " && sh /tmp/get-docker.sh && rm -f /tmp/get-docker.sh"
        )
    )
    info = Template.build(
        template,
        name=TEMPLATE_NAME,
        cpu_count=CPU_COUNT,
        memory_mb=MEMORY_MB,
        on_build_logs=lambda entry: log(f"[build] {getattr(entry, 'message', entry)}"),
    )
    reference = f"{info.name}:{info.build_id}"

    sandbox = Sandbox.create(
        reference,
        timeout=300,
        secure=True,
        network=sandbox_network_policy(),
        metadata={"workload": "osworld-v2-fleet-build-smoke"},
    )
    try:
        applied_network_policy = sandbox.get_info().network
        expected_network_policy = sandbox_network_policy()
        if applied_network_policy != expected_network_policy:
            raise RuntimeError(f"fleet sandbox network policy mismatch: {applied_network_policy!r}")
        result = sandbox.commands.run(
            "df -B1 --output=size,avail / | tail -n 1",
            user="root",
            timeout=30,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"root capacity probe failed: {result.stderr or result.stdout}")
        root_capacity_bytes, root_free_bytes = map(int, (result.stdout or "").split())
        if root_free_bytes < MIN_ROOT_FREE_GB * 1_000_000_000:
            raise RuntimeError(
                f"fleet template has insufficient free root space: "
                f"{root_free_bytes / 1_000_000_000:.2f} GB < {MIN_ROOT_FREE_GB} GB"
            )
        docker = sandbox.commands.run(
            "docker --version && docker compose version", user="root", timeout=30
        )
        if docker.exit_code != 0:
            raise RuntimeError(f"Docker smoke failed: {docker.stderr or docker.stdout}")
    finally:
        sandbox.kill()

    receipt = {
        "generated_at": datetime.now(UTC).isoformat(),
        "name": info.name,
        "template_id": info.template_id,
        "build_id": info.build_id,
        "reference": reference,
        "cpu_count": CPU_COUNT,
        "memory_mb": MEMORY_MB,
        "root_capacity_bytes": root_capacity_bytes,
        "root_free_bytes": root_free_bytes,
        "minimum_root_free_gb": MIN_ROOT_FREE_GB,
        "base_image": DEBIAN_IMAGE,
        "docker_install_sha256": DOCKER_INSTALL_SHA256,
        "smoke_sandbox_id": sandbox.sandbox_id,
        "network_policy": applied_network_policy,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"FLEET_TEMPLATE={reference}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
