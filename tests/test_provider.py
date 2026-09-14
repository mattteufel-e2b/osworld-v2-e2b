from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

V2_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = V2_ROOT / "OSWorld-V2"
if not (UPSTREAM_ROOT / "desktop_env").is_dir():
    import pytest

    pytest.skip(
        "pinned OSWorld-V2 checkout not present (run runner/setup.sh first); "
        "provider tests import desktop_env from it",
        allow_module_level=True,
    )
sys.path.insert(0, str(UPSTREAM_ROOT))
sys.path.insert(0, str(V2_ROOT))

from desktop_env.evaluators.backends.base import BackendConfig  # noqa: E402
from desktop_env.evaluators.backends.openai_backend import OpenAIBackend  # noqa: E402

bridge_spec = importlib.util.spec_from_file_location(
    "desktop_env.providers.e2b.bridge", V2_ROOT / "provider" / "bridge.py"
)
bridge_module = importlib.util.module_from_spec(bridge_spec)
assert bridge_spec.loader is not None
sys.modules["desktop_env.providers.e2b.bridge"] = bridge_module
bridge_spec.loader.exec_module(bridge_module)

spec = importlib.util.spec_from_file_location(
    "e2b_provider_under_test", V2_ROOT / "provider" / "provider.py"
)
provider_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(provider_module)

manager_spec = importlib.util.spec_from_file_location(
    "e2b_manager_under_test", V2_ROOT / "provider" / "manager.py"
)
manager_module = importlib.util.module_from_spec(manager_spec)
assert manager_spec.loader is not None
manager_spec.loader.exec_module(manager_module)


class EvaluatorTransportTests(unittest.TestCase):
    def test_openai_evaluator_leaves_sdk_transport_defaults_untouched(self):
        captured = {}
        fake_openai = types.ModuleType("openai")

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_openai.OpenAI = FakeOpenAI
        config = BackendConfig(
            provider="openai_compatible",
            model="judge",
            api_key="secret",
            base_url="https://example.test/v1",
        )

        with patch.dict(sys.modules, {"openai": fake_openai}):
            OpenAIBackend(config)

        self.assertNotIn("timeout", captured)
        self.assertNotIn("max_retries", captured)


class ProviderVolumeTests(unittest.TestCase):
    def test_prepare_volume_normalizes_and_records_positive_gib(self):
        provider = provider_module.E2BProvider()

        provider.prepare_volume("ignored", "100", "Ubuntu")

        self.assertEqual(provider.volume_size, 100)

    def test_prepare_volume_rejects_non_positive_size(self):
        provider = provider_module.E2BProvider()

        with self.assertRaisesRegex(ValueError, "positive"):
            provider.prepare_volume("ignored", 0, "Ubuntu")


class FakeBridge:
    def __init__(self, config):
        self.config = config
        self.started = False
        self.calls = []
        self.server_port, self.cdp_port, self.vlc_port = 50001, 50002, 50003

    def start(self):
        self.started = True
        self.calls.append("start")

    def reset(self, snapshot_name=None):
        self.calls.append(("reset", snapshot_name))
        return {"sandbox_id": "sbx-2", "generation": 2, "source": "template"}

    def save(self, name):
        self.calls.append(("save", name))
        return "snap-1"

    def check_volume(self, gb):
        self.calls.append(("volume", gb))
        return {"requested_gb": gb, "root_capacity_bytes": 10**11}

    def state(self):
        return {"sandbox_id": "sbx-1", "generation": 1}

    def stop(self):
        self.calls.append("stop")


class ProviderBridgeTests(unittest.TestCase):
    TEMPLATE = "osworld-v2-gnome:817519a3-6360-475f-bdae-77780743d6a5"

    def setUp(self):
        self.env = patch.dict(
            os.environ, {"OSWORLD_CAMPAIGN_ID": "c", "GUEST_TEMPLATE": self.TEMPLATE}
        )
        self.env.start()
        self.bridge_patch = patch.object(provider_module, "Bridge", FakeBridge)
        self.bridge_patch.start()

    def tearDown(self):
        self.bridge_patch.stop()
        self.env.stop()

    def test_start_emulator_builds_and_starts_one_bridge_for_the_template(self):
        provider = provider_module.E2BProvider()
        provider.start_emulator(self.TEMPLATE, headless=True)
        provider.start_emulator(self.TEMPLATE, headless=True)  # after a revert
        self.assertEqual(provider.bridge.calls, ["start"])
        self.assertEqual(provider.bridge.config.template, self.TEMPLATE)

    def test_ip_tuple_uses_the_bridge_ports_with_vnc_zero(self):
        provider = provider_module.E2BProvider()
        provider.start_emulator(self.TEMPLATE, headless=True)
        self.assertEqual(
            provider.get_ip_address(self.TEMPLATE), "127.0.0.1:50001:50002:0:50003"
        )

    def test_revert_save_volume_and_stop_delegate_to_the_bridge(self):
        provider = provider_module.E2BProvider()
        provider.start_emulator(self.TEMPLATE, headless=True)
        self.assertEqual(
            provider.revert_to_snapshot(self.TEMPLATE, "init_state"), self.TEMPLATE
        )
        provider.save_state(self.TEMPLATE, "mid")
        provider.finalize_volume(self.TEMPLATE, 80, "Ubuntu", None, None, "pw")
        provider.finalize_volume(self.TEMPLATE, None, "Ubuntu", None, None, "pw")
        stopped = provider.bridge  # stop_emulator drops the stopped bridge
        provider.stop_emulator(self.TEMPLATE)
        self.assertEqual(
            stopped.calls,
            ["start", ("reset", "init_state"), ("save", "mid"), ("volume", 80), "stop"],
        )
        # A later start_emulator builds a fresh bridge rather than reusing the
        # stopped one.
        self.assertIsNone(provider.bridge)
        provider.start_emulator(self.TEMPLATE, headless=True)
        self.assertIsNot(provider.bridge, stopped)
        self.assertEqual(provider.bridge.calls, ["start"])

    def test_get_ip_address_before_start_fails_closed(self):
        provider = provider_module.E2BProvider()
        with self.assertRaisesRegex(RuntimeError, "start_emulator"):
            provider.get_ip_address(self.TEMPLATE)


class ManagerIdentityTests(unittest.TestCase):
    def test_manager_requires_guest_template_environment_variable(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "GUEST_TEMPLATE"),
        ):
            manager_module.E2BVMManager().get_vm_path()

    def test_manager_rejects_mutable_guest_template_alias(self):
        with (
            patch.dict(os.environ, {"GUEST_TEMPLATE": "osworld-v2-gnome"}, clear=True),
            self.assertRaisesRegex(ValueError, "immutable name:build_id"),
        ):
            manager_module.E2BVMManager().get_vm_path()

    def test_manager_returns_immutable_guest_template_reference(self):
        reference = "osworld-v2-gnome:817519a3-6360-475f-bdae-77780743d6a5"
        with patch.dict(os.environ, {"GUEST_TEMPLATE": reference}, clear=True):
            self.assertEqual(manager_module.E2BVMManager().get_vm_path(), reference)


if __name__ == "__main__":
    unittest.main()
