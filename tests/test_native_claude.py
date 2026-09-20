"""Exercise the real pinned agent/SDK with an offline HTTP transport."""

from pathlib import Path
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_PYTHON = ROOT / "OSWorld-V2" / ".venv" / "bin" / "python"


@pytest.mark.skipif(
    not UPSTREAM_PYTHON.exists(), reason="requires the upstream runtime from setup.sh"
)
def test_native_agent_sends_effective_settings_and_preserves_tool_action_dictionaries():
    result = subprocess.run(
        [
            str(UPSTREAM_PYTHON),
            "-c",
            textwrap.dedent("""
            import sys, os, io, json
            from pathlib import Path
            sys.path[:0] = [str(Path('runner').resolve()), str(Path('OSWorld-V2').resolve())]
            os.environ['MODEL_BASE_URL'] = 'https://bedrock-mantle.us-east-1.api.aws/anthropic'
            os.environ['MODEL_API_KEY'] = 'offline-test-key'
            import httpx, anthropic
            from PIL import Image
            from agents import agent_settings, build_agent
            from evaluator_model_calls import EvaluatorModelCallTracker
            from types import SimpleNamespace
            import mm_agents.anthropic.main as native

            tracker = EvaluatorModelCallTracker()
            tracker.install(
                model_client=SimpleNamespace(generate_text=lambda: 'unused', generate_chat=lambda: 'unused'),
                llm_metrics=SimpleNamespace(generate_text=lambda: 'unused'),
            )

            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(200, json={
                    'id':'msg_test', 'type':'message', 'role':'assistant',
                    'model':'anthropic.claude-opus-5',
                    'content':[{'type':'tool_use','id':'tool_test','name':'computer',
                                'input':{'action':'left_click','coordinate':[640,360]}}],
                    'stop_reason':'tool_use','stop_sequence':None,
                    'usage':{'input_tokens':1,'output_tokens':1},
                })
            real_client = anthropic.Anthropic
            native.Anthropic = lambda **kwargs: real_client(
                **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
            )
            settings = agent_settings('claude', max_tokens=8192, max_trajectory_length=6, max_steps=37)
            agent = build_agent('claude', model='anthropic.claude-opus-5', settings=settings,
                                client_password='osworld-public-evaluation')
            assert isinstance(agent, native.AnthropicAgent)
            png = io.BytesIO()
            Image.new('RGB',(1920,1080)).save(png,format='PNG')
            response, actions = agent.predict('Click the center.', {'screenshot':png.getvalue()})
            assert len(requests) == 1
            assert tracker.usage['agent'] == {
                'calls': 1, 'input_tokens': 1, 'output_tokens': 1, 'unmeasured_calls': 0,
            }
            assert tracker.usage['judge']['calls'] == tracker.usage['simulator']['calls'] == 0
            request = requests[0]
            payload = json.loads(request.content)
            assert request.url.host == 'bedrock-mantle.us-east-1.api.aws'
            assert request.url.path == '/anthropic/v1/messages'
            assert request.headers['authorization'] == 'Bearer offline-test-key'
            assert request.headers['x-api-key'] == 'offline-test-key'
            assert request.headers['anthropic-beta'] == 'computer-use-2025-11-24'
            assert payload['system'][0]['cache_control'] == {'type': 'ephemeral'}
            assert any(block.get('cache_control') == {'type': 'ephemeral'}
                       for message in payload['messages'] for block in message['content'])
            assert payload['model'] == 'anthropic.claude-opus-5'
            assert payload['max_tokens'] == settings['max_tokens'] == 16000
            assert payload['thinking'] == {'type':'adaptive'}
            assert payload['output_config'] == {'effort':'max'}
            assert 'temperature' not in payload and 'top_p' not in payload
            assert agent.only_n_most_recent_images == 6 and agent.max_steps == 37
            assert len(actions) == 1 and isinstance(actions[0], dict)
            assert actions[0]['input']['coordinate'] == [640,360]
            assert 'pyautogui.click(960, 540)' in actions[0]['command'], actions[0]['command']
        """),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    not UPSTREAM_PYTHON.exists(), reason="requires the upstream runtime from setup.sh"
)
def test_native_agent_rejects_mismatched_profiles_and_propagates_failures_without_changing_questions():
    result = subprocess.run(
        [
            str(UPSTREAM_PYTHON),
            "-c",
            textwrap.dedent("""
            import sys, os, io
            from pathlib import Path
            sys.path[:0] = [str(Path('runner').resolve()), str(Path('OSWorld-V2').resolve())]
            os.environ['MODEL_BASE_URL'] = 'https://model.test/anthropic'
            os.environ['MODEL_API_KEY'] = 'offline-test-key'
            import httpx, anthropic
            from PIL import Image
            from agents import agent_settings, build_agent
            import mm_agents.anthropic.main as native

            def build(model='anthropic.claude-opus-5', password='osworld-public-evaluation'):
                return build_agent('claude', model=model, settings=agent_settings('claude'),
                                   client_password=password)

            for model, password, error_fragment in [
                ('anthropic.claude-opus-5', 'custom-test-password', 'password'),
                ('anthropic.claude-opus-4-8', 'osworld-public-evaluation', 'anthropic.claude-opus-4-8'),
                ('anthropic.claude-sonnet-5', 'osworld-public-evaluation', 'anthropic.claude-sonnet-5'),
            ]:
                try:
                    build(model, password)
                except ValueError as error:
                    assert error_fragment in str(error), str(error)
                else:
                    raise AssertionError('invalid native configuration was accepted: ' + error_fragment)

            def handler(request):
                return httpx.Response(200, json={
                    'id':'msg_question', 'type':'message', 'role':'assistant',
                    'model':'anthropic.claude-opus-5',
                    'content':[{'type':'text', 'text':'Which item should I use?'}],
                    'stop_reason':'end_turn', 'stop_sequence':None,
                    'usage':{'input_tokens':1, 'output_tokens':1},
                })
            real_client = anthropic.Anthropic
            native.Anthropic = lambda **kwargs: real_client(
                **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
            )
            agent = build()
            png = io.BytesIO()
            Image.new('RGB', (1920,1080)).save(png, format='PNG')
            assert agent.predict('Choose an item.', {'screenshot':png.getvalue()}) == (
                'Which item should I use?', []
            )

            # A real upstream error branch returns (None, None); do not turn
            # this into ASK_USER and charge the user simulator for a blank query.
            def fail_api(**kwargs):
                raise ValueError('offline response construction failed')
            # The error must occur inside upstream's guarded API call, after
            # its SDK client is constructed.
            from types import SimpleNamespace
            class FailingClient:
                def with_options(self, **kwargs):
                    return self
                beta = SimpleNamespace(messages=SimpleNamespace(create=fail_api))
            native.Anthropic = lambda **kwargs: FailingClient()
            try:
                build().predict('Choose an item.', {'screenshot':png.getvalue()})
            except RuntimeError as error:
                assert 'Claude' in str(error)
            else:
                raise AssertionError('upstream failure sentinel was not raised')
        """),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
