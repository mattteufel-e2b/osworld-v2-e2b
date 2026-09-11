"""The relay/worker lifecycle lives once, in runner/worker_lib.sh.

run_agent.sh (customer path) and the maintainer validation wrappers used to each
carry their own copy of relay start-up, health wait, watchdog and process-group
teardown. One library means a lifecycle fix lands everywhere at once.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "runner" / "worker_lib.sh"
WRAPPERS = (
    ROOT / "runner" / "run_agent.sh",
    ROOT / "maintainer" / "run_path_task.sh",
    ROOT / "maintainer" / "validate.sh",
)
LIFECYCLE_FUNCTIONS = (
    "start_in_new_session",
    "process_alive",
    "signal_process",
    "wait_for_exit",
    "terminate_process_group",
    "start_relay",
    "shutdown_relay",
    "export_fleet_wiring",
    "require_immutable_guest_template",
    "resolve_e2b_api_key",
)


def _definitions(source: str) -> set[str]:
    return set(re.findall(r"^([a-z0-9_]+)\(\)\s*\{", source, re.MULTILINE))


def test_lifecycle_functions_are_defined_once_in_the_shared_library():
    lib_defs = _definitions(LIB.read_text())
    for name in LIFECYCLE_FUNCTIONS:
        assert name in lib_defs, name
    for wrapper in WRAPPERS:
        assert not (_definitions(wrapper.read_text()) & set(LIFECYCLE_FUNCTIONS)), (
            wrapper.name
        )


def test_every_worker_wrapper_sources_the_shared_library():
    for wrapper in WRAPPERS:
        assert "runner/worker_lib.sh" in wrapper.read_text() or (
            'source "$HERE/worker_lib.sh"' in wrapper.read_text()
        ), wrapper.name


def test_shared_library_parses_and_exports_the_worker_uv_command():
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIB}" && echo "${{WORKER_UV[*]}}" && '
            "declare -F start_relay terminate_process_group >/dev/null && echo defined",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "uv run --locked --extra full --python 3.12" in result.stdout
    assert "defined" in result.stdout
