from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

V2_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fleetlib_under_test", V2_ROOT / "services" / "fleetlib.py"
)
fleetlib = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(fleetlib)

IMMUTABLE_FLEET = "osworld-v2-fleet-base:11111111-2222-3333-4444-555555555555"
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
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
]


class FakeFleetSandbox:
    sandbox_id = "fleet-sandbox"
    traffic_access_token = "fleet-token"

    @classmethod
    def create(cls, template, **kwargs):
        instance = cls()
        instance.template = template
        instance.create_kwargs = kwargs
        return instance

    def kill(self):
        pass


class FleetRuntimePolicyTests(unittest.TestCase):
    def test_ensure_fleet_template_only_validates_prebuilt_immutable_reference(self):
        self.assertFalse(hasattr(fleetlib, "Template"))
        with patch.dict(os.environ, {"FLEET_TEMPLATE": IMMUTABLE_FLEET}, clear=True):
            self.assertEqual(fleetlib.ensure_fleet_template(), IMMUTABLE_FLEET)

    def test_ensure_fleet_template_fails_when_prebuilt_reference_is_missing(self):
        self.assertFalse(hasattr(fleetlib, "Template"))
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "FLEET_TEMPLATE"),
        ):
            fleetlib.ensure_fleet_template()

    def test_create_rejects_mutable_fleet_template(self):
        with (
            patch.object(fleetlib, "read_runtime", return_value={}),
            patch.object(fleetlib, "Sandbox", FakeFleetSandbox),
            self.assertRaisesRegex(ValueError, "immutable name:build_id"),
        ):
            fleetlib.reuse_or_create("websites", template="osworld-v2-fleet-base")

    def test_create_uses_secure_ingress_and_protected_egress_policy(self):
        with (
            patch.object(fleetlib, "read_runtime", return_value={}),
            patch.object(fleetlib, "Sandbox", FakeFleetSandbox),
        ):
            sandbox, created = fleetlib.reuse_or_create("websites", template=IMMUTABLE_FLEET)

        self.assertTrue(created)
        self.assertEqual(sandbox.template, IMMUTABLE_FLEET)
        self.assertTrue(sandbox.create_kwargs["secure"])
        self.assertEqual(
            sandbox.create_kwargs["network"],
            {
                "allow_public_traffic": False,
                "deny_out": PROTECTED_EGRESS_CIDRS,
            },
        )

    def test_stale_runtime_from_a_different_template_is_not_reused(self):
        stale = {
            "websites": {
                "sandbox_id": "old-sandbox",
                "template": "osworld-v2-fleet-base:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            }
        }
        with (
            patch.object(fleetlib, "read_runtime", return_value=stale),
            patch.object(
                fleetlib,
                "_connect",
                side_effect=AssertionError("stale immutable build must not be reconnected"),
            ),
            patch.object(fleetlib, "Sandbox", FakeFleetSandbox),
        ):
            sandbox, created = fleetlib.reuse_or_create(
                "websites",
                template=IMMUTABLE_FLEET,
            )

        self.assertTrue(created)
        self.assertEqual(sandbox.template, IMMUTABLE_FLEET)


if __name__ == "__main__":
    unittest.main()
