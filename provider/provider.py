"""E2B provider for OSWorld.

The provider owns one in-process Bridge (desktop_env.providers.e2b.bridge):
sandbox lifecycle, snapshot save/revert, and the loopback proxy DesktopEnv
dials. No separate process, no fixed ports.
"""

from __future__ import annotations

import logging

from desktop_env.providers.base import Provider
from desktop_env.providers.e2b.bridge import Bridge, BridgeConfig
from desktop_env.providers.volume import normalize_volume_size

logger = logging.getLogger("desktopenv.providers.e2b")


class E2BProvider(Provider):
    def __init__(self, region: str = None):
        super().__init__(region)
        self.bridge: Bridge | None = None
        self.volume_size = None

    def prepare_volume(self, path_to_vm: str, volume_size: int | None, os_type: str):
        self.volume_size = normalize_volume_size(volume_size)

    def finalize_volume(
        self,
        path_to_vm: str,
        volume_size: int | None,
        os_type: str,
        controller,
        setup_controller,
        client_password: str,
    ):
        requested_gb = normalize_volume_size(volume_size)
        if requested_gb is None:
            return
        state = self._require_bridge().check_volume(requested_gb)
        logger.info(
            "E2B root capacity verified: requested=%sGB actual_bytes=%s",
            requested_gb,
            state.get("root_capacity_bytes"),
        )

    def start_emulator(
        self, path_to_vm: str, headless: bool, os_type: str = None, *args, **kwargs
    ):
        # path_to_vm is the immutable GUEST_TEMPLATE reference from the manager.
        # First call: bind the proxy and create the guest. Later calls (after
        # every revert) find the bridge already running its replaced guest.
        if self.bridge is None:
            self.bridge = Bridge(BridgeConfig.from_env(template=path_to_vm))
            self.bridge.start()
        state = self.bridge.state()
        logger.info("E2B guest ready: %s", state["sandbox_id"])

    def get_ip_address(self, path_to_vm: str) -> str:
        # host:server_port:chromium_port:vnc_port:vlc_port. Headless, so the VNC
        # slot is 0. Ports are whatever the OS assigned to this bridge.
        bridge = self._require_bridge()
        return f"127.0.0.1:{bridge.server_port}:{bridge.cdp_port}:0:{bridge.vlc_port}"

    def save_state(self, path_to_vm: str, snapshot_name: str):
        snapshot_id = self._require_bridge().save(snapshot_name)
        logger.info("E2B snapshot saved: %s -> %s", snapshot_name, snapshot_id)

    def revert_to_snapshot(self, path_to_vm: str, snapshot_name: str) -> str:
        # A name saved via save_state reverts to that state; any other name
        # (OSWorld's default "init_state") means a fresh sandbox from the template.
        state = self._require_bridge().reset(snapshot_name)
        logger.info(
            "E2B guest replaced: %s (generation %s, source %s)",
            state["sandbox_id"],
            state["generation"],
            state.get("source", "template"),
        )
        return path_to_vm

    def stop_emulator(self, path_to_vm: str, *args, **kwargs):
        if self.bridge is None:
            return
        try:
            self.bridge.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not stop E2B bridge cleanly: %s", exc)

    def _require_bridge(self) -> Bridge:
        if self.bridge is None:
            raise RuntimeError(
                "E2B bridge is not running; start_emulator was not called"
            )
        return self.bridge
