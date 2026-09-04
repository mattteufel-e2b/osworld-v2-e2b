from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
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


def load_gitlab_launcher():
    launcher_spec = importlib.util.spec_from_file_location(
        "gitlab_launcher_under_test", V2_ROOT / "services" / "gitlab" / "launch.py"
    )
    launcher = importlib.util.module_from_spec(launcher_spec)
    assert launcher_spec.loader is not None
    with patch.dict(sys.modules, {"fleetlib": fleetlib}):
        launcher_spec.loader.exec_module(launcher)
    return launcher


def load_websites_launcher():
    launcher_spec = importlib.util.spec_from_file_location(
        "websites_launcher_under_test", V2_ROOT / "services" / "websites" / "launch.py"
    )
    launcher = importlib.util.module_from_spec(launcher_spec)
    assert launcher_spec.loader is not None
    with patch.dict(sys.modules, {"fleetlib": fleetlib}):
        launcher_spec.loader.exec_module(launcher)
    return launcher


def load_fleet_template_builder():
    builder_spec = importlib.util.spec_from_file_location(
        "fleet_template_builder_under_test",
        V2_ROOT / "services" / "build_fleet_template.py",
    )
    builder = importlib.util.module_from_spec(builder_spec)
    assert builder_spec.loader is not None
    builder_spec.loader.exec_module(builder)
    return builder


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
    def test_restricted_ingress_gate_requires_authenticated_200_and_unauthenticated_403(
        self,
    ):
        fleetlib.require_restricted_ingress(
            "website", authenticated_status=200, unauthenticated_status=403
        )
        with self.assertRaisesRegex(RuntimeError, "website authenticated ingress"):
            fleetlib.require_restricted_ingress(
                "website", authenticated_status=502, unauthenticated_status=403
            )
        with self.assertRaisesRegex(RuntimeError, "website unauthenticated ingress"):
            fleetlib.require_restricted_ingress(
                "website", authenticated_status=200, unauthenticated_status=200
            )

    def setUp(self):
        self.campaign_environment = patch.dict(
            os.environ, {"OSWORLD_CAMPAIGN_ID": "test-campaign"}
        )
        self.campaign_environment.start()

    def tearDown(self):
        self.campaign_environment.stop()

    def test_service_artifact_paths_are_anchored_to_candidate_root(self):
        gitlab = load_gitlab_launcher()
        websites = load_websites_launcher()
        builder = load_fleet_template_builder()

        self.assertEqual(fleetlib.REPO_ROOT, V2_ROOT)
        self.assertEqual(
            gitlab.LOCKFILE,
            V2_ROOT / "examples" / "osworld-v2" / "upstream.lock.json",
        )
        self.assertTrue(gitlab.LOCKFILE.is_file())
        self.assertEqual(
            gitlab.RECEIPT,
            V2_ROOT / "out" / "osworld-v2-raw" / "services" / "gitlab.json",
        )
        self.assertEqual(
            websites.RECEIPT,
            V2_ROOT / "out" / "osworld-v2-raw" / "services" / "websites.json",
        )
        self.assertEqual(websites.v2_checkout(), V2_ROOT / "OSWorld-V2")
        self.assertEqual(
            builder.RECEIPT,
            V2_ROOT / "out" / "osworld-v2-raw" / "builds" / "fleet-template-build.json",
        )

    def test_failed_v2_website_builder_check_is_release_blocking(self):
        websites = load_websites_launcher()

        with self.assertRaisesRegex(RuntimeError, "V2 website routing check failed"):
            websites.require_v2_builder_success(
                {"status": None, "error": "checkout could not be imported"}
            )

    def test_websites_main_rejects_non_running_host_proxy_without_success_receipt(self):
        websites = load_websites_launcher()
        kill_calls = []
        sandbox = type(
            "Sandbox",
            (),
            {
                "sandbox_id": "website-sandbox",
                "traffic_access_token": "runtime-only-token",
                "get_host": lambda _self, port: f"{port}-website.example.test",
                "kill": lambda _self: kill_calls.append("website-sandbox"),
            },
        )()

        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "services-websites.json"
            with (
                patch.object(websites, "RECEIPT", receipt),
                patch.object(websites.fl, "load_e2b_key"),
                patch.object(
                    websites.fl,
                    "ensure_fleet_template",
                    return_value=IMMUTABLE_FLEET,
                ),
                patch.object(
                    websites.fl,
                    "reuse_or_create",
                    return_value=(sandbox, True),
                ),
                patch.object(websites.fl, "ensure_docker", return_value=0.0),
                patch.object(websites.fl, "ensure_swap"),
                patch.object(websites, "clone_repo"),
                patch.object(
                    websites,
                    "enumerate_sites",
                    return_value={"mailhub": 13001},
                ),
                patch.object(websites, "write_fanout"),
                patch.object(websites, "compose_up", return_value=0.0),
                patch.object(websites, "recreate_fanout"),
                patch.object(websites, "wait_ready", return_value={"mailhub": 0.1}),
                patch.object(
                    websites,
                    "probe_host_ingress",
                    return_value={
                        "per_port_probe": {"status": 200},
                        "unauthenticated_probe": {"status": 403},
                    },
                ),
                patch.object(
                    websites.fl,
                    "restart_host_proxy",
                    return_value={"running": False, "port": 8090},
                ),
                patch.object(websites.fl, "write_runtime_section") as write_runtime,
                self.assertRaisesRegex(RuntimeError, "host proxy failed to start"),
            ):
                websites.main()

            self.assertFalse(receipt.exists())
            write_runtime.assert_not_called()
            self.assertEqual(kill_calls, ["website-sandbox"])

    def test_websites_main_kills_new_sandbox_on_earlier_setup_failure(self):
        websites = load_websites_launcher()
        kill_calls = []
        sandbox = type(
            "Sandbox",
            (),
            {
                "sandbox_id": "website-sandbox",
                "kill": lambda _self: kill_calls.append("website-sandbox"),
            },
        )()

        with (
            patch.object(websites.fl, "load_e2b_key"),
            patch.object(
                websites.fl,
                "ensure_fleet_template",
                return_value=IMMUTABLE_FLEET,
            ),
            patch.object(
                websites.fl,
                "reuse_or_create",
                return_value=(sandbox, True),
            ),
            patch.object(websites.fl, "ensure_docker", return_value=0.0),
            patch.object(websites.fl, "ensure_swap"),
            patch.object(
                websites,
                "clone_repo",
                side_effect=RuntimeError("setup failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "setup failed"),
        ):
            websites.main()

        self.assertEqual(kill_calls, ["website-sandbox"])

    def test_websites_main_rolls_back_runtime_and_kills_new_sandbox_on_final_gate_failure(
        self,
    ):
        websites = load_websites_launcher()
        kill_calls = []
        sandbox = type(
            "Sandbox",
            (),
            {
                "sandbox_id": "website-sandbox",
                "traffic_access_token": "runtime-only-token",
                "get_host": lambda _self, port: f"{port}-website.example.test",
                "kill": lambda _self: kill_calls.append("website-sandbox"),
            },
        )()

        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "services-websites.json"
            with (
                patch.object(websites, "RECEIPT", receipt),
                patch.object(websites, "websites_pin", return_value="a" * 40),
                patch.object(websites.fl, "load_e2b_key"),
                patch.object(
                    websites.fl, "ensure_fleet_template", return_value=IMMUTABLE_FLEET
                ),
                patch.object(
                    websites.fl, "reuse_or_create", return_value=(sandbox, True)
                ),
                patch.object(websites.fl, "ensure_docker", return_value=0.0),
                patch.object(websites.fl, "ensure_swap"),
                patch.object(websites, "clone_repo"),
                patch.object(
                    websites, "enumerate_sites", return_value={"mailhub": 13001}
                ),
                patch.object(websites, "write_fanout"),
                patch.object(websites, "compose_up", return_value=0.0),
                patch.object(websites, "recreate_fanout"),
                patch.object(websites, "wait_ready", return_value={"mailhub": 0.1}),
                patch.object(
                    websites,
                    "probe_host_ingress",
                    return_value={
                        "per_port_probe": {"status": 200},
                        "unauthenticated_probe": {"status": 403},
                    },
                ),
                patch.object(
                    websites.fl,
                    "restart_host_proxy",
                    return_value={"running": True, "port": 8090},
                ),
                patch.object(
                    websites.fl, "verify_host_proxy_path", return_value={"status": 200}
                ),
                patch.object(
                    websites, "verify_via_v2_builder", return_value={"status": 500}
                ),
                patch.object(websites.fl, "write_runtime_section"),
                patch.object(websites.fl, "delete_runtime_section") as delete_runtime,
                self.assertRaisesRegex(RuntimeError, "V2 website routing check failed"),
            ):
                websites.main()

            self.assertFalse(receipt.exists())
            delete_runtime.assert_called_once_with("websites", "website-sandbox")
            self.assertEqual(kill_calls, ["website-sandbox"])

    def test_gitlab_main_requires_all_paths_and_cleans_up_new_sandbox_on_failure(self):
        gitlab = load_gitlab_launcher()
        kill_calls = []
        sandbox = type(
            "Sandbox",
            (),
            {
                "sandbox_id": "gitlab-sandbox",
                "traffic_access_token": "runtime-only-token",
                "get_host": lambda _self, port: f"{port}-gitlab.example.test",
                "kill": lambda _self: kill_calls.append("gitlab-sandbox"),
            },
        )()

        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / ".gitlab-token"
            receipt = Path(directory) / "services-gitlab.json"
            with (
                patch.object(gitlab, "TOKEN_FILE", token_file),
                patch.object(gitlab, "RECEIPT", receipt),
                patch.object(gitlab, "gitlab_pin", return_value="a" * 40),
                patch.object(gitlab.fl, "load_e2b_key"),
                patch.object(
                    gitlab.fl, "ensure_fleet_template", return_value=IMMUTABLE_FLEET
                ),
                patch.object(
                    gitlab.fl, "reuse_or_create", return_value=(sandbox, True)
                ),
                patch.object(gitlab.fl, "ensure_docker"),
                patch.object(gitlab.fl, "ensure_swap"),
                patch.object(gitlab.fl, "read_runtime", return_value={}),
                patch.object(gitlab, "clone_repo"),
                patch.object(gitlab, "write_fanout"),
                patch.object(gitlab, "compose_up"),
                patch.object(gitlab, "wait_api_ready", return_value=0.1),
                patch.object(
                    gitlab.requests,
                    "get",
                    return_value=type("Response", (), {"status_code": 503})(),
                ),
                patch.object(gitlab.fl, "delete_runtime_section") as delete_runtime,
                self.assertRaisesRegex(RuntimeError, "GitLab authenticated ingress"),
            ):
                gitlab.main()

            self.assertFalse(token_file.exists())
            self.assertFalse(receipt.exists())
            delete_runtime.assert_called_once_with("gitlab", "gitlab-sandbox")
            self.assertEqual(kill_calls, ["gitlab-sandbox"])

    def test_runtime_write_atomically_replaces_complete_owner_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / ".runtime.json"
            runtime_file.write_text('{"websites": {}}\n')
            runtime_file.chmod(0o644)
            real_replace = os.replace
            replacement_observation = {}

            def observe_replace(source, destination):
                replacement_observation["old"] = Path(destination).read_text()
                replacement_observation["new"] = Path(source).read_text()
                replacement_observation["mode"] = stat.S_IMODE(
                    Path(source).stat().st_mode
                )
                real_replace(source, destination)

            with (
                patch.object(fleetlib, "RUNTIME_FILE", runtime_file),
                patch.object(fleetlib.os, "replace", side_effect=observe_replace),
            ):
                fleetlib.write_runtime_section("gitlab", {"private_token": "secret"})

            self.assertEqual(replacement_observation["old"], '{"websites": {}}\n')
            self.assertEqual(replacement_observation["mode"], 0o600)
            self.assertIn('"private_token": "secret"', replacement_observation["new"])
            self.assertEqual(stat.S_IMODE(runtime_file.stat().st_mode), 0o600)
            self.assertEqual(
                runtime_file.read_text(),
                '{\n  "gitlab": {\n    "private_token": "secret"\n  },\n'
                '  "websites": {}\n}\n',
            )

    def test_gitlab_compose_passes_private_token_via_command_environment(self):
        launcher = load_gitlab_launcher()
        secret = "glpat-command-secret"

        class Commands:
            def __init__(self):
                self.calls = []

            def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                return object()

        commands = Commands()
        sandbox = type("Sandbox", (), {"commands": commands})()
        with (
            patch.object(launcher.fl, "poll_cmd", side_effect=["no", "COMPOSE_OK"]),
            patch.object(launcher.time, "sleep"),
        ):
            launcher.compose_up(sandbox, secret)

        command, kwargs = commands.calls[0]
        self.assertNotIn(secret, command)
        self.assertEqual(
            kwargs["envs"],
            {"GITLAB_URL": launcher.gitlab_url(), "GITLAB_PRIVATE_TOKEN": secret},
        )

    def test_gitlab_compose_accepts_success_observed_at_deadline(self):
        """A compose completion during the final sleep must not become a timeout."""
        launcher = load_gitlab_launcher()

        class Commands:
            @staticmethod
            def run(_command, **_kwargs):
                return object()

        sandbox = type("Sandbox", (), {"commands": Commands()})()
        with (
            patch.object(
                launcher.fl,
                "poll_cmd",
                side_effect=["no", "pulling", "COMPOSE_OK"],
            ) as poll_cmd,
            patch.object(launcher.time, "time", side_effect=[0, 899, 900]),
            patch.object(launcher.time, "sleep"),
        ):
            launcher.compose_up(sandbox, "glpat-command-secret")

        self.assertEqual(poll_cmd.call_count, 3)

    def test_gitlab_main_validates_release_lock_before_sandbox_creation(self):
        launcher = load_gitlab_launcher()

        with (
            patch.object(launcher.fl, "load_e2b_key"),
            patch.object(
                launcher,
                "gitlab_pin",
                side_effect=ValueError("release lock invalid: mutable GitLab commit"),
            ),
            patch.object(launcher.fl, "reuse_or_create") as reuse_or_create,
            self.assertRaisesRegex(ValueError, "release lock invalid"),
        ):
            launcher.main()

        reuse_or_create.assert_not_called()

    def test_website_fanout_uses_the_release_locked_image(self):
        websites = load_websites_launcher()
        writes = {}

        class Files:
            def write(self, path, content):
                writes[path] = content

        sandbox = type("Sandbox", (), {"files": Files()})()
        digest = "nginx@sha256:" + ("a" * 64)

        with patch.object(websites.fl, "service_image", return_value=digest):
            websites.write_fanout(sandbox, {"mailhub": 13001})

        compose = writes[f"{websites.REPO_DIR}/docker-compose.fanout.yml"]
        self.assertIn(f"image: {digest}", compose)
        self.assertNotIn("nginx:alpine", compose)

    def test_websites_main_validates_source_pin_before_sandbox_creation(self):
        websites = load_websites_launcher()

        with (
            patch.object(websites.fl, "load_e2b_key"),
            patch.object(
                websites,
                "websites_pin",
                side_effect=ValueError("release lock invalid: mutable websites commit"),
            ),
            patch.object(websites.fl, "reuse_or_create") as reuse_or_create,
            self.assertRaisesRegex(ValueError, "release lock invalid"),
        ):
            websites.main()

        reuse_or_create.assert_not_called()

    def test_gitlab_health_check_passes_private_token_via_command_environment(self):
        launcher = load_gitlab_launcher()
        secret = "glpat-health-secret"
        calls = []

        def poll_command(_sandbox, command, **kwargs):
            calls.append((command, kwargs))
            return "200"

        with patch.object(launcher.fl, "poll_cmd", side_effect=poll_command):
            launcher.wait_api_ready(object(), secret)

        command, kwargs = calls[0]
        self.assertNotIn(secret, command)
        self.assertIn("$GITLAB_PRIVATE_TOKEN", command)
        self.assertEqual(kwargs["envs"], {"GITLAB_PRIVATE_TOKEN": secret})

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
            sandbox, created = fleetlib.reuse_or_create(
                "websites", template=IMMUTABLE_FLEET
            )

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
                "campaign_id": "test-campaign",
            }
        }
        stale_kills = []
        stale_sandbox = type(
            "Sandbox",
            (),
            {"kill": lambda _self: stale_kills.append("old-sandbox")},
        )()
        with (
            patch.object(fleetlib, "read_runtime", return_value=stale),
            patch.object(fleetlib, "_connect", return_value=stale_sandbox),
            patch.object(fleetlib, "delete_runtime_section") as delete_runtime,
            patch.object(fleetlib, "Sandbox", FakeFleetSandbox),
        ):
            sandbox, created = fleetlib.reuse_or_create(
                "websites",
                template=IMMUTABLE_FLEET,
            )

        self.assertTrue(created)
        self.assertEqual(sandbox.template, IMMUTABLE_FLEET)
        self.assertEqual(stale_kills, ["old-sandbox"])
        delete_runtime.assert_called_once_with("websites", "old-sandbox")

    def test_reuse_or_create_refuses_other_campaigns_sandbox(self):
        # runtime records a live sandbox owned by campaign-A; launching campaign-B
        # must refuse loudly — stop.py refuses the same cross-campaign action.
        stale = unittest.mock.MagicMock()
        with (
            patch.object(
                fleetlib,
                "read_runtime",
                return_value={
                    "websites": {
                        "sandbox_id": "sb-a",
                        "template": IMMUTABLE_FLEET,
                        "campaign_id": "campaign-A",
                    }
                },
            ),
            patch.dict(
                os.environ,
                {"OSWORLD_CAMPAIGN_ID": "campaign-B", "FLEET_TEMPLATE": IMMUTABLE_FLEET},
            ),
            patch.object(fleetlib, "_connect", return_value=stale),
            patch.object(fleetlib.Sandbox, "create") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "campaign"):
                fleetlib.reuse_or_create("websites", template=IMMUTABLE_FLEET)
        stale.kill.assert_not_called()
        create.assert_not_called()

    def test_fleet_timeout_default_covers_a_full_campaign(self):
        # Nothing refreshes fleet timeouts mid-run; the default must outlast the
        # longest supported campaign (multi-wave 108-task run with 4h ceilings).
        assert fleetlib.SANDBOX_TIMEOUT_S == 24 * 3600

    def test_rollback_launch_spares_reused_sandboxes(self):
        sbx = unittest.mock.MagicMock(sandbox_id="sb-1")
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("t")
            with (
                patch.object(fleetlib, "stop_host_proxy") as stop_proxy,
                patch.object(fleetlib, "delete_runtime_section") as delete_section,
            ):
                fleetlib.rollback_launch("gitlab", sbx, created=False, token_file=token)
            sbx.kill.assert_not_called()
            assert token.exists()  # a reused fleet keeps its live PAT
        stop_proxy.assert_called_once()
        delete_section.assert_called_once_with("gitlab", "sb-1")

    def test_rollback_launch_destroys_fresh_sandboxes(self):
        sbx = unittest.mock.MagicMock(sandbox_id="sb-1")
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("t")
            with (
                patch.object(fleetlib, "stop_host_proxy"),
                patch.object(fleetlib, "delete_runtime_section"),
            ):
                fleetlib.rollback_launch("gitlab", sbx, created=True, token_file=token)
            sbx.kill.assert_called_once()
            assert not token.exists()

    def test_websites_v2_checkout_honors_osworld_root(self):
        websites = load_websites_launcher()
        with patch.dict(os.environ, {"OSWORLD_ROOT": "/custom/OSWorld-V2"}):
            assert websites.v2_checkout() == Path("/custom/OSWorld-V2")
        os.environ.pop("OSWORLD_ROOT", None)
        assert websites.v2_checkout() == websites.fl.REPO_ROOT / "OSWorld-V2"

    def test_websites_launch_fails_fast_without_checkout(self):
        # The post-build URL check needs the checkout; discovering that AFTER a
        # ~50-minute compose build (and then killing the sandbox) is the bug.
        websites = load_websites_launcher()
        with (
            patch.dict(os.environ, {"OSWORLD_ROOT": "/nonexistent/OSWorld-V2"}),
            patch.object(websites.fl, "reuse_or_create") as reuse_or_create,
        ):
            with self.assertRaises(SystemExit):
                websites.main()
        reuse_or_create.assert_not_called()  # no sandbox spend before the check


if __name__ == "__main__":
    unittest.main()
