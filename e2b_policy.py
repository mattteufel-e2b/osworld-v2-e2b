"""Shared E2B runtime policy for the OSWorld guest and service sandboxes."""

from __future__ import annotations

import re

_IMMUTABLE_TEMPLATE_RE = re.compile(
    r"^[a-z0-9_-]+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# OSWorld setup needs the public internet for signed package repositories,
# source checkouts, and OCI pulls. Deny destinations that can expose cloud
# metadata, customer private networks, or non-public routing domains while
# leaving public task dependencies reachable.
PROTECTED_EGRESS_CIDRS = [
    "10.0.0.0/8",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
    # E2B guests currently use IPv4 for the benchmark, but deny IPv6 loopback,
    # unique-local, link-local, and multicast ranges as defense in depth if an
    # image or platform update enables IPv6 routing.
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
]


def require_immutable_template_ref(reference: str | None, variable: str) -> str:
    """Return a validated E2B ``name:build_id`` reference or fail closed."""
    if not reference:
        raise ValueError(
            f"{variable} is required and must be an immutable name:build_id reference"
        )
    if not _IMMUTABLE_TEMPLATE_RE.fullmatch(reference):
        raise ValueError(
            f"{variable} must be an immutable name:build_id reference; got {reference!r}"
        )
    return reference


def require_campaign_id(value: str | None) -> str:
    """Return a bounded metadata-safe campaign id or fail closed."""
    if not value or _CAMPAIGN_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "OSWORLD_CAMPAIGN_ID is required (1-80 letters, digits, dot, underscore, or dash)"
        )
    return value


def sandbox_network_policy() -> dict[str, object]:
    """Authenticated ingress plus public egress excluding protected ranges."""
    return {
        "allow_public_traffic": False,
        "deny_out": list(PROTECTED_EGRESS_CIDRS),
    }
