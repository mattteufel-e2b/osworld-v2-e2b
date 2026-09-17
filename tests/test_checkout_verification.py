import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "examples/osworld-v2/upstream.lock.json").read_text())["code"][
    "commit"
]


def test_verify_checkout_rejects_drift_without_modifying_it(tmp_path):
    source = ROOT / "OSWorld-V2"
    if not (source / ".git").exists():
        pytest.skip("pinned upstream checkout not installed")
    checkout = tmp_path / "upstream"
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(source), str(checkout)], check=True
    )
    setup = ["bash", str(ROOT / "runner/setup.sh")]
    result = subprocess.run(
        [*setup, str(checkout)], check=True, capture_output=True, text=True
    )
    assert str(ROOT / "README.md") in result.stdout
    assert "# host-side relay" not in result.stdout
    verify = [*setup, "--verify", str(checkout)]
    assert subprocess.run(verify, capture_output=True).returncode == 0
    upstream_runner = subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{PIN}:lib_run_single.py"]
    )
    assert (checkout / "lib_run_single.py").read_bytes() == upstream_runner
    backend = "desktop_env/evaluators/backends/openai_backend.py"
    pristine = subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{PIN}:{backend}"]
    )
    assert (checkout / backend).read_bytes() == pristine
    for relative in (
        "lib_run_single.py",
        "desktop_env/providers/e2b/bridge.py",
        "run.py",
        backend,
    ):
        target = checkout / relative
        original = target.read_bytes()
        changed = original + b"\n# unexpected modification\n"
        target.write_bytes(changed)
        failed = subprocess.run(verify, capture_output=True, text=True)
        assert failed.returncode != 0
        assert "runner/setup.sh" in failed.stderr, relative  # says how to repair
        assert target.read_bytes() == changed
        target.write_bytes(original)
    # --restore reverts every tracked file in the footprint, including a
    # leftover edit to the evaluator backend from an earlier setup.sh version.
    (checkout / backend).write_bytes(pristine + b"\n# legacy edit\n")
    subprocess.run(
        [*setup, "--restore", str(checkout)], check=True, capture_output=True
    )
    status = subprocess.check_output(
        ["git", "-C", str(checkout), "status", "--porcelain"], text=True
    )
    assert status == ""
    subprocess.run([*setup, str(checkout)], check=True, capture_output=True)
    assert subprocess.run(verify, capture_output=True).returncode == 0
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--quiet", "HEAD~1"], check=True
    )
    assert subprocess.run(verify, capture_output=True).returncode != 0


def _apply_patches(dest: Path) -> None:
    script = (ROOT / "runner" / "setup.sh").read_text()
    body = script[
        script.index("apply_adapter_patches() {") : script.index(
            "\n}\n", script.index("apply_adapter_patches() {")
        )
        + 3
    ]
    subprocess.run(
        ["bash", "-c", body + f'\napply_adapter_patches "{dest}"'],
        check=True,
        capture_output=True,
        text=True,
    )


PINNED_PARSER_HEAD = "import re\n"
PINNED_KEY_TABLE = (
    "            key_conversion = {\n"
    '                "page_down": "pagedown",\n'
    '                "page_up": "pageup",\n'
    '                "super_l": "win",\n'
    '                "super": "command",\n'
    '                "escape": "esc",\n'
    "            }\n"
)
PINNED_INFEASIBLE = (
    '    if "[INFEASIBLE]" in response:\n        return "[INFEASIBLE]", ["FAIL"]\n'
)
PINNED_NON_200_BRANCH = (
    "                else:\n"
    '                    logger.error("Failed to execute command. Status code: %d", response.status_code)\n'
    '                    logger.info("Retrying to execute command.")\n'
)
PINNED_CONTROLLER = (
    "                response = requests.post(self.http_server + \"/execute\", headers={'Content-Type': 'application/json'},\n"
    "                                         data=payload, timeout=90)\n"
    "                if response.status_code == 200:\n"
    "                    return response.json()\n"
    + PINNED_NON_200_BRANCH
    + "    def run_python_script(self, script: str, timeout=90) -> Optional[Dict[str, Any]]:\n"
)


def _seed_minimal_checkout(dest: Path, parser_text: str, controller_text: str) -> None:
    (dest / "scripts" / "python").mkdir(parents=True)
    (dest / "desktop_env" / "providers").mkdir(parents=True)
    (dest / "desktop_env" / "controllers").mkdir(parents=True)
    (dest / "mm_agents" / "m3").mkdir(parents=True)
    (dest / "desktop_env" / "providers" / "__init__.py").write_text(
        '    else:\n        raise NotImplementedError(f"{provider_name} not implemented!")'
    )
    (dest / "desktop_env" / "desktop_env.py").write_text(
        'if self.provider_name in {"docker", "aws", "gcp", "azure", "aliyun", "volcengine"}:\n'
        "if self.is_environment_used:\n"
    )
    (dest / "scripts" / "python" / "run_multienv_m3.py").write_text(
        'choices=["aws", "virtualbox", "vmware", "docker", "azure"]\n'
    )
    (dest / "mm_agents" / "m3" / "parser.py").write_text(parser_text)
    (dest / "desktop_env" / "controllers" / "python.py").write_text(controller_text)


def test_setup_patches_the_m3_runner_provider_choices(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    _seed_minimal_checkout(
        dest,
        PINNED_PARSER_HEAD + PINNED_KEY_TABLE + PINNED_INFEASIBLE,
        PINNED_CONTROLLER,
    )
    _apply_patches(dest)
    patched = (dest / "scripts" / "python" / "run_multienv_m3.py").read_text()
    assert (
        'choices=["aws", "virtualbox", "vmware", "docker", "azure", "e2b"]' in patched
    )
    # idempotent
    _apply_patches(dest)
    assert patched == (dest / "scripts" / "python" / "run_multienv_m3.py").read_text()


def test_setup_maps_m3_super_key_to_x11_win(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    _seed_minimal_checkout(
        dest,
        PINNED_PARSER_HEAD + PINNED_KEY_TABLE + PINNED_INFEASIBLE,
        PINNED_CONTROLLER,
    )
    _apply_patches(dest)
    patched = (dest / "mm_agents" / "m3" / "parser.py").read_text()
    assert '"super": "win",' in patched
    assert '"super": "command"' not in patched
    assert '"super_l": "win",' in patched  # neighbours untouched
    _apply_patches(dest)  # idempotent
    assert patched == (dest / "mm_agents" / "m3" / "parser.py").read_text()


def test_setup_ignores_infeasible_marker_inside_the_thinking_block(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    _seed_minimal_checkout(
        dest,
        PINNED_PARSER_HEAD + PINNED_KEY_TABLE + PINNED_INFEASIBLE,
        PINNED_CONTROLLER,
    )
    _apply_patches(dest)
    patched = (dest / "mm_agents" / "m3" / "parser.py").read_text()
    assert 'if "[INFEASIBLE]" in _M3_THINK_BLOCK.sub("", response):' in patched
    assert '_M3_THINK_BLOCK = re.compile(r"<mm:think>.*?</mm:think>", re.S)' in patched
    _apply_patches(dest)
    assert patched == (dest / "mm_agents" / "m3" / "parser.py").read_text()


def test_patched_infeasible_check_semantics():
    # Pure-Python check of the exact expression the patch installs.
    import re

    think = re.compile(r"<mm:think>.*?</mm:think>", re.S)
    inside = (
        "<mm:think>maybe [INFEASIBLE]?\nno, try ctrl+c</mm:think>\n"
        '<tool_call>{"action":"key","text":"ctrl+c"}</tool_call>'
    )
    outside = "<mm:think>reasoning</mm:think>\n[INFEASIBLE]"
    assert "[INFEASIBLE]" not in think.sub("", inside)
    assert "[INFEASIBLE]" in think.sub("", outside)


def test_setup_extends_the_action_deadline_past_the_guest_kill(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    _seed_minimal_checkout(
        dest,
        PINNED_PARSER_HEAD + PINNED_KEY_TABLE + PINNED_INFEASIBLE,
        PINNED_CONTROLLER,
    )
    _apply_patches(dest)
    patched = (dest / "desktop_env" / "controllers" / "python.py").read_text()
    assert "data=payload, timeout=130)" in patched
    assert (
        "def run_python_script(self, script: str, timeout=90)" in patched
    )  # untouched
    _apply_patches(dest)
    assert patched == (dest / "desktop_env" / "controllers" / "python.py").read_text()


def test_setup_stops_retrying_after_the_guest_reports_its_own_timeout(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    _seed_minimal_checkout(
        dest,
        PINNED_PARSER_HEAD + PINNED_KEY_TABLE + PINNED_INFEASIBLE,
        PINNED_CONTROLLER,
    )
    _apply_patches(dest)
    patched = (dest / "desktop_env" / "controllers" / "python.py").read_text()
    guard = 'if response.status_code == 500 and "timed out after" in response.text:'
    assert guard in patched
    # The guard gives up (break) instead of falling through to the retry.
    after_guard = patched.index(guard)
    assert patched.index("break", after_guard) < patched.index(
        'logger.info("Retrying to execute command.")', after_guard
    )
    # A non-timeout non-200 still retries: the retry log line survives.
    assert 'logger.info("Retrying to execute command.")' in patched
    assert (
        "def run_python_script(self, script: str, timeout=90)" in patched
    )  # untouched
    _apply_patches(dest)  # idempotent
    assert patched == (dest / "desktop_env" / "controllers" / "python.py").read_text()
