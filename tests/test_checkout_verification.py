import ast
import base64
import json
import logging
import os
import subprocess
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

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
        "mm_agents/anthropic/main.py",
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


# The anchor strings blocks (e)-(h) replace, mirrored from runner/setup.sh. Each
# must occur exactly once in the pinned upstream file -- see
# test_setup_anchors_are_unique_in_the_pinned_upstream_files.
PIN_ANCHORS = {
    "mm_agents/anthropic/main.py": (
        "            betas.append(PROMPT_CACHING_BETA_FLAG)\n",
    ),
    "mm_agents/m3/parser.py": (
        '                "super_l": "win",\n                "super": "command",\n',
        PINNED_INFEASIBLE,
        "import re\n",
    ),
    "desktop_env/controllers/python.py": (
        "data=payload, timeout=90)",
        PINNED_NON_200_BRANCH,
    ),
}


def test_setup_anchors_are_unique_in_the_pinned_upstream_files():
    """A pin bump that moves an anchor must fail here, not at run time."""
    source = ROOT / "OSWorld-V2"
    if not (source / ".git").exists():
        pytest.skip("pinned upstream checkout not installed")
    for relative, anchors in PIN_ANCHORS.items():
        pristine = subprocess.check_output(
            ["git", "-C", str(source), "show", f"{PIN}:{relative}"], text=True
        )
        for anchor in anchors:
            assert pristine.count(anchor) == 1, (relative, anchor)


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
    (dest / "mm_agents" / "m3" / "parser.py").write_text(parser_text + PINNED_TYPING)
    (dest / "desktop_env" / "controllers" / "python.py").write_text(controller_text)


PINNED_TYPING = """            for char in text:
                if char == '\\n':
                    code.append("pyautogui.press('enter')")
    elif action == "scroll":
"""


@pytest.fixture
def patched_checkout(tmp_path):
    source = ROOT / "OSWorld-V2"
    if not (source / ".git").exists():
        pytest.skip("pinned upstream checkout not installed")
    _seed_minimal_checkout(tmp_path, "", "")
    for relative in (
        "mm_agents/anthropic/main.py",
        "mm_agents/m3/parser.py",
        "mm_agents/m3/agent.py",
        "desktop_env/controllers/python.py",
        "desktop_env/controllers/website.py",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(
            subprocess.check_output(
                ["git", "-C", str(source), "show", f"{PIN}:{relative}"]
            )
        )
    _apply_patches(tmp_path)
    return tmp_path


def test_native_claude_cache_header_patch_preserves_every_other_byte_and_is_idempotent(
    patched_checkout,
):
    relative = "mm_agents/anthropic/main.py"
    pristine = subprocess.check_output(
        ["git", "-C", str(ROOT / "OSWorld-V2"), "show", f"{PIN}:{relative}"], text=True
    )
    expected = pristine.replace(
        "            betas.append(PROMPT_CACHING_BETA_FLAG)\n",
        "            # Prompt caching is generally available; keep cache_control without the obsolete beta.\n",
        1,
    )
    assert (patched_checkout / relative).read_text() == expected
    _apply_patches(patched_checkout)
    assert (patched_checkout / relative).read_text() == expected


def test_fleet_scheme_does_not_downgrade_on_transient_probe_failure(
    patched_checkout, monkeypatch
):
    source = ast.parse(
        (patched_checkout / "desktop_env/controllers/website.py").read_text()
    )
    function = next(
        n
        for n in source.body
        if isinstance(n, ast.FunctionDef) and n.name == "_select_website_scheme"
    )
    namespace = {
        "os": os,
        "requests": requests,
        "lru_cache": lru_cache,
        "logger": logging.getLogger(__name__),
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "website.py", "exec"),
        namespace,
    )

    def timeout(*args, **kwargs):
        raise requests.Timeout("slow fleet")

    monkeypatch.setattr(requests, "get", timeout)
    monkeypatch.setenv("OSWORLD_WEBSITE_SCHEME", "https")
    assert namespace["_select_website_scheme"]("mailhub.test:8090") == "https://"
    monkeypatch.delenv("OSWORLD_WEBSITE_SCHEME")
    assert namespace["_select_website_scheme"]("unconfigured.test") == "http://"


@pytest.mark.parametrize(
    "response, error_type, recover",
    [
        (None, ConnectionError, False),
        ("", ValueError, False),
        (
            '<tool_call>\n{"name":"computer","arguments":{"action":"done"}}\n</tool_function>',
            ValueError,
            False,
        ),
        (
            '<tool_call>\n{"name":"computer","arguments":{"action":"call_user"}}\n</tool_call>',
            None,
            False,
        ),
        ("", None, True),
        ("<tool_call>malformed", None, True),
    ],
)
def test_m3_failures_do_not_become_user_questions(
    patched_checkout, response, error_type, recover
):
    source = ast.parse((patched_checkout / "mm_agents/m3/agent.py").read_text())
    cls = next(
        n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "M3Agent"
    )
    predict = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "predict"
    )
    parser_namespace = {}
    exec((patched_checkout / "mm_agents/m3/parser.py").read_text(), parser_namespace)
    namespace = {
        "Dict": dict,
        "List": list,
        "Image": SimpleNamespace(open=lambda _: SimpleNamespace(size=(1920, 1080))),
        "BytesIO": BytesIO,
        "base64": base64,
        "logger": logging.getLogger(__name__),
        "_encode_screenshot": lambda _: ("eA==", "image/png"),
        "wrap_for_history": lambda s: s,
        "parse_m3_response": parser_namespace["parse_m3_response"],
    }
    exec(
        compile(ast.Module(body=[predict], type_ignores=[]), "agent.py", "exec"),
        namespace,
    )

    calls = 0

    def failed(messages):
        nonlocal calls
        calls += 1
        if recover and calls > 1:
            return (
                '<tool_call>\n{"name":"computer","arguments":{"action":"done"}}\n</tool_call>',
                {},
            )
        if response is None:
            raise ConnectionError("exhausted transport retries")
        return response, {}

    logs = []
    agent = SimpleNamespace(
        _api_call_count=0,
        _api_log_dir=None,
        screenshots=[],
        responses=[],
        user_responses=[],
        _build_messages=lambda *a: [],
        _build_request_body=lambda *a: {},
        _call_llm=failed,
        max_llm_retries=2,
        coordinate_type="relative",
        _save_api_log=lambda *a, **kw: logs.append(kw),
        actions=[],
        thoughts=[],
    )
    if error_type:
        with pytest.raises(error_type):
            namespace["predict"](agent, "test", {"screenshot": b"x"})
    else:
        assert namespace["predict"](agent, "test", {"screenshot": b"x"})[1] == (
            ["DONE"] if recover else []
        )
    assert calls == (3 if error_type else 2 if recover else 1)
    assert logs
    if response is None:
        assert logs[0]["retry_attempts"][0]["outcome"] == "exception"


def test_m3_long_typing_preserves_text_without_per_character_pauses(patched_checkout):
    namespace = {}
    exec((patched_checkout / "mm_agents/m3/parser.py").read_text(), namespace)
    text = 'A "quoted" path \\tmp\n' * 250
    code = namespace["tool_action_to_pyautogui"](
        "type", {"text": text}, lambda x, y: (x, y)
    )
    typed = []
    elapsed = 0.0

    def press(key):
        nonlocal elapsed
        typed.append("\n" if key == "enter" else key)
        elapsed += 0.1

    def write(value, interval=0):
        nonlocal elapsed
        typed.extend(value)
        elapsed += 0.1 + len(value) * interval

    exec("\n".join(code), {"pyautogui": SimpleNamespace(press=press, write=write)})
    assert "".join(typed) == text
    assert elapsed < 120


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


@pytest.mark.parametrize("mode", ["adaptive", "enabled"])
def test_m3_adaptive_thinking_omits_fixed_budget(patched_checkout, mode):
    source = ast.parse((patched_checkout / "mm_agents/m3/agent.py").read_text())
    cls = next(
        n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "M3Agent"
    )
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_build_request_body"
    )
    namespace = {"List": list, "Dict": dict, "Any": object}
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "agent.py", "exec"),
        namespace,
    )
    agent = SimpleNamespace(
        model="test",
        max_tokens=4096,
        thinking_mode=mode,
        thinking_budget=1024,
        temperature=0.6,
        top_p=0.9,
        stop_sequences=None,
        _translate_messages=lambda _: ("", []),
    )
    body = namespace["_build_request_body"](agent, [])
    assert body["thinking"] == (
        {"type": "adaptive"}
        if mode == "adaptive"
        else {"type": "enabled", "budget_tokens": 1024}
    )
    assert body["temperature"] == 1
    assert "top_p" not in body
