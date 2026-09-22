import ast
import base64
import json
import logging
import os
import shlex
import subprocess
import sys
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
PINNED_ANTHROPIC_KEY_TABLE = (
    '            if action == "key":\n'
    "                key_conversion = {\n"
    '                    "page_down": "pagedown",\n'
    '                    "page_up": "pageup",\n'
    '                    "super_l": "win",\n'
    '                    "super": "command",\n'
    '                    "escape": "esc"\n'
    "                }\n"
)
PINNED_INFEASIBLE = (
    '    if "[INFEASIBLE]" in response:\n        return "[INFEASIBLE]", ["FAIL"]\n'
)
PINNED_NON_200_BRANCH = (
    "                else:\n"
    '                    logger.error("Failed to execute command. Status code: %d", response.status_code)\n'
    '                    logger.info("Retrying to execute command.")\n'
)
PINNED_DICT_ACTION = (
    "                elif type(action) == dict:\n"
    "                    self.controller.execute_python_command(action['command'])\n"
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
    "desktop_env/desktop_env.py": (PINNED_DICT_ACTION,),
    "mm_agents/anthropic/main.py": (
        "            betas.append(PROMPT_CACHING_BETA_FLAG)\n",
        '                    "super_l": "win",\n                    "super": "command",\n',
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
        "if self.is_environment_used:\n" + PINNED_DICT_ACTION
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
        "desktop_env/desktop_env.py",
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


def _desktop_step(checkout, sleep):
    source = ast.parse((checkout / "desktop_env/desktop_env.py").read_text())
    cls = next(
        n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "DesktopEnv"
    )
    step = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "step"
    )
    namespace = {
        "time": SimpleNamespace(sleep=sleep),
        "logger": logging.getLogger(__name__),
    }
    exec(
        compile(ast.Module(body=[step], type_ignores=[]), "desktop_env.py", "exec"),
        namespace,
    )
    return namespace["step"]


def _step_env(events, provider="e2b", action_space="claude_computer_use"):
    def observe():
        events.append(("observe",))
        return {"screenshot": b"after-action"}

    return SimpleNamespace(
        provider_name=provider,
        action_space=action_space,
        _step_no=0,
        _traj_no=0,
        action_history=[],
        is_environment_used=False,
        controller=SimpleNamespace(
            execute_python_command=lambda command: events.append(("guest", command)),
        ),
        _get_obs=observe,
    )


@pytest.mark.parametrize(
    "duration,expected",
    [(1, 1), (120, 120), (180, 180), (240, 240), (600, 600), (0, 0.5), (None, 0.5)],
)
def test_native_wait_preserves_duration_and_observes_after_one_pause(
    patched_checkout, duration, expected
):
    events = []
    step = _desktop_step(
        patched_checkout, lambda seconds: events.append(("sleep", seconds))
    )
    env = _step_env(events)
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": "wait", "duration": duration},
        "command": f"pyautogui.sleep({duration or 0.5})\n",
    }
    result = step(env, action, pause=2)
    assert events == [("sleep", expected), ("sleep", 2), ("observe",)]
    assert result == ({"screenshot": b"after-action"}, 0, False, {})
    assert env.action_history == [action] and env.action_history[0] is action
    assert env._step_no == 1 and env.is_environment_used
    before = (patched_checkout / "desktop_env/desktop_env.py").read_bytes()
    _apply_patches(patched_checkout)
    assert (patched_checkout / "desktop_env/desktop_env.py").read_bytes() == before


@pytest.mark.parametrize(
    "provider,space,action_name",
    [
        ("e2b", "claude_computer_use", "left_click"),
        ("aws", "claude_computer_use", "wait"),
        ("e2b", "pyautogui", "wait"),
    ],
)
def test_native_wait_does_not_change_other_action_execution(
    patched_checkout, provider, space, action_name
):
    events = []
    step = _desktop_step(
        patched_checkout, lambda seconds: events.append(("sleep", seconds))
    )
    env = _step_env(events, provider, space)
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": action_name, "duration": 240},
        "command": "unchanged command",
    }
    step(env, action, pause=0)
    assert events == [("guest", "unchanged command"), ("sleep", 0), ("observe",)]


@pytest.mark.parametrize("duration", [601, 3600, 86400])
def test_native_wait_clamps_an_overlong_duration_and_says_so(
    patched_checkout, caplog, duration
):
    # A model that asks for an hour of wait would otherwise burn the whole
    # sandbox lifetime (and the worker slot) doing nothing.
    events = []
    step = _desktop_step(
        patched_checkout, lambda seconds: events.append(("sleep", seconds))
    )
    env = _step_env(events)
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": "wait", "duration": duration},
        "command": f"pyautogui.sleep({duration})\n",
    }
    with caplog.at_level(logging.WARNING):
        step(env, action, pause=0)
    assert events == [("sleep", 600), ("sleep", 0), ("observe",)]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert str(duration) in warnings[0] and "600" in warnings[0]


@pytest.mark.parametrize("duration", [-1, float("inf"), float("nan"), "240", True])
def test_native_wait_rejects_invalid_duration_before_execution(
    patched_checkout, duration
):
    events = []
    step = _desktop_step(
        patched_checkout, lambda seconds: events.append(("sleep", seconds))
    )
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": "wait", "duration": duration},
        "command": "unused",
    }
    with pytest.raises(ValueError, match="wait duration"):
        step(_step_env(events), action, pause=0)
    assert events == []


def test_native_wait_worker_deadline_interrupts_before_observation(patched_checkout):
    class Deadline(BaseException):
        pass

    def interrupt(_seconds):
        raise Deadline()

    events = []
    step = _desktop_step(patched_checkout, interrupt)
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": "wait", "duration": 240},
        "command": "pyautogui.sleep(240)\n",
    }
    with pytest.raises(Deadline):
        step(_step_env(events), action, pause=0)
    assert events == []


@pytest.mark.parametrize("nested", [False, True])
def test_native_batches_preserve_order_and_use_wait_and_clipboard_repairs(
    patched_checkout, nested
):
    python = ROOT / "OSWorld-V2/.venv/bin/python"
    if not python.exists():
        pytest.skip("requires upstream runtime")
    leaves = [{"action": "wait", "duration": 240}, {"action": "type", "text": "café"}]
    inputs = {
        "actions": ([{"actions": leaves}] if nested else leaves)
        + [
            {"action": "left_click", "coordinate": [640, 360]},
        ]
    }
    # Use the real agent adapter and pinned parser; replace only the model call.
    result = subprocess.run(
        [
            str(python),
            "-c",
            """
import json,os,sys
from pathlib import Path
sys.path[:0] = [str(Path('runner').resolve()), str(Path('OSWorld-V2').resolve())]
from agents import build_agent, agent_settings
from mm_agents.anthropic.main import AnthropicAgent
os.environ.update(MODEL_API_KEY='offline', MODEL_BASE_URL='https://model.test')
agent = build_agent('claude', model='claude-opus-4-7', settings=agent_settings('claude'),
                    client_password='osworld-public-evaluation')
action = {'name':'computer', 'input':json.loads(sys.argv[1]), 'action_type':'tool_use'}
action['command'] = agent.parse_actions_from_tool_call(action)
AnthropicAgent.predict = lambda *a, **kw: ('response', [action])
print(json.dumps(agent.predict('instruction', {})[1][0]))
""",
            json.dumps(inputs),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    action = json.loads(result.stdout)
    events = []
    env = _step_env(events)
    _desktop_step(patched_checkout, lambda seconds: events.append(("sleep", seconds)))(
        env, action, pause=2
    )
    assert events[0] == ("sleep", 240)
    assert events[1][0] == "guest" and "DEVNULL" in events[1][1]
    assert "pyperclip.copy('café')" in events[1][1]
    assert events[2][0] == "guest" and "pyautogui.click(960, 540)" in events[2][1]
    assert events[3:] == [("sleep", 2), ("observe",)]
    assert env.action_history == [action] and env._step_no == 1
    assert action["input"] == inputs


def test_native_unicode_clipboard_daemon_cannot_hold_execute_response_open(
    patched_checkout, tmp_path, monkeypatch
):
    # Model xclip's real daemon behavior: the copy process exits, but its child
    # keeps inherited stdout/stderr open. The real guest /execute function
    # must still finish and preserve ordinary captured command output.
    clipboard = tmp_path / "clipboard.txt"
    xclip = tmp_path / "xclip"
    xclip.write_text(
        f"#!{sys.executable}\n"
        "import os,sys,time\n"
        f"open({str(clipboard)!r}, 'wb').write(sys.stdin.buffer.read())\n"
        "if os.fork() == 0:\n"
        "    time.sleep(2)\n"
        "    os._exit(0)\n"
    )
    xclip.chmod(0o755)
    (tmp_path / "pyperclip.py").write_text(
        "import subprocess\n"
        "def copy(text):\n"
        "    p = subprocess.Popen(['xclip', '-selection', 'c'], stdin=subprocess.PIPE, close_fds=True)\n"
        "    p.communicate(input=text.encode('utf-8'))\n"
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    source = ast.parse((ROOT / "template/files/server/src/http_routes.py").read_text())
    register = next(
        n
        for n in source.body
        if isinstance(n, ast.FunctionDef) and n.name == "register_http_routes"
    )
    execute = next(
        n
        for n in register.body
        if isinstance(n, ast.FunctionDef) and n.name == "execute_command"
    )
    execute.decorator_list = []
    request = SimpleNamespace(json={})
    namespace = {
        "request": request,
        "jsonify": lambda data: data,
        "subprocess": subprocess,
        "platform_name": "Linux",
        "os": os,
        "shlex": shlex,
    }
    exec(
        compile(ast.Module(body=[execute], type_ignores=[]), "http_routes.py", "exec"),
        namespace,
    )
    responses = []

    def execute_python(command):
        request.json = {
            "command": [sys.executable, "-c", command],
            "shell": False,
            "timeout": 1,
        }
        responses.append(namespace["execute_command"]())

    env = _step_env([])
    env.controller.execute_python_command = execute_python
    text = "Clipboard caf\u00e9 \u65e5\u672c\u8a9e"
    command = f"import pyperclip\npyperclip.copy({text!r})\nprint('ordinary stdout retained')\n"
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": "type", "text": text},
        "command": command,
    }
    _desktop_step(patched_checkout, lambda _: None)(env, action, pause=0)
    assert responses == [
        {
            "status": "success",
            "output": "ordinary stdout retained\n",
            "error": "",
            "returncode": 0,
        }
    ]
    assert clipboard.read_bytes() == text.encode("utf-8")
    assert action["command"] == command and env.action_history == [action]


@pytest.mark.parametrize(
    "provider,space,text",
    [
        ("e2b", "claude_computer_use", "ASCII"),
        ("aws", "claude_computer_use", "caf\u00e9"),
        ("e2b", "pyautogui", "caf\u00e9"),
    ],
)
def test_unicode_clipboard_repair_leaves_other_execution_unchanged(
    patched_checkout, provider, space, text
):
    events = []
    action = {
        "name": "computer",
        "action_type": "tool_use",
        "input": {"action": "type", "text": text},
        "command": "original command",
    }
    _desktop_step(patched_checkout, lambda _: None)(
        _step_env(events, provider, space), action, pause=0
    )
    assert events == [("guest", "original command"), ("observe",)]


def test_native_claude_patches_preserve_every_other_byte_and_are_idempotent(
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
    ).replace(
        '                    "super_l": "win",\n                    "super": "command",\n',
        '                    "super_l": "win",\n                    "super": "win",\n',
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


def test_setup_maps_claude_super_key_to_x11_win(tmp_path):
    dest = tmp_path / "OSWorld-V2"
    _seed_minimal_checkout(
        dest,
        PINNED_PARSER_HEAD + PINNED_KEY_TABLE + PINNED_INFEASIBLE,
        PINNED_CONTROLLER,
    )
    main = dest / "mm_agents" / "anthropic" / "main.py"
    main.parent.mkdir(parents=True)
    # Patch (n) shares this file, so the seed must carry its anchor too.
    main.write_text(
        PINNED_ANTHROPIC_KEY_TABLE
        + "            betas.append(PROMPT_CACHING_BETA_FLAG)\n"
    )
    _apply_patches(dest)
    patched = main.read_text()
    assert '                    "super": "win",' in patched
    assert '"super": "command"' not in patched
    assert '                    "super_l": "win",' in patched  # neighbours untouched
    _apply_patches(dest)  # idempotent
    assert patched == main.read_text()


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
