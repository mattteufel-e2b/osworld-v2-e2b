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

    def test_finalize_volume_asks_relay_to_verify_the_requested_capacity(self):
        provider = provider_module.E2BProvider()

        with patch.object(
            provider_module, "_control", return_value={"requested_gb": 80}
        ) as control:
            provider.finalize_volume("ignored", 80, "Ubuntu", None, None, "password")

        control.assert_called_once_with(
            "/volume",
            method="POST",
            payload={"requested_gb": 80},
        )

    def test_finalize_volume_is_a_noop_without_a_request(self):
        provider = provider_module.E2BProvider()

        with patch.object(provider_module, "_control") as control:
            provider.finalize_volume("ignored", None, "Ubuntu", None, None, "password")

        control.assert_not_called()


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
