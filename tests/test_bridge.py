from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp.test_utils import make_mocked_request
from e2b import InvalidArgumentException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "provider"))

import bridge  # noqa: E402

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


class BridgeConfigTests(unittest.TestCase):
    def test_from_env_reads_every_knob_and_validates_template_and_campaign(self):
        env = {
            "GUEST_TEMPLATE": IMMUTABLE_TEMPLATE,
            "OSWORLD_CAMPAIGN_ID": "camp-1",
            "SANDBOX_TIMEOUT_S": "1200",
            "GUEST_READY_TIMEOUT_S": "30",
            "RELAY_HTTP_TIMEOUT_S": "99",
            "SANDBOX_HEARTBEAT_INTERVAL_S": "1000",
            "OSWORLD_TASK_SERVICE_PORTS": "3000:3000",
            "OSWORLD_RETAIN_SNAPSHOTS": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            config = bridge.BridgeConfig.from_env()
        self.assertEqual(config.template, IMMUTABLE_TEMPLATE)
        self.assertEqual(config.campaign_id, "camp-1")
        self.assertEqual(config.sandbox_timeout_s, 1200)
        self.assertEqual(config.ready_timeout_s, 30)
        self.assertEqual(config.http_timeout_s, 99)
        # clamped to sandbox_timeout_s // 4
        self.assertEqual(config.heartbeat_interval_s, 300)
        self.assertEqual(config.task_service_ports, "3000:3000")
        self.assertTrue(config.retain_snapshots)

    def test_from_env_defaults_task_service_ports_to_empty(self):
        env = {"GUEST_TEMPLATE": IMMUTABLE_TEMPLATE, "OSWORLD_CAMPAIGN_ID": "c"}
        with patch.dict(os.environ, env, clear=True):
            config = bridge.BridgeConfig.from_env()
        self.assertEqual(config.task_service_ports, "")
        self.assertEqual(bridge.parse_task_service_ports(config.task_service_ports), {})

    def test_from_env_rejects_mutable_template(self):
        env = {"GUEST_TEMPLATE": "osworld-v2-gnome", "OSWORLD_CAMPAIGN_ID": "c"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "immutable name:build_id"):
                bridge.BridgeConfig.from_env()

    def test_explicit_template_argument_wins_over_env(self):
        other = "osworld-v2-gnome:11111111-2222-3333-4444-555555555555"
        env = {"GUEST_TEMPLATE": IMMUTABLE_TEMPLATE, "OSWORLD_CAMPAIGN_ID": "c"}
        with patch.dict(os.environ, env, clear=True):
            config = bridge.BridgeConfig.from_env(template=other)
        self.assertEqual(config.template, other)


class TaskServicePortTests(unittest.TestCase):
    def test_literal_and_mapped_entries(self):
        self.assertEqual(
            bridge.parse_task_service_ports("3000,13001:8000"),
            {3000: 3000, 13001: 8000},
        )

    def test_rejects_duplicates_and_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            bridge.parse_task_service_ports("3000,3000:8000")
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            bridge.parse_task_service_ports("70000")


class FakeSandbox:
    created = []
    deleted_snapshots = []
    root_capacity_bytes = 100 * 1024**3
    # Tests that need a slow create set this to a threading.Event; create()
    # blocks on it until the test releases it.
    create_gate = None

    def __init__(self, sandbox_id):
        self.sandbox_id = sandbox_id
        self.traffic_access_token = f"token-{sandbox_id}"
        self.killed = False
        self.timeout_calls = []
        self.commands = self.FakeCommands(self)
        self.files = self.FakeFiles()

    class FakeFiles:
        """Records the E2B native file calls the setup-upload path makes."""

        def __init__(self):
            self.entries = {}
            self.writes = []
            self.renames = []
            self.removals = []

        def write(self, path, data, **kwargs):
            payload = data.read()
            self.writes.append((path, payload, kwargs))
            self.entries[path] = payload

        def rename(self, old_path, new_path, **kwargs):
            self.renames.append((old_path, new_path, kwargs))
            self.entries[new_path] = self.entries.pop(old_path)

        def remove(self, path, **kwargs):
            self.removals.append((path, kwargs))
            self.entries.pop(path, None)

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
        if cls.create_gate is not None:
            cls.create_gate.wait(timeout=10)
        sandbox = cls(f"sandbox-{len(cls.created) + 1}")
        sandbox.create_template = template
        sandbox.create_kwargs = kwargs
        cls.created.append(sandbox)
        return sandbox

    @classmethod
    def delete_snapshot(cls, snapshot_id):
        cls.deleted_snapshots.append(snapshot_id)

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


class GuestManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSandbox.created.clear()
        FakeSandbox.deleted_snapshots.clear()
        self.config = bridge.BridgeConfig(
            template=IMMUTABLE_TEMPLATE, campaign_id="test-campaign"
        )
        self.guest_ports = frozenset({5000, 9222, 8080})
        self.manager = bridge.GuestManager(self.config, self.guest_ports)
        self.sandbox_patch = patch.object(bridge, "Sandbox", FakeSandbox)
        self.sandbox_patch.start()

    async def asyncTearDown(self):
        self.sandbox_patch.stop()

    @staticmethod
    def _upload_request(
        destination="/home/user/Desktop/input.bin", data=b"file-payload"
    ):
        class Part:
            def __init__(self, name, payload):
                self.name = name
                self.payload = payload
                self.offset = 0

            async def text(self):
                return self.payload.decode()

            async def read_chunk(self, size=8192):
                chunk = self.payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class Reader:
            def __init__(self):
                self.parts = iter(
                    [
                        Part("file_path", destination.encode()),
                        Part("file_data", data),
                    ]
                )

            async def next(self):
                return next(self.parts, None)

        class Request:
            method = "POST"
            path = "/setup/upload"
            rel_url = "/setup/upload"
            headers = {}

            async def multipart(self):
                return Reader()

            async def read(self):
                return b"proxied-upload-body"

        return Request()

    async def test_guest_proxy_install_keeps_only_required_fleet_credentials(self):
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
            config = bridge.BridgeConfig(
                template=IMMUTABLE_TEMPLATE,
                campaign_id="test-campaign",
                guest_proxy_script=str(script_file),
                fleet_rules=str(runtime_file),
            )
            bridge._install_guest_proxy(sandbox, config)

        guest_runtime = json.loads(sandbox.files.writes["/opt/fleet_runtime.json"])

        self.assertEqual(
            guest_runtime,
            {
                "websites": {
                    "traffic_token": "websites-traffic-token",
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
                    "traffic_token": "gitlab-traffic-token",
                    "host": "gitlab.127.0.0.1.nip.io",
                    "url": "http://gitlab.127.0.0.1.nip.io:8090",
                    "ingress_host": "8929-gitlab.e2b.app",
                    "port": 8929,
                },
            },
        )
        serialized = json.dumps(guest_runtime)
        self.assertNotIn("private_token", serialized)
        self.assertNotIn("token_file", serialized)
        self.assertNotIn("/host/", serialized)
        self.assertIn(
            "chmod 0700 /opt/hostmap_proxy.py && chmod 0600 /opt/fleet_runtime.json",
            [command for command, _kwargs in sandbox.commands.calls],
        )

    async def test_guest_proxy_gets_tls_material_trust_install_and_alias_hosts(self):
        runtime = {
            "websites": {
                "sandbox_id": "websites-sandbox",
                "template": "fleet:build-id",
                "traffic_token": "websites-traffic-token",
                "host_suffix": "127.0.0.1.nip.io",
                "public_host_suffix": "127.0.0.1.nip.io:8090",
                "scheme": "https",
                "caddy_ingress_host": "80-websites.e2b.app",
                "mode": "per-port-fanout",
                "asset_url_map": {
                    "http://dead-upstream.example/logo.png": (
                        "https://mailhub.127.0.0.1.nip.io:8090/logo.png"
                    )
                },
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
                "url": "https://gitlab.127.0.0.1.nip.io:8090",
                "external_url": "https://gitlab.127.0.0.1.nip.io",
                "scheme": "https",
                "aliases": ["54.174.16.65.sslip.io"],
                "private_token": "gitlab-private-token",
                "token_file": "/host/services/.gitlab-token",
            },
            "tls": {
                "campaign_id": "test-campaign",
                "hosts": [
                    "mailhub.127.0.0.1.nip.io",
                    "gitlab.127.0.0.1.nip.io",
                    "54.174.16.65.sslip.io",
                ],
            },
        }

        class Files:
            def __init__(self):
                self.writes = {}
                self.write_kwargs = {}

            def write(self, path, content, **kwargs):
                self.writes[path] = content
                self.write_kwargs[path] = kwargs

        class Commands:
            def __init__(self):
                self.calls = []

            def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                # "ok" means the nip.io DNS probe succeeds, so the probe's own
                # /etc/hosts fallback branch never fires -- this proves the
                # alias entry is written unconditionally, not from that branch.
                return type("Result", (), {"stdout": "ok\n"})()

        sandbox = type("Sandbox", (), {"files": Files(), "commands": Commands()})()
        with tempfile.TemporaryDirectory() as directory:
            script_file = Path(directory) / "hostmap_proxy.py"
            runtime_file = Path(directory) / "runtime.json"
            ca_cert_file = Path(directory) / "ca.crt"
            leaf_cert_file = Path(directory) / "leaf.crt"
            leaf_key_file = Path(directory) / "leaf.key"
            bundle_file = Path(directory) / "bundle.crt"
            ca_cert_file.write_text("CACERT")
            leaf_cert_file.write_text("LEAFCERT")
            leaf_key_file.write_text("LEAFKEY")
            bundle_file.write_text("BUNDLE")
            runtime["tls"].update(
                {
                    "ca_cert": str(ca_cert_file),
                    "leaf_cert": str(leaf_cert_file),
                    "leaf_key": str(leaf_key_file),
                    "bundle": str(bundle_file),
                }
            )
            script_file.write_text("# guest proxy")
            runtime_file.write_text(json.dumps(runtime))
            config = bridge.BridgeConfig(
                template=IMMUTABLE_TEMPLATE,
                campaign_id="test-campaign",
                guest_proxy_script=str(script_file),
                fleet_rules=str(runtime_file),
            )
            bridge._install_guest_proxy(sandbox, config)

        writes = sandbox.files.writes
        calls = [command for command, _kwargs in sandbox.commands.calls]

        self.assertEqual(writes["/opt/hostmap-tls/leaf.key"], "LEAFKEY")
        self.assertEqual(writes["/opt/hostmap-tls/leaf.crt"], "LEAFCERT")
        self.assertEqual(writes["/opt/hostmap-tls/ca.crt"], "CACERT")
        self.assertNotIn("/opt/hostmap-tls/ca.key", writes)
        self.assertFalse(any("CAKEY" in str(value) for value in writes.values()))
        # The TLS material is uploaded as root, not the SDK's default user, so
        # there is no window where the agent-controlled `user` account owns
        # the leaf private key.
        for tls_path in (
            "/opt/hostmap-tls/leaf.key",
            "/opt/hostmap-tls/leaf.crt",
            "/opt/hostmap-tls/ca.crt",
        ):
            self.assertEqual(sandbox.files.write_kwargs[tls_path].get("user"), "root")
        trust_install = [c for c in calls if "update-ca-certificates" in c][0]
        self.assertIn(
            "chown root:root /opt/hostmap-tls /opt/hostmap-tls/leaf.crt "
            "/opt/hostmap-tls/leaf.key /opt/hostmap-tls/ca.crt",
            trust_install,
        )
        # chown must land before the chmod that locks the key down to 0600 --
        # ownership established after the mode narrows would leave a window
        # where a non-root-owned file already carries a "secure" mode.
        self.assertLess(
            trust_install.index("chown root:root"),
            trust_install.index("chmod 0600 /opt/hostmap-tls/leaf.key"),
        )
        self.assertTrue(any("chmod 0600 /opt/hostmap-tls/leaf.key" in c for c in calls))
        self.assertTrue(any("update-ca-certificates" in c for c in calls))
        self.assertTrue(
            any(
                'certutil -d sql:/home/user/.pki/nssdb -A -t "C,," -n osworld-campaign'
                in c
                for c in calls
            )
        )
        self.assertTrue(
            any("127.0.0.1 54.174.16.65.sslip.io" in c for c in calls)
        )  # unconditional alias entry, even though the DNS probe said "ok"
        start = [c for c in calls if "python3 /opt/hostmap_proxy.py" in c][0]
        self.assertIn("HOSTMAP_PORT=80,443,8090", start)
        self.assertIn("HOSTMAP_TLS_PORTS=443,8090", start)
        self.assertIn(
            "HOSTMAP_TLS_CERT=/opt/hostmap-tls/leaf.crt HOSTMAP_TLS_KEY=/opt/hostmap-tls/leaf.key",
            start,
        )
        guest_runtime = json.loads(writes["/opt/fleet_runtime.json"])
        self.assertEqual(guest_runtime["gitlab"]["aliases"], ["54.174.16.65.sslip.io"])
        self.assertTrue(guest_runtime["websites"]["asset_url_map"])
        serialized = json.dumps(guest_runtime)
        self.assertNotIn("private_token", serialized)
        self.assertNotIn("token_file", serialized)
        self.assertNotIn("/host/", serialized)

    async def test_guest_proxy_certutil_failure_names_the_missing_template_dependency(
        self,
    ):
        """certutil ships in libnss3-tools, which this repo's template does
        not install, and nothing creates /home/user/.pki/nssdb. When that
        command fails, the resulting error must name both -- so a reader
        knows the guest template is missing a dependency, not that TLS
        itself is broken -- and it must still propagate (no swallowing, no
        falling back to plain HTTP)."""
        runtime = {
            "websites": {
                "traffic_token": "websites-traffic-token",
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
                "traffic_token": "gitlab-traffic-token",
                "host": "gitlab.127.0.0.1.nip.io",
                "ingress_host": "8929-gitlab.e2b.app",
                "port": 8929,
            },
            "tls": {
                "campaign_id": "test-campaign",
                "hosts": ["mailhub.127.0.0.1.nip.io"],
            },
        }

        class Files:
            def __init__(self):
                self.writes = {}

            def write(self, path, content, **kwargs):
                self.writes[path] = content

        class CertutilMissing(Exception):
            pass

        class Commands:
            def __init__(self):
                self.calls = []

            def run(self, command, **kwargs):
                self.calls.append(command)
                if "certutil" in command:
                    raise CertutilMissing("exit status 127: certutil: not found")
                return type("Result", (), {"stdout": "ok\n"})()

        sandbox = type("Sandbox", (), {"files": Files(), "commands": Commands()})()
        with tempfile.TemporaryDirectory() as directory:
            script_file = Path(directory) / "hostmap_proxy.py"
            runtime_file = Path(directory) / "runtime.json"
            ca_cert_file = Path(directory) / "ca.crt"
            leaf_cert_file = Path(directory) / "leaf.crt"
            leaf_key_file = Path(directory) / "leaf.key"
            ca_cert_file.write_text("CACERT")
            leaf_cert_file.write_text("LEAFCERT")
            leaf_key_file.write_text("LEAFKEY")
            runtime["tls"].update(
                {
                    "ca_cert": str(ca_cert_file),
                    "leaf_cert": str(leaf_cert_file),
                    "leaf_key": str(leaf_key_file),
                }
            )
            script_file.write_text("# guest proxy")
            runtime_file.write_text(json.dumps(runtime))
            config = bridge.BridgeConfig(
                template=IMMUTABLE_TEMPLATE,
                campaign_id="test-campaign",
                guest_proxy_script=str(script_file),
                fleet_rules=str(runtime_file),
            )
            with self.assertRaises(RuntimeError) as ctx:
                bridge._install_guest_proxy(sandbox, config)

        message = str(ctx.exception)
        self.assertIn("libnss3-tools", message)
        self.assertIn("/home/user/.pki/nssdb", message)
        self.assertIn("GUEST_TEMPLATE", message)
        self.assertIsInstance(ctx.exception.__cause__, CertutilMissing)

    async def test_guest_proxy_without_tls_section_behaves_as_before(self):
        runtime = {
            "websites": {
                "traffic_token": "websites-traffic-token",
                "host_suffix": "127.0.0.1.nip.io",
                "sites": {
                    "mailhub": {
                        "ingress_host": "13001-websites.e2b.app",
                        "port": 13001,
                    }
                },
            },
            "gitlab": {
                "traffic_token": "gitlab-traffic-token",
                "host": "gitlab.127.0.0.1.nip.io",
                "ingress_host": "8929-gitlab.e2b.app",
                "port": 8929,
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
                return type("Result", (), {"stdout": "no\n"})()

        sandbox = type("Sandbox", (), {"files": Files(), "commands": Commands()})()
        with tempfile.TemporaryDirectory() as directory:
            script_file = Path(directory) / "hostmap_proxy.py"
            runtime_file = Path(directory) / "runtime.json"
            script_file.write_text("# guest proxy")
            runtime_file.write_text(json.dumps(runtime))
            config = bridge.BridgeConfig(
                template=IMMUTABLE_TEMPLATE,
                campaign_id="test-campaign",
                guest_proxy_script=str(script_file),
                fleet_rules=str(runtime_file),
            )
            bridge._install_guest_proxy(sandbox, config)

        writes = sandbox.files.writes
        calls = [command for command, _kwargs in sandbox.commands.calls]

        self.assertFalse(any(path.startswith("/opt/hostmap-tls/") for path in writes))
        self.assertFalse(any("certutil" in c for c in calls))
        self.assertFalse(any("update-ca-certificates" in c for c in calls))
        self.assertFalse(any("54.174.16.65.sslip.io" in c for c in calls))
        start = [c for c in calls if "python3 /opt/hostmap_proxy.py" in c][0]
        self.assertIn("HOSTMAP_PORT=80,8090", start)
        self.assertNotIn("HOSTMAP_TLS_PORTS", start)
        self.assertNotIn("HOSTMAP_TLS_CERT", start)

    async def test_replace_does_not_expand_fleet_routes_into_guest_network_rules(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()

        network = FakeSandbox.created[0].create_kwargs["network"]
        self.assertNotIn("rules", network)
        self.assertFalse(network["allow_public_traffic"])

    async def test_replace_uses_restricted_ingress_and_kills_previous_guest(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
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
        # run unowned until sandbox_timeout_s).
        gate = asyncio.Event()

        async def blocked_wait_ready(_guest):
            await gate.wait()

        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", blocked_wait_ready),
        ):
            replace_task = asyncio.create_task(self.manager.replace())
            while not FakeSandbox.created:
                await asyncio.sleep(0.01)
            await self.manager.stop()
            gate.set()
            with self.assertRaises(bridge.GuestUnavailable):
                await replace_task

        self.assertTrue(FakeSandbox.created[0].killed)
        with self.assertRaises(bridge.GuestUnavailable):
            await self.manager.current()

    async def test_replace_after_stop_is_refused(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            await self.manager.stop()
            with self.assertRaises(bridge.GuestUnavailable):
                await self.manager.replace()
        # No second sandbox was ever created for the refused replace.
        self.assertEqual(len(FakeSandbox.created), 1)
        self.assertTrue(FakeSandbox.created[0].killed)

    async def test_ingress_token_is_added_but_not_exposed_by_state(self):
        guest = bridge.Guest(
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
        headers = bridge._upstream_headers(request, guest)
        self.assertEqual(headers["e2b-traffic-access-token"], "top-secret")
        self.assertNotIn("Host", headers)
        self.assertEqual(headers["X-Test"], "yes")
        self.assertNotIn("traffic_token", self.manager.public_state(guest))
        self.assertNotIn("top-secret", json.dumps(self.manager.public_state(guest)))

    async def test_setup_upload_retries_transient_transport_then_renames(self):
        attempts = 0
        renames = []

        class Files:
            def __init__(self):
                self.entries = {}

            def write(self, path, data, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    self.entries[path] = b"partial"
                    raise aiohttp.ClientConnectionError("connection reset")
                self.entries[path] = data.read()

            def rename(self, old_path, new_path, **_kwargs):
                renames.append((old_path, new_path))
                self.entries[new_path] = self.entries.pop(old_path)

            def remove(self, path, **_kwargs):
                self.entries.pop(path, None)

        files = Files()
        guest = bridge.Guest(
            sandbox=type("Sandbox", (), {"files": files})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={},
            generation=1,
        )

        with patch.object(bridge.asyncio, "sleep", AsyncMock()) as retry_sleep:
            response = await bridge._direct_setup_upload(
                self._upload_request(), guest, self.config
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(attempts, 3)
        self.assertEqual(retry_sleep.await_count, 2)
        self.assertEqual(len(renames), 1)
        self.assertEqual(
            files.entries, {"/home/user/Desktop/input.bin": b"file-payload"}
        )

    async def test_setup_upload_permanent_sdk_failure_is_not_retried_and_cleans_staging(
        self,
    ):
        writes = []
        removals = []

        class Files:
            def __init__(self):
                self.entries = {"/home/user/Desktop/input.bin": b"original"}

            def write(self, path, _data, **_kwargs):
                writes.append(path)
                self.entries[path] = b"partial"
                raise InvalidArgumentException("invalid destination")

            def remove(self, path, **_kwargs):
                removals.append(path)
                self.entries.pop(path, None)

        files = Files()
        guest = bridge.Guest(
            sandbox=type("Sandbox", (), {"files": files})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={},
            generation=1,
        )

        retry_sleep = AsyncMock()
        with (
            patch.object(bridge.asyncio, "sleep", retry_sleep),
            self.assertRaises(bridge.web.HTTPBadGateway),
        ):
            await bridge._direct_setup_upload(
                self._upload_request(), guest, self.config
            )

        self.assertEqual(len(writes), 1)
        self.assertEqual(removals, [writes[0]])
        self.assertEqual(files.entries, {"/home/user/Desktop/input.bin": b"original"})
        self.assertEqual(retry_sleep.await_count, 0)

    async def test_saved_snapshot_reverts_to_running_state_source(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
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
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            guest = await self.manager.replace()
            # A fresh sandbox starts with a full sandbox_timeout_s: no refresh owed.
            self.assertFalse(await self.manager._heartbeat_once())
            self.assertEqual(guest.sandbox.timeout_calls, [])
            # Guest-directed traffic arms the next beat...
            self.manager.touch()
            self.assertTrue(await self.manager._heartbeat_once())
            self.assertEqual(
                guest.sandbox.timeout_calls, [self.config.sandbox_timeout_s]
            )
            # ...and without new traffic the following beat is a no-op, so an
            # abandoned guest still expires within sandbox_timeout_s.
            self.assertFalse(await self.manager._heartbeat_once())
            self.assertEqual(
                guest.sandbox.timeout_calls, [self.config.sandbox_timeout_s]
            )

    async def test_heartbeat_retries_transient_control_plane_failure(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
            patch.object(bridge.asyncio, "sleep", AsyncMock()) as retry_sleep,
        ):
            guest = await self.manager.replace()
            attempts = 0

            def flaky_set_timeout(seconds):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise ConnectionError("transient control-plane failure")
                guest.sandbox.timeout_calls.append(seconds)

            guest.sandbox.set_timeout = flaky_set_timeout
            self.manager.touch()

            self.assertTrue(await self.manager._heartbeat_once())
            self.assertFalse(await self.manager._heartbeat_once())

        self.assertEqual(attempts, 3)
        self.assertEqual(guest.sandbox.timeout_calls, [self.config.sandbox_timeout_s])
        self.assertEqual(retry_sleep.await_count, 2)
        self.assertTrue(
            all(0 <= call.args[0] <= 4 for call in retry_sleep.await_args_list)
        )

    async def test_heartbeat_final_failure_keeps_activity_pending_for_later_retry(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
            patch.object(bridge.asyncio, "sleep", AsyncMock()) as retry_sleep,
        ):
            guest = await self.manager.replace()
            attempts = 0

            def failed_set_timeout(_seconds):
                nonlocal attempts
                attempts += 1
                raise ConnectionError("control plane unavailable")

            guest.sandbox.set_timeout = failed_set_timeout
            self.manager.touch()
            refresh_before_failure = self.manager._last_refresh

            self.assertFalse(await self.manager._heartbeat_once())
            self.assertEqual(self.manager._last_refresh, refresh_before_failure)
            self.assertEqual(attempts, 3)
            self.assertEqual(retry_sleep.await_count, 2)

            guest.sandbox.set_timeout = guest.sandbox.timeout_calls.append
            self.assertTrue(await self.manager._heartbeat_once())

        self.assertEqual(guest.sandbox.timeout_calls, [self.config.sandbox_timeout_s])

    async def test_heartbeat_does_not_consume_replacement_guest_activity(self):
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            first = await self.manager.replace()

            def blocked_set_timeout(seconds):
                refresh_started.set()
                release_refresh.wait(timeout=2)
                first.sandbox.timeout_calls.append(seconds)

            first.sandbox.set_timeout = blocked_set_timeout
            self.manager.touch()
            heartbeat = asyncio.create_task(self.manager._heartbeat_once())
            self.assertTrue(await asyncio.to_thread(refresh_started.wait, 2))

            second = await self.manager.replace()
            self.manager.touch()
            release_refresh.set()

            self.assertFalse(await heartbeat)
            self.assertTrue(await self.manager._heartbeat_once())

        self.assertEqual(first.sandbox.timeout_calls, [self.config.sandbox_timeout_s])
        self.assertEqual(second.sandbox.timeout_calls, [self.config.sandbox_timeout_s])

    async def test_heartbeat_never_refreshes_after_stop(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
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
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", wait_ready),
        ):
            guest = await self.manager.replace()
            await self.manager.save_snapshot("mid_task")
        self.assertEqual(wait_ready.await_count, 2)  # replace bring-up + post-resume
        self.assertIs(wait_ready.await_args.args[0], guest)

    async def test_state_exposes_persistent_snapshot_ids(self):
        # Snapshots persist in the E2B account past the run (never auto-deleted);
        # consumers track the ids from public_state (and the /save response).
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            guest = await self.manager.replace()
            snapshot_id = await self.manager.save_snapshot("mid_task")
            state = self.manager.public_state(guest)
        self.assertEqual(state["snapshots"], {"mid_task": snapshot_id})
        self.assertEqual(
            state["heartbeat_interval_seconds"], self.config.heartbeat_interval_s
        )

    async def test_stop_kills_guest_and_deletes_run_owned_snapshots_by_default(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            guest = await self.manager.replace()
            snapshot_id = await self.manager.save_snapshot("mid_task")
            await self.manager.stop()

        self.assertTrue(guest.sandbox.killed)
        self.assertEqual(self.manager.snapshot_ids(), {})
        self.assertEqual(FakeSandbox.deleted_snapshots, [snapshot_id])

    async def test_stop_can_retain_snapshots_for_explicit_debugging(self):
        with patch.object(bridge, "Sandbox", FakeSandbox):
            config = bridge.BridgeConfig(
                template=IMMUTABLE_TEMPLATE,
                campaign_id="test-campaign",
                retain_snapshots=True,
            )
            manager = bridge.GuestManager(config, self.guest_ports)
            with patch.object(manager, "_wait_ready", AsyncMock()):
                await manager.replace()
                snapshot_id = await manager.save_snapshot("mid_task")
                await manager.stop()

        self.assertEqual(manager.snapshot_ids(), {"mid_task": snapshot_id})
        self.assertEqual(FakeSandbox.deleted_snapshots, [])

    async def test_ws_connect_retries_until_upstream_resumes(self):
        class FlakySession:
            attempts = 0

            async def ws_connect(self, url, headers=None, max_msg_size=0):
                FlakySession.attempts += 1
                if FlakySession.attempts <= 2:
                    raise ConnectionError("guest still resuming")
                return "upstream-ws"

        with patch.object(bridge.asyncio, "sleep", AsyncMock()):
            result = await bridge._ws_connect_with_retry(
                FlakySession(), "wss://guest", {}, 30
            )
        self.assertEqual(result, "upstream-ws")
        self.assertEqual(FlakySession.attempts, 3)

    async def test_ws_connect_gives_up_after_retry_window(self):
        class DeadSession:
            async def ws_connect(self, url, headers=None, max_msg_size=0):
                raise ConnectionError("upstream down")

        with self.assertRaisesRegex(ConnectionError, "upstream down"):
            await bridge._ws_connect_with_retry(DeadSession(), "wss://guest", {}, 0)

    async def test_unknown_snapshot_name_falls_back_to_template(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            reverted = await self.manager.revert("init_state")

        self.assertEqual(FakeSandbox.created[1].create_template, self.config.template)
        self.assertEqual(reverted.source, "template")

    async def test_volume_check_accepts_exact_requested_root_capacity(self):
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            await self.manager.replace()
            result = await self.manager.check_volume(100)

        self.assertEqual(result["requested_gb"], 100)
        self.assertEqual(result["required_bytes"], 100_000_000_000)
        self.assertEqual(result["root_capacity_bytes"], 100 * 1024**3)
        capacity_calls = [
            call
            for call in FakeSandbox.created[0].commands.calls
            if call[0].startswith("df -B1")
        ]
        self.assertEqual(len(capacity_calls), 1)
        self.assertEqual(capacity_calls[0][1]["user"], "root")

    async def test_volume_check_rejects_undersized_root_before_task_setup(self):
        FakeSandbox.root_capacity_bytes = 99_999_999_999
        self.addCleanup(setattr, FakeSandbox, "root_capacity_bytes", 100 * 1024**3)
        with (
            patch.object(bridge, "Sandbox", FakeSandbox),
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
        rewritten = bridge._rewrite_cdp_host(payload, 19222).decode()
        self.assertIn("ws://127.0.0.1:19222/devtools/browser/1", rewritten)
        self.assertIn('"host": "127.0.0.1:19222"', rewritten)


def _config(**overrides):
    base = dict(
        template=IMMUTABLE_TEMPLATE, campaign_id="test-campaign", ready_timeout_s=5
    )
    base.update(overrides)
    return bridge.BridgeConfig(**base)


class _GateEvent(threading.Event):
    """A create_gate that also reports when a create started waiting on it."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def wait(self, timeout=None):
        self.entered.set()
        return super().wait(timeout)


class BridgeTestCase(unittest.TestCase):
    """Bridge tests against the fake E2B sandbox, with readiness stubbed out."""

    def setUp(self):
        FakeSandbox.created.clear()
        FakeSandbox.deleted_snapshots.clear()
        self.patches = [
            patch.object(bridge, "Sandbox", FakeSandbox),
            patch.object(
                bridge.GuestManager, "_wait_ready", AsyncMock(return_value=None)
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()


class BridgeThreadTests(BridgeTestCase):
    def test_start_binds_free_loopback_ports_and_creates_one_guest(self):
        b = bridge.Bridge(_config())
        try:
            b.start()
            self.assertTrue(b.started)
            self.assertTrue(0 < b.server_port <= 65535)
            self.assertNotEqual(b.server_port, b.cdp_port)
            self.assertNotEqual(b.cdp_port, b.vlc_port)
            self.assertEqual(b.local_ports[b.server_port], 5000)
            self.assertEqual(b.local_ports[b.cdp_port], 9222)
            self.assertEqual(b.local_ports[b.vlc_port], 8080)
            self.assertEqual(len(FakeSandbox.created), 1)
            state = b.state()
            self.assertEqual(state["sandbox_id"], "sandbox-1")
            self.assertEqual(state["generation"], 1)
            self.assertNotIn("traffic_token", json.dumps(state))
        finally:
            b.stop()
        self.assertTrue(FakeSandbox.created[0].killed)

    def test_two_bridges_in_one_process_do_not_collide(self):
        a, c = bridge.Bridge(_config()), bridge.Bridge(_config())
        try:
            a.start()
            c.start()
            self.assertEqual(
                len(
                    {
                        a.server_port,
                        a.cdp_port,
                        a.vlc_port,
                        c.server_port,
                        c.cdp_port,
                        c.vlc_port,
                    }
                ),
                6,
            )
        finally:
            a.stop()
            c.stop()

    def test_reset_replaces_the_guest_and_kills_the_previous_one(self):
        b = bridge.Bridge(_config())
        try:
            b.start()
            state = b.reset("init_state")
            self.assertEqual(state["sandbox_id"], "sandbox-2")
            self.assertEqual(state["generation"], 2)
            self.assertTrue(FakeSandbox.created[0].killed)
            self.assertFalse(FakeSandbox.created[1].killed)
        finally:
            b.stop()

    def test_save_then_reset_by_name_seeds_from_the_snapshot(self):
        b = bridge.Bridge(_config())
        try:
            b.start()
            snapshot_id = b.save("mid-task")
            self.assertEqual(snapshot_id, "snap-of-sandbox-1")
            b.reset("mid-task")
            self.assertEqual(
                FakeSandbox.created[1].create_template, "snap-of-sandbox-1"
            )
        finally:
            b.stop()
        self.assertEqual(FakeSandbox.deleted_snapshots, ["snap-of-sandbox-1"])

    def test_an_interrupted_start_stops_the_bridge_instead_of_orphaning_the_guest(
        self,
    ):
        # SIGTERM/SIGALRM/Ctrl-C land in the main thread while it waits on the
        # readiness gate; the loop thread is still creating the first guest and
        # the caller never gets an env to close, so start() must stop itself.
        b = bridge.Bridge(_config())
        gate = _GateEvent()
        FakeSandbox.create_gate = gate
        self.addCleanup(setattr, FakeSandbox, "create_gate", None)
        self.addCleanup(gate.set)

        def release_once_stopping():
            # Hold the create until stop() has marked the manager stopped, so
            # the create returns into the reap path every run.
            deadline = time.monotonic() + 5
            while not b._manager._stopped and time.monotonic() < deadline:
                time.sleep(0.005)
            gate.set()

        def interrupt(*_args, **_kwargs):
            self.assertTrue(gate.entered.wait(timeout=5))  # first guest mid-create
            threading.Thread(target=release_once_stopping, daemon=True).start()
            raise KeyboardInterrupt

        with self.assertLogs(bridge.logger, level="WARNING") as logs:
            with patch.object(b._ready, "wait", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    b.start()

        b._thread.join(timeout=5)
        self.assertFalse(b._thread.is_alive())
        self.assertEqual(len(FakeSandbox.created), 1)
        self.assertTrue(FakeSandbox.created[0].killed)
        # Reaped by the create itself: no readiness wait, no proxy install, no
        # guest handed to a bridge that is already stopping.
        self.assertTrue(
            any("reaped just-created sandbox" in line for line in logs.output),
            logs.output,
        )
        self.assertIsNone(b._manager._guest)
        self.assertFalse(b.started)
        b.stop()  # a second stop is a no-op

    def test_stop_is_idempotent_and_reset_after_stop_raises(self):
        b = bridge.Bridge(_config())
        b.start()
        b.stop()
        b.stop()
        with self.assertRaises(bridge.GuestUnavailable):
            b.reset()

    def test_literal_task_service_port_collision_fails_loudly(self):
        import socket

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        b = bridge.Bridge(_config(task_service_ports=f"{port}:3000"))
        try:
            with self.assertRaisesRegex(RuntimeError, "already owned"):
                b.start()
            self.assertEqual(FakeSandbox.created, [])  # no guest before listeners bind
        finally:
            blocker.close()
            b.stop()

    def test_proxy_forwards_to_ingress_with_token_and_rewrites_cdp_host(self):
        import urllib.request

        b = bridge.Bridge(_config())
        captured = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def read(self):
                return b'{"webSocketDebuggerUrl": "ws://9222-sandbox-1.example.test/devtools/x", "host": "x"}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(
                self, method, url, headers=None, data=None, allow_redirects=False
            ):
                captured.update(method=method, url=url, headers=headers)
                return FakeResponse()

        try:
            with patch.object(bridge.aiohttp, "ClientSession", FakeSession):
                b.start()
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{b.cdp_port}/json/version", timeout=5
                ) as response:
                    body = response.read().decode()
            self.assertEqual(
                captured["url"], "https://9222-sandbox-1.example.test/json/version"
            )
            self.assertEqual(
                captured["headers"]["e2b-traffic-access-token"], "token-sandbox-1"
            )
            self.assertIn(f"ws://127.0.0.1:{b.cdp_port}/devtools/x", body)
            self.assertIn(f'"host": "127.0.0.1:{b.cdp_port}"', body)
        finally:
            b.stop()


class BridgeUploadTests(BridgeTestCase):
    """POST /setup/upload through the real listener and the real handler."""

    DESTINATION = "/home/user/Desktop/input.bin"
    PAYLOAD = b"file-payload"

    @staticmethod
    def _multipart_body(destination, payload):
        boundary = "osworld-bridge-test-boundary"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file_path"\r\n\r\n',
                destination.encode() + b"\r\n",
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file_data"; '
                b'filename="input.bin"\r\n',
                b"Content-Type: application/octet-stream\r\n\r\n",
                payload + b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        return body, f"multipart/form-data; boundary={boundary}"

    def _post_upload(self, bridge_instance, destination=None, payload=None):
        import urllib.request

        body, content_type = self._multipart_body(
            self.DESTINATION if destination is None else destination,
            self.PAYLOAD if payload is None else payload,
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge_instance.server_port}/setup/upload",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()

    @staticmethod
    def _recording_session(requests):
        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(self, method, url, **kwargs):
                requests.append((method, url))
                raise AssertionError("upload must not be forwarded over HTTP")

        return FakeSession

    def test_setup_upload_uses_the_native_file_api_without_forwarding_http(self):
        b = bridge.Bridge(_config())
        forwarded = []
        try:
            with patch.object(
                bridge.aiohttp, "ClientSession", self._recording_session(forwarded)
            ):
                b.start()
                status, text = self._post_upload(b)
            files = FakeSandbox.created[0].files
        finally:
            b.stop()

        self.assertEqual(status, 200)
        self.assertEqual(text, f"File Uploaded: {len(self.PAYLOAD)} bytes")
        self.assertEqual(forwarded, [])
        self.assertEqual(len(files.writes), 1)
        staging_path, written, kwargs = files.writes[0]
        self.assertEqual(str(Path(staging_path).parent), "/home/user/Desktop")
        self.assertTrue(
            Path(staging_path).name.startswith(".input.bin.osworld-upload-")
        )
        self.assertEqual(written, self.PAYLOAD)
        self.assertEqual(
            kwargs,
            {
                "user": "user",
                "request_timeout": b.config.http_timeout_s,
                "use_octet_stream": True,
            },
        )
        self.assertEqual(
            files.renames,
            [
                (
                    staging_path,
                    self.DESTINATION,
                    {"user": "user", "request_timeout": b.config.http_timeout_s},
                )
            ],
        )
        self.assertEqual(files.removals, [])
        self.assertEqual(files.entries, {self.DESTINATION: self.PAYLOAD})

    def test_setup_upload_failure_is_a_502_that_leaves_no_staging_file(self):
        import urllib.error

        b = bridge.Bridge(_config())
        try:
            b.start()
            files = FakeSandbox.created[0].files
            files.entries[self.DESTINATION] = b"original"

            def failing_write(path, _data, **kwargs):
                files.writes.append((path, None, kwargs))
                files.entries[path] = b"partial"
                raise InvalidArgumentException("invalid destination")

            files.write = failing_write
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post_upload(b)
        finally:
            b.stop()

        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(len(files.writes), 1)  # permanent failure is not retried
        staging_path = files.writes[0][0]
        self.assertNotEqual(staging_path, self.DESTINATION)
        self.assertEqual(
            files.removals,
            [
                (
                    staging_path,
                    {"user": "user", "request_timeout": b.config.http_timeout_s},
                )
            ],
        )
        self.assertEqual(files.renames, [])
        self.assertEqual(files.entries, {self.DESTINATION: b"original"})


class BridgeRoadmapTests(BridgeTestCase):
    """Timed-out calls, shutdown reporting and the task-082 listener hint."""

    @staticmethod
    def _wait_until(predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_reset_timeout_cancels_the_replace_and_leaves_no_untracked_guest(self):
        # reset()'s production timeout is ready_timeout_s + 600, far too long to
        # wait for here, so the test submits the very coroutine reset() submits
        # and gives _call a short deadline.
        b = bridge.Bridge(_config())
        gate = threading.Event()
        try:
            b.start()
            FakeSandbox.create_gate = gate
            self.addCleanup(setattr, FakeSandbox, "create_gate", None)

            with self.assertRaises(TimeoutError) as caught:
                b._call("reset", b._manager.revert(None), timeout=0.5)
            self.assertEqual(str(caught.exception), "bridge reset exceeded 0.5s")

            # The cancelled replace has unwound once it drops the replace lock.
            self.assertTrue(
                self._wait_until(lambda: not b._manager._replace_lock.locked())
            )
            gate.set()
            # The create that was already in flight finishes on its worker thread
            # and reaps its own sandbox rather than leaving it running untracked.
            self.assertTrue(self._wait_until(lambda: len(FakeSandbox.created) == 2))
            self.assertTrue(self._wait_until(lambda: FakeSandbox.created[1].killed))

            state = b.state()
            self.assertEqual(state["sandbox_id"], "sandbox-1")
            self.assertEqual(state["generation"], 1)
            self.assertFalse(FakeSandbox.created[0].killed)
        finally:
            gate.set()
            b.stop()

    def test_stop_reports_a_clean_shutdown(self):
        b = bridge.Bridge(_config())
        self.assertFalse(b.clean_stop)
        b.start()
        b.stop()
        self.assertTrue(b.clean_stop)
        self.assertTrue(FakeSandbox.created[0].killed)

    def test_stop_kills_the_guest_after_the_heartbeat_died_of_its_own_exception(self):
        # cancel() is a no-op on an already-failed task and awaiting it re-raises,
        # which used to abort shutdown before the guest kill while stop() still
        # reported clean_stop.
        died = threading.Event()

        async def failing_heartbeat(_manager):
            try:
                await asyncio.sleep(0)  # let _main reach its stop-event wait
                raise RuntimeError("boom")
            finally:
                died.set()

        b = bridge.Bridge(_config())
        with (
            patch.object(bridge.GuestManager, "heartbeat", failing_heartbeat),
            # A drained exception leaves no "Task exception was never retrieved".
            self.assertNoLogs("asyncio", level="ERROR"),
        ):
            b.start()
            self.assertTrue(died.wait(timeout=5))
            with self.assertLogs(bridge.logger, level="WARNING") as logs:
                b.stop()
            gc.collect()  # an unretrieved task exception is reported at GC

        self.assertTrue(b.clean_stop)
        self.assertTrue(FakeSandbox.created[0].killed)
        self.assertTrue(
            any("heartbeat task ended with boom" in line for line in logs.output),
            logs.output,
        )

    def test_stop_reports_incomplete_cleanup_when_the_loop_thread_does_not_exit(self):
        b = bridge.Bridge(_config())
        b.start()
        loop_thread = b._thread

        class StuckThread:
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        b._thread = StuckThread()
        with self.assertLogs(bridge.logger, level="ERROR") as logs:
            b.stop()

        self.assertFalse(b.clean_stop)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("bridge did not stop", logs.output[0])
        self.assertIn("test-campaign", logs.output[0])
        self.assertIn("sandbox-1", logs.output[0])
        self.assertNotIn("token-sandbox-1", logs.output[0])
        # stop() still signalled the real loop, so nothing is left running.
        loop_thread.join(timeout=10)
        self.assertFalse(loop_thread.is_alive())
        self.assertTrue(FakeSandbox.created[0].killed)

    def test_start_hints_that_task_082_needs_a_task_service_listener(self):
        b = bridge.Bridge(_config())
        try:
            with self.assertLogs(bridge.logger, level="INFO") as logs:
                b.start()
        finally:
            b.stop()

        hints = [
            line for line in logs.output if "OSWORLD_TASK_SERVICE_PORTS empty" in line
        ]
        self.assertEqual(len(hints), 1)
        self.assertIn(
            "no task-service listeners configured (OSWORLD_TASK_SERVICE_PORTS "
            "empty); task 082 needs 3000, see README",
            hints[0],
        )


if __name__ == "__main__":
    unittest.main()
