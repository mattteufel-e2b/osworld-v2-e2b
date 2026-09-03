from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from e2b import InvalidArgumentException

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
    deleted_snapshots = []
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


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSandbox.created.clear()
        FakeSandbox.deleted_snapshots.clear()
        relay.TEMPLATE = IMMUTABLE_TEMPLATE
        self.manager = relay.GuestManager()

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

    @staticmethod
    def _proxy_session(requests):
        class Response:
            status = 200
            headers = {}

            async def read(self):
                return b"proxied"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def request(self, method, url, **kwargs):
                requests.append((method, url, kwargs))
                return Response()

        return Session

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

    async def test_replace_does_not_expand_fleet_routes_into_guest_network_rules(self):
        runtime = {
            "websites": {
                "traffic_token": "websites-traffic-token",
                "sites": {
                    f"site-{index}": {
                        "ingress_host": f"{13001 + index}-websites.e2b.app",
                        "port": 13001 + index,
                    }
                    for index in range(26)
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

        network = FakeSandbox.created[0].create_kwargs["network"]
        self.assertNotIn("rules", network)
        self.assertFalse(network["allow_public_traffic"])

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

    async def test_setup_upload_uses_one_native_file_write_without_http_forward(self):
        writes = []
        renames = []
        host_staged_paths = []

        class Files:
            def __init__(self):
                self.entries = {}

            def write(self, path, data, **kwargs):
                payload = data.read()
                host_staged_paths.append(Path(data.name))
                writes.append((path, payload, kwargs))
                self.entries[path] = payload

            def rename(self, old_path, new_path, **kwargs):
                renames.append((old_path, new_path, kwargs))
                self.entries[new_path] = self.entries.pop(old_path)

            def remove(self, path, **_kwargs):
                self.entries.pop(path, None)

        files = Files()
        guest = relay.Guest(
            sandbox=type("Sandbox", (), {"files": files})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={5000: "5000-sandbox-1.example.test"},
            generation=1,
        )

        class Manager:
            async def current(self):
                return guest

            def touch(self):
                pass

        proxy_requests = []
        with (
            patch.object(relay, "manager", Manager()),
            patch.object(
                relay.aiohttp,
                "ClientSession",
                self._proxy_session(proxy_requests),
            ),
        ):
            response = await relay.make_proxy_handler(relay.SERVER_LOCAL)(
                self._upload_request()
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, "File Uploaded: 12 bytes")
        self.assertEqual(len(writes), 1)
        guest_staging_path = writes[0][0]
        self.assertEqual(Path(guest_staging_path).parent, Path("/home/user/Desktop"))
        self.assertTrue(Path(guest_staging_path).name.startswith(".input.bin.osworld-upload-"))
        self.assertEqual(writes[0][1], b"file-payload")
        self.assertEqual(
            writes[0][2],
            {
                "user": "user",
                "request_timeout": relay.RELAY_HTTP_TIMEOUT_S,
                "use_octet_stream": True,
            },
        )
        self.assertEqual(
            renames,
            [
                (
                    guest_staging_path,
                    "/home/user/Desktop/input.bin",
                    {
                        "user": "user",
                        "request_timeout": relay.RELAY_HTTP_TIMEOUT_S,
                    },
                )
            ],
        )
        self.assertEqual(files.entries, {"/home/user/Desktop/input.bin": b"file-payload"})
        self.assertEqual(proxy_requests, [])
        self.assertEqual(len(host_staged_paths), 1)
        self.assertFalse(host_staged_paths[0].exists())

    async def test_setup_upload_transport_failure_is_bounded_502_and_cleans_staging(
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
                raise aiohttp.ClientConnectionError("control-plane upload failed")

            def remove(self, path, **kwargs):
                removals.append((path, kwargs))
                self.entries.pop(path, None)

        files = Files()
        guest = relay.Guest(
            sandbox=type("Sandbox", (), {"files": files})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={5000: "5000-sandbox-1.example.test"},
            generation=1,
        )

        class Manager:
            async def current(self):
                return guest

            def touch(self):
                pass

        proxy_requests = []
        retry_sleep = AsyncMock()
        with (
            patch.object(relay, "manager", Manager()),
            patch.object(
                relay.aiohttp,
                "ClientSession",
                self._proxy_session(proxy_requests),
            ),
            patch.object(relay.asyncio, "sleep", retry_sleep),
            self.assertRaises(web.HTTPBadGateway),
        ):
            await relay.make_proxy_handler(relay.SERVER_LOCAL)(self._upload_request())

        self.assertEqual(len(writes), 3)
        self.assertEqual(len(set(writes)), 1)
        self.assertNotEqual(writes[0], "/home/user/Desktop/input.bin")
        self.assertEqual(files.entries, {"/home/user/Desktop/input.bin": b"original"})
        self.assertEqual(
            removals,
            [
                (
                    writes[0],
                    {
                        "user": "user",
                        "request_timeout": relay.RELAY_HTTP_TIMEOUT_S,
                    },
                )
            ],
        )
        self.assertEqual(retry_sleep.await_count, 2)
        self.assertTrue(
            all(0 <= call.args[0] <= 2 for call in retry_sleep.await_args_list)
        )
        self.assertEqual(proxy_requests, [])

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
        guest = relay.Guest(
            sandbox=type("Sandbox", (), {"files": files})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={},
            generation=1,
        )

        with patch.object(relay.asyncio, "sleep", AsyncMock()) as retry_sleep:
            response = await relay._direct_setup_upload(self._upload_request(), guest)

        self.assertEqual(response.status, 200)
        self.assertEqual(attempts, 3)
        self.assertEqual(retry_sleep.await_count, 2)
        self.assertEqual(len(renames), 1)
        self.assertEqual(files.entries, {"/home/user/Desktop/input.bin": b"file-payload"})

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
        guest = relay.Guest(
            sandbox=type("Sandbox", (), {"files": files})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={},
            generation=1,
        )

        retry_sleep = AsyncMock()
        with (
            patch.object(relay.asyncio, "sleep", retry_sleep),
            self.assertRaises(web.HTTPBadGateway),
        ):
            await relay._direct_setup_upload(self._upload_request(), guest)

        self.assertEqual(len(writes), 1)
        self.assertEqual(removals, [writes[0]])
        self.assertEqual(files.entries, {"/home/user/Desktop/input.bin": b"original"})
        self.assertEqual(retry_sleep.await_count, 0)

    async def test_setup_upload_rejects_relative_destination_without_sdk_write(self):
        writes = []

        class Files:
            def write(self, *args, **kwargs):
                writes.append((args, kwargs))

        guest = relay.Guest(
            sandbox=type("Sandbox", (), {"files": Files()})(),
            sandbox_id="sandbox-1",
            traffic_token="token",
            hosts={5000: "5000-sandbox-1.example.test"},
            generation=1,
        )

        class Manager:
            async def current(self):
                return guest

            def touch(self):
                pass

        with (
            patch.object(relay, "manager", Manager()),
            patch.object(
                relay.aiohttp,
                "ClientSession",
                self._proxy_session([]),
            ),
            self.assertRaises(web.HTTPBadRequest),
        ):
            await relay.make_proxy_handler(relay.SERVER_LOCAL)(
                self._upload_request(destination="home/user/Desktop/input.bin")
            )

        self.assertEqual(writes, [])

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

    async def test_heartbeat_retries_transient_control_plane_failure(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
            patch.object(relay.asyncio, "sleep", AsyncMock()) as retry_sleep,
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
        self.assertEqual(guest.sandbox.timeout_calls, [relay.SANDBOX_TIMEOUT_S])
        self.assertEqual(retry_sleep.await_count, 2)
        self.assertTrue(
            all(0 <= call.args[0] <= 4 for call in retry_sleep.await_args_list)
        )

    async def test_heartbeat_final_failure_keeps_activity_pending_for_later_retry(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
            patch.object(relay.asyncio, "sleep", AsyncMock()) as retry_sleep,
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

        self.assertEqual(guest.sandbox.timeout_calls, [relay.SANDBOX_TIMEOUT_S])

    async def test_heartbeat_does_not_consume_replacement_guest_activity(self):
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
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

        self.assertEqual(first.sandbox.timeout_calls, [relay.SANDBOX_TIMEOUT_S])
        self.assertEqual(second.sandbox.timeout_calls, [relay.SANDBOX_TIMEOUT_S])

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

    async def test_stop_kills_guest_but_keeps_snapshot_persistent_without_delete(self):
        with (
            patch.object(relay, "Sandbox", FakeSandbox),
            patch.object(self.manager, "_wait_ready", AsyncMock()),
        ):
            guest = await self.manager.replace()
            snapshot_id = await self.manager.save_snapshot("mid_task")
            await self.manager.stop()

        self.assertTrue(guest.sandbox.killed)
        self.assertEqual(self.manager.snapshot_ids(), {"mid_task": snapshot_id})
        self.assertEqual(FakeSandbox.deleted_snapshots, [])

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
