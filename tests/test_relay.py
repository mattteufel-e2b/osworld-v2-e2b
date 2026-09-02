from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import make_mocked_request

# The relay ships as a standalone module in the sibling relay/ directory (it runs
# on the OSWorld host next to run.py, not as part of an installed package), so put
# that directory on sys.path and import it by module name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
import relay  # noqa: E402

IMMUTABLE_TEMPLATE = "osworld-v2-gnome:817519a3-6360-475f-bdae-77780743d6a5"
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


class FakeSandbox:
    created = []
    root_capacity_bytes = 100 * 1024**3

    def __init__(self, sandbox_id):
        self.sandbox_id = sandbox_id
        self.traffic_access_token = f"token-{sandbox_id}"
        self.killed = False
        self.timeout_calls = []
        self.commands = self.FakeCommands(self)

    class FakeCommands:
        def __init__(self, sandbox):
            self.sandbox = sandbox
            self.calls = []

        def run(self, command, **kwargs):
            self.calls.append((command, kwargs))

            class Result:
                exit_code = 0
                stderr = ""
                stdout = ""

            result = Result()
            if command == "df -B1 --output=size / | tail -n 1":
                result.stdout = f"{self.sandbox.root_capacity_bytes}\n"
            return result

    @classmethod
    def create(cls, template, **kwargs):
        sandbox = cls(f"sandbox-{len(cls.created) + 1}")
        sandbox.create_template = template
        sandbox.create_kwargs = kwargs
        cls.created.append(sandbox)
        return sandbox

    def get_host(self, port):
        return f"{port}-{self.sandbox_id}.example.test"

    def kill(self):
        self.killed = True

    def set_timeout(self, seconds):
        self.timeout_calls.append(seconds)

    def create_snapshot(self):
        class SnapshotInfo:
            snapshot_id = f"snap-of-{self.sandbox_id}"

        return SnapshotInfo()


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSandbox.created.clear()
        relay.TEMPLATE = IMMUTABLE_TEMPLATE
        self.manager = relay.GuestManager()

    async def test_guest_proxy_install_omits_host_credentials_and_paths(self):
        runtime = {
            "websites": {
                "sandbox_id": "websites-sandbox",
                "template": "fleet:build-id",
                "traffic_token": "websites-traffic-token",
                "host_suffix": "127.0.0.1.nip.io",
                "public_host_suffix": "127.0.0.1.nip.io:8090",
                "caddy_ingress_host": "80-websites.e2b.app",
                "mode": "per-port-fanout",
                "sites": {
                    "mailhub": {
                        "ingress_host": "13001-websites.e2b.app",
                        "port": 13001,
                    }
                },
            },
            "gitlab": {
                "sandbox_id": "gitlab-sandbox",
                "template": "fleet:build-id",
                "traffic_token": "gitlab-traffic-token",
                "host": "gitlab.127.0.0.1.nip.io",
                "ingress_host": "8929-gitlab.e2b.app",
                "port": 8929,
                "url": "http://gitlab.127.0.0.1.nip.io:8090",
                "external_url": "http://gitlab.127.0.0.1.nip.io",
                "private_token": "gitlab-private-token",
                "token_file": "/host/services/.gitlab-token",
            },
        }

        class Files:
            def __init__(self):
                self.writes = {}

            def write(self, path, content):
                self.writes[path] = content

        class Commands:
            def __init__(self):
                self.calls = []

            def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                return type("Result", (), {"stdout": "ok\n"})()

        sandbox = type("Sandbox", (), {"files": Files(), "commands": Commands()})()
        with tempfile.TemporaryDirectory() as directory:
            script_file = Path(directory) / "hostmap_proxy.py"
            runtime_file = Path(directory) / "runtime.json"
            script_file.write_text("# guest proxy")
            runtime_file.write_text(json.dumps(runtime))
            with (
                patch.object(relay, "GUEST_PROXY_SCRIPT", str(script_file)),
                patch.object(relay, "GUEST_FLEET_RULES", str(runtime_file)),
            ):
                relay._install_guest_proxy(sandbox)

        guest_runtime = json.loads(sandbox.files.writes["/opt/fleet_runtime.json"])

        self.assertEqual(
            guest_runtime,
            {
                "websites": {
                    "host_suffix": "127.0.0.1.nip.io",
                    "public_host_suffix": "127.0.0.1.nip.io:8090",
                    "sites": {
                        "mailhub": {
                            "ingress_host": "13001-websites.e2b.app",
                            "port": 13001,
                        }
                    },
                },
                "gitlab": {
                    "host": "gitlab.127.0.0.1.nip.io",
                    "url": "http://gitlab.127.0.0.1.nip.io:8090",
                    "ingress_host": "8929-gitlab.e2b.app",
                    "port": 8929,
                },
            },
        )
        serialized = json.dumps(guest_runtime)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("/host/", serialized)
        self.assertIn(
            "chmod 0700 /opt/hostmap_proxy.py && chmod 0600 /opt/fleet_runtime.json",
            [command for command, _kwargs in sandbox.commands.calls],
        )

    async def test_service_tokens_are_injected_only_for_exact_fleet_ingress_domains(
        self,
    ):
        runtime = {
            "websites": {
                "traffic_token": "websites-traffic-token",
                "sites": {
                    "mailhub": {
                        "ingress_host": "13001-websites.e2b.app",
                        "port": 13001,
                    },
                    "teamchat": {
                        "ingress_host": "13002-websites.e2b.app",
                        "port": 13002,
                    },
                },
            },
            "gitlab": {
                "ingress_host": "8929-gitlab.e2b.app",
                "traffic_token": "gitlab-traffic-token",
            },
        }

        rules = relay._service_network_rules(json.dumps(runtime))

        self.assertEqual(
            rules,
            {
                "13001-websites.e2b.app": [
                    {
                        "transform": {
                            "headers": {
                                "e2b-traffic-access-token": "websites-traffic-token"
                            }
                        }
                    }
                ],
                "13002-websites.e2b.app": [
                    {
                        "transform": {
                            "headers": {
                                "e2b-traffic-access-token": "websites-traffic-token"
                            }
                        }
                    }
                ],
                "8929-gitlab.e2b.app": [
                    {
                        "transform": {
                            "headers": {
                                "e2b-traffic-access-token": "gitlab-traffic-token"
                            }
                        }
                    }
                ],
            },
        )

    async def test_replace_applies_fleet_token_rules_to_guest_network_policy(self):
        runtime = {
            "websites": {
                "traffic_token": "websites-traffic-token",
                "sites": {
                    "mailhub": {"ingress_host": "13001-websites.e2b.app", "port": 13001}
                },
            },
            "gitlab": {
                "ingress_host": "8929-gitlab.e2b.app",
                "traffic_token": "gitlab-traffic-token",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / "runtime.json"
            runtime_file.write_text(json.dumps(runtime))
            with (
                patch.object(relay, "GUEST_FLEET_RULES", str(runtime_file)),
                patch.object(relay, "Sandbox", FakeSandbox),
                patch.object(self.manager, "_wait_ready", AsyncMock()),
            ):
                await self.manager.replace()

        self.assertEqual(
            FakeSandbox.created[0].create_kwargs["network"]["rules"],
            {
                "13001-websites.e2b.app": [
                    {
                        "transform": {
                            "headers": {
                                "e2b-traffic-access-token": "websites-traffic-token"
                            }
                        }
                    }
                ],
                "8929-gitlab.e2b.app": [
                    {
                        "transform": {
                            "headers": {
                                "e2b-traffic-access-token": "gitlab-traffic-token"
                            }
                        }
                    }
                ],
            },
        )

    async def test_replace_uses_restricted_ingress_and_kills_previous_guest(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            first = await self.manager.replace()
            second = await self.manager.replace()

        self.assertEqual(first.generation, 1)
        self.assertEqual(second.generation, 2)
        self.assertNotEqual(first.sandbox_id, second.sandbox_id)
        self.assertTrue(FakeSandbox.created[0].killed)
        self.assertFalse(FakeSandbox.created[1].killed)
        self.assertEqual(
            FakeSandbox.created[1].create_kwargs["network"],
            {
                "allow_public_traffic": False,
                "deny_out": PROTECTED_EGRESS_CIDRS,
            },
        )
        self.assertTrue(FakeSandbox.created[1].create_kwargs["secure"])

    async def test_stop_during_replace_reaps_the_fresh_sandbox(self):
        # /stop (or SIGTERM) can land while a /reset's slow create is in flight;
        # the finished replace must reap its new sandbox instead of installing
        # it into a manager that shutdown already emptied (it would otherwise
        # run unowned until SANDBOX_TIMEOUT_S).
        import asyncio

        from aiohttp import web

        gate = asyncio.Event()

        async def blocked_wait_ready(_guest):
            await gate.wait()

        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", blocked_wait_ready),
        ):
            replace_task = asyncio.create_task(self.manager.replace())
            while not FakeSandbox.created:
                await asyncio.sleep(0.01)
            await self.manager.stop()
            gate.set()
            with self.assertRaises(web.HTTPServiceUnavailable):
                await replace_task

        self.assertTrue(FakeSandbox.created[0].killed)
        with self.assertRaises(web.HTTPServiceUnavailable):
            await self.manager.current()

    async def test_replace_after_stop_is_refused(self):
        from aiohttp import web

        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            await self.manager.stop()
            with self.assertRaises(web.HTTPServiceUnavailable):
                await self.manager.replace()
        # No second sandbox was ever created for the refused replace.
        self.assertEqual(len(FakeSandbox.created), 1)
        self.assertTrue(FakeSandbox.created[0].killed)

    async def test_ingress_token_is_added_but_not_exposed_by_state(self):
        guest = relay.Guest(
            sandbox=FakeSandbox("sandbox-1"),
            sandbox_id="sandbox-1",
            traffic_token="top-secret",
            hosts={5000: "host"},
            generation=1,
        )
        request = make_mocked_request(
            "POST",
            "/execute",
            headers={"Host": "localhost", "Connection": "keep-alive", "X-Test": "yes"},
        )
        headers = relay._upstream_headers(request, guest)
        self.assertEqual(headers["e2b-traffic-access-token"], "top-secret")
        self.assertNotIn("Host", headers)
        self.assertEqual(headers["X-Test"], "yes")
        self.assertNotIn("traffic_token", relay._public_state(guest))
        self.assertNotIn("top-secret", json.dumps(relay._public_state(guest)))

    async def test_saved_snapshot_reverts_to_running_state_source(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            first = await self.manager.replace()
            snapshot_id = await self.manager.save_snapshot("mid_task")
            reverted = await self.manager.revert("mid_task")

        self.assertEqual(snapshot_id, f"snap-of-{first.sandbox_id}")
        self.assertEqual(FakeSandbox.created[1].create_template, snapshot_id)
        self.assertEqual(reverted.source, f"snapshot:{snapshot_id}")
        self.assertEqual(
            FakeSandbox.created[1].create_kwargs["network"],
            {
                "allow_public_traffic": False,
                "deny_out": PROTECTED_EGRESS_CIDRS,
            },
        )

    async def test_heartbeat_refreshes_timeout_only_after_activity(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            guest = await self.manager.replace()
            # A fresh sandbox starts with a full SANDBOX_TIMEOUT_S: no refresh owed.
            self.assertFalse(await self.manager._heartbeat_once())
            self.assertEqual(guest.sandbox.timeout_calls, [])
            # Guest-directed traffic arms the next beat...
            self.manager.touch()
            self.assertTrue(await self.manager._heartbeat_once())
            self.assertEqual(guest.sandbox.timeout_calls, [relay.SANDBOX_TIMEOUT_S])
            # ...and without new traffic the following beat is a no-op, so an
            # abandoned guest still expires within SANDBOX_TIMEOUT_S.
            self.assertFalse(await self.manager._heartbeat_once())
            self.assertEqual(guest.sandbox.timeout_calls, [relay.SANDBOX_TIMEOUT_S])

    async def test_heartbeat_never_refreshes_after_stop(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            guest = await self.manager.replace()
            self.manager.touch()
            await self.manager.stop()
            self.assertFalse(await self.manager._heartbeat_once())
        self.assertEqual(guest.sandbox.timeout_calls, [])

    async def test_save_snapshot_gates_on_resumed_guest_readiness(self):
        # create_snapshot pauses the guest (dropping live CDP sockets); the /save
        # path must wait for the resumed server to answer before returning.
        wait_ready = AsyncMock()
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", wait_ready),
        ):
            guest = await self.manager.replace()
            await self.manager.save_snapshot("mid_task")
        self.assertEqual(wait_ready.await_count, 2)  # replace bring-up + post-resume
        self.assertIs(wait_ready.await_args.args[0], guest)

    async def test_state_exposes_persistent_snapshot_ids(self):
        # Snapshots persist in the E2B account past the run (never auto-deleted);
        # consumers track the ids from /state (and the /save response).
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
            patch.object(relay, "manager", self.manager),
        ):
            guest = await self.manager.replace()
            snapshot_id = await self.manager.save_snapshot("mid_task")
            state = relay._public_state(guest)
        self.assertEqual(state["snapshots"], {"mid_task": snapshot_id})
        self.assertEqual(state["heartbeat_interval_seconds"], relay.HEARTBEAT_INTERVAL_S)

    async def test_stop_endpoint_initiates_shutdown_without_a_guest(self):
        import asyncio
        import json

        event = asyncio.Event()
        with (
            patch.object(relay, "manager", self.manager),
            patch.object(relay, "stop_event", event),
        ):
            response = await relay.stop(make_mocked_request("POST", "/stop"))
        self.assertTrue(event.is_set())
        self.assertEqual(json.loads(response.text), {"stopping": True, "sandbox_id": None})

    async def test_ws_connect_retries_until_upstream_resumes(self):
        class FlakySession:
            attempts = 0

            async def ws_connect(self, url, headers=None, max_msg_size=0):
                FlakySession.attempts += 1
                if FlakySession.attempts <= 2:
                    raise ConnectionError("guest still resuming")
                return "upstream-ws"

        with (
            patch.object(relay, "WS_CONNECT_RETRY_S", 30),
            patch.object(relay.asyncio, "sleep", AsyncMock()),
        ):
            result = await relay._ws_connect_with_retry(FlakySession(), "wss://guest", {})
        self.assertEqual(result, "upstream-ws")
        self.assertEqual(FlakySession.attempts, 3)

    async def test_ws_connect_gives_up_after_retry_window(self):
        class DeadSession:
            async def ws_connect(self, url, headers=None, max_msg_size=0):
                raise ConnectionError("upstream down")

        with (
            patch.object(relay, "WS_CONNECT_RETRY_S", 0),
            self.assertRaisesRegex(ConnectionError, "upstream down"),
        ):
            await relay._ws_connect_with_retry(DeadSession(), "wss://guest", {})

    async def test_mutable_guest_template_is_rejected_before_create(self):
        relay.TEMPLATE = "osworld-v2-gnome"
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
            self.assertRaisesRegex(ValueError, "immutable name:build_id"),
        ):
            await self.manager.replace()

        self.assertEqual(FakeSandbox.created, [])

    async def test_unknown_snapshot_name_falls_back_to_template(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            reverted = await self.manager.revert("init_state")

        self.assertEqual(FakeSandbox.created[1].create_template, relay.TEMPLATE)
        self.assertEqual(reverted.source, "template")

    async def test_volume_check_accepts_exact_requested_root_capacity(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            result = await self.manager.check_volume(100)

        self.assertEqual(result["requested_gb"], 100)
        self.assertEqual(result["required_bytes"], 100_000_000_000)
        self.assertEqual(result["root_capacity_bytes"], 100 * 1024**3)
        marker_calls = [
            call
            for call in FakeSandbox.created[0].commands.calls
            if "/run/osworld-required-volume-gb" in call[0]
        ]
        self.assertEqual(len(marker_calls), 1)
        self.assertEqual(marker_calls[0][1]["user"], "root")

    async def test_volume_check_rejects_undersized_root_before_task_setup(self):
        FakeSandbox.root_capacity_bytes = 99_999_999_999
        self.addCleanup(setattr, FakeSandbox, "root_capacity_bytes", 100 * 1024**3)
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            with self.assertRaisesRegex(RuntimeError, "requires 100 GB"):
                await self.manager.check_volume(100)

    async def test_volume_check_rejects_non_positive_request(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            await self.manager.check_volume(0)

    async def test_cdp_discovery_is_rewritten_to_local_relay(self):
        payload = b'{"webSocketDebuggerUrl":"ws://upstream/devtools/browser/1","host":"upstream"}'
        rewritten = relay._rewrite_cdp_host(payload, 19222).decode()
        self.assertIn("ws://127.0.0.1:19222/devtools/browser/1", rewritten)
        self.assertIn('"host": "127.0.0.1:19222"', rewritten)

    def test_port_map_exposes_default_v2_task_service_ports_as_literals(self):
        # The default remains byte-compatible for single-worker callers.
        self.assertEqual(relay.PORT_MAP[3000], 3000)
        self.assertEqual(relay.PORT_MAP[8000], 8000)
        self.assertIn(3000, relay.PORT_MAP.values())
        self.assertIn(8000, relay.PORT_MAP.values())
        # The control/CDP/VLC channels stay remapped so they don't collide with any
        # real local service on 5000/9222/8080.
        self.assertEqual(relay.PORT_MAP[15000], 5000)
        self.assertEqual(relay.PORT_MAP[19222], 9222)
        self.assertEqual(relay.PORT_MAP[18080], 8080)


class PortNamespacingTests(unittest.TestCase):
    """OSWORLD_RELAY_PORT_BASE lets many relays run at once (one per parallel
    rollout worker) on disjoint loopback port blocks. Default (unset / 0) must
    reproduce today's single-instance addresses byte-for-byte."""

    @staticmethod
    def _reload_with_base(base_value, task_service_ports=None):
        env = dict(os.environ)
        env.pop("OSWORLD_RELAY_PORT_BASE", None)
        env.pop("E2B_RELAY_CONTROL_PORT", None)
        env.pop("OSWORLD_TASK_SERVICE_PORTS", None)
        if base_value is not None:
            env["OSWORLD_RELAY_PORT_BASE"] = str(base_value)
        if task_service_ports is not None:
            env["OSWORLD_TASK_SERVICE_PORTS"] = task_service_ports
        with patch.dict(os.environ, env, clear=True):
            return importlib.reload(relay)

    def tearDown(self):
        # Restore the module to its unset-base default for every other test.
        self._reload_with_base(None)

    def test_default_base_is_byte_compatible_with_single_instance(self):
        mod = self._reload_with_base(None)
        self.assertEqual(mod.PORT_BASE, 0)
        self.assertEqual(mod.CONTROL_PORT, 14999)
        self.assertEqual(mod.SERVER_LOCAL, 15000)
        self.assertEqual(mod.CDP_LOCAL, 19222)
        self.assertEqual(mod.VLC_LOCAL, 18080)
        self.assertEqual(
            mod.PORT_MAP,
            {15000: 5000, 19222: 9222, 18080: 8080, 3000: 3000, 8000: 8000},
        )

    def test_nonzero_base_shifts_remapped_ports_but_not_default_literals(self):
        base = 100
        mod = self._reload_with_base(base)
        self.assertEqual(mod.PORT_BASE, base)
        # Control + remapped control/CDP/VLC channels all shift by the base so a
        # second worker never collides with the first.
        self.assertEqual(mod.CONTROL_PORT, 14999 + base)
        self.assertEqual(mod.SERVER_LOCAL, 15000 + base)
        self.assertEqual(mod.CDP_LOCAL, 19222 + base)
        self.assertEqual(mod.VLC_LOCAL, 18080 + base)
        self.assertEqual(mod.PORT_MAP[15000 + base], 5000)
        self.assertEqual(mod.PORT_MAP[19222 + base], 9222)
        self.assertEqual(mod.PORT_MAP[18080 + base], 8080)
        # Literal task-service ports keep their literal numbers regardless of base
        # (task code dials them directly on the guest IP), and are declared as the
        # best-effort set that main() binds only when free.
        self.assertEqual(mod.PORT_MAP[3000], 3000)
        self.assertEqual(mod.PORT_MAP[8000], 8000)
        self.assertEqual(mod.LITERAL_PORTS, {3000, 8000})

    def test_task_service_mapping_can_namespace_host_port_from_guest_port(self):
        # A task may keep using localhost:3000 inside its guest while its host-side
        # setup/evaluator dials a collision-free relay listener.
        mod = self._reload_with_base(100, task_service_ports="43000:3000")
        self.assertEqual(mod.PORT_MAP[43000], 3000)
        self.assertEqual(mod.LITERAL_PORTS, {43000})
        self.assertNotIn(3000, mod.PORT_MAP)

    def test_task_service_mapping_rejects_duplicate_local_ports(self):
        with self.assertRaisesRegex(ValueError, "duplicate local task-service port"):
            self._reload_with_base(100, task_service_ports="43000:3000,43000:8000")

    def test_empty_task_service_ports_disables_literal_listeners(self):
        # Literal loopback ports cannot be namespaced per worker (task code
        # dials vm_ip=127.0.0.1 at the literal number), so parallel waves set
        # OSWORLD_TASK_SERVICE_PORTS="" and task-service dials fail loudly
        # instead of silently reaching another worker's guest.
        mod = self._reload_with_base(100, task_service_ports="")
        self.assertEqual(mod.LITERAL_PORTS, set())
        self.assertNotIn(3000, mod.PORT_MAP)
        self.assertNotIn(8000, mod.PORT_MAP)
        self.assertEqual(set(mod.PORT_MAP.values()), {5000, 9222, 8080})

    def test_two_distinct_bases_produce_disjoint_port_blocks(self):
        worker0 = set(self._reload_with_base(0).PORT_MAP) | {self._reload_with_base(0).CONTROL_PORT}
        remapped0 = {p for p in worker0 if p not in (3000, 8000)}
        worker50 = set(self._reload_with_base(50).PORT_MAP) | {
            self._reload_with_base(50).CONTROL_PORT
        }
        remapped50 = {p for p in worker50 if p not in (3000, 8000)}
        self.assertTrue(remapped0.isdisjoint(remapped50))


if __name__ == "__main__":
    unittest.main()
