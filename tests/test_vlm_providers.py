"""VLM 适配器的请求形状与响应解析：用假客户端 / 假 HTTP 服务，不需要真密钥。"""
import base64
import json
import threading

import pytest
from fastapi import FastAPI, Request

from app.vlm.base import SYSTEM_PROMPT


class _Block:
    def __init__(self, text):
        self.type, self.text = 'text', text


class _Resp:
    def __init__(self, text, stop_reason='end_turn', model='claude-opus-5'):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.model = model
        self.usage = type('U', (), {'input_tokens': 100, 'output_tokens': 12})()
        self.stop_details = None


class _FakeMessages:
    def __init__(self, resp, record):
        self.resp, self.record = resp, record

    def create(self, **kw):
        self.record.append(kw)
        return self.resp


def _fake_client(resp, record):
    c = type('C', (), {})()
    c.beta = type('B', (), {})()
    c.beta.messages = _FakeMessages(resp, record)
    c.messages = _FakeMessages(resp, record)
    return c


def test_anthropic_provider_request_shape_and_parse():
    from app.vlm.anthropic_provider import AnthropicVlm
    v = AnthropicVlm(api_key='sk-test', model='claude-opus-5', fallbacks=True)
    record = []
    v.client = _fake_client(_Resp('{"answer": "no", "reason": "门开着"}'), record)
    r = v.ask_yes_no([(b'\xff\xd8jpeg', 'image/jpeg')], '门是否关好？')
    assert r.answer == 'no' and r.provider == 'anthropic' and r.extra['input_tokens'] == 100
    kw = record[0]
    assert kw['model'] == 'claude-opus-5' and kw['output_config'] == {'effort': 'low'} and kw['system'] == SYSTEM_PROMPT
    assert kw['betas'] == ['server-side-fallback-2026-07-01'] and kw['fallbacks'] == 'default'
    content = kw['messages'][0]['content']
    assert content[0]['type'] == 'image' and content[0]['source']['media_type'] == 'image/jpeg'
    assert base64.standard_b64decode(content[0]['source']['data']) == b'\xff\xd8jpeg'
    assert content[-1] == {'type': 'text', 'text': '门是否关好？'}
    assert 'thinking' not in kw                        # Opus 5 默认自适应，不传 thinking


def test_anthropic_provider_refusal_and_no_fallbacks():
    from app.vlm.anthropic_provider import AnthropicVlm
    v = AnthropicVlm(api_key='sk-test', fallbacks=False)
    record = []
    resp = _Resp('', stop_reason='refusal')
    resp.stop_details = type('S', (), {'category': 'x', 'explanation': '不便回答'})()
    v.client = _fake_client(resp, record)
    r = v.ask_yes_no([(b'x', 'image/jpeg')], 'q')
    assert r.answer == 'unknown' and '拒绝' in r.error
    assert 'fallbacks' not in record[0]


def test_anthropic_provider_falls_back_when_sdk_lacks_fallbacks_kwarg():
    from app.vlm.anthropic_provider import AnthropicVlm
    v = AnthropicVlm(api_key='sk-test', fallbacks=True)
    record = []

    class OldMessages(_FakeMessages):
        def create(self, **kw):
            if 'fallbacks' in kw:
                raise TypeError("unexpected keyword argument 'fallbacks'")
            return super().create(**kw)
    c = _fake_client(_Resp('yes'), record)
    c.beta.messages = OldMessages(_Resp('yes'), record)
    v.client = c
    assert v.ask_yes_no([(b'x', 'image/jpeg')], 'q').answer == 'yes'
    assert 'fallbacks' not in record[-1]


@pytest.fixture(scope='module')
def fake_openai():
    from mock_gateway.server import serve_in_thread
    from tests.conftest import free_port
    app = FastAPI()
    seen = []

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        body = await request.json()
        seen.append({'body': body, 'auth': request.headers.get('authorization')})
        return {'choices': [{'message': {'role': 'assistant', 'content': '{"answer":"yes","reason":"ok"}'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
    port = free_port()
    server, _ = serve_in_thread(app, port=port)
    yield f'http://127.0.0.1:{port}/v1', seen
    server.should_exit = True


def test_openai_compat_provider(fake_openai):
    from app.vlm.openai_compat import OpenAICompatVlm
    base, seen = fake_openai
    v = OpenAICompatVlm(base, 'qwen2.5vl:7b', 'k-123')
    r = v.ask_yes_no([(b'img1', 'image/jpeg'), (b'img2', 'image/jpeg')], '问题？')
    assert r.answer == 'yes' and r.extra['usage']['prompt_tokens'] == 10
    req = seen[-1]
    assert req['auth'] == 'Bearer k-123' and req['body']['model'] == 'qwen2.5vl:7b' and req['body']['temperature'] == 0
    msgs = req['body']['messages']
    assert msgs[0]['role'] == 'system' and msgs[0]['content'] == SYSTEM_PROMPT
    parts = msgs[1]['content']
    assert parts[0] == {'type': 'text', 'text': '问题？'}
    assert len([p for p in parts if p['type'] == 'image_url']) == 2
    assert parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,')


def test_openai_compat_network_error_is_error_answer():
    from app.vlm.openai_compat import OpenAICompatVlm
    v = OpenAICompatVlm('http://127.0.0.1:1/v1', 'm', '', timeout=2)
    r = v.ask_yes_no([(b'x', 'image/jpeg')], 'q')
    assert r.answer == 'error' and '网络' in r.error


def test_build_provider_switch(tmp_path, monkeypatch):
    from app.config import Config
    from app.vlm.base import build_provider
    for k, v in {'VLM_PROVIDER': 'openai_compat', 'VLM_BASE_URL': 'http://x/v1', 'VLM_MODEL': 'm'}.items():
        monkeypatch.setenv(k, v)
    assert build_provider(Config(env_file=tmp_path / 'no.env')).name == 'openai_compat'
    monkeypatch.setenv('VLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    assert build_provider(Config(env_file=tmp_path / 'no.env')).name == 'anthropic'
    monkeypatch.setenv('VLM_PROVIDER', 'mock')
    assert build_provider(Config(env_file=tmp_path / 'no.env')).name == 'mock'


def test_qwen_provider_defaults_and_enable_thinking(fake_openai):
    """DashScope 非流式调用必须显式带 enable_thinking:false，否则 400；base_url 默认走兼容模式地址。"""
    from app.vlm.openai_compat import QwenVlm
    base, seen = fake_openai
    v = QwenVlm(model='qwen3.5-flash')
    assert v.base_url == 'https://dashscope.aliyuncs.com/compatible-mode/v1' and v.name == 'qwen'
    assert v.extra_body == {'enable_thinking': False}
    v = QwenVlm(base, 'qwen3.5-flash', 'sk-test', extra_body={'vl_high_resolution_images': True})
    r = v.ask_yes_no([(b'img', 'image/jpeg')], '有红色圆柱吗？')
    assert r.answer == 'yes' and r.provider == 'qwen'
    body = seen[-1]['body']
    assert body['enable_thinking'] is False and body['vl_high_resolution_images'] is True
    assert body['model'] == 'qwen3.5-flash' and seen[-1]['auth'] == 'Bearer sk-test'
    assert body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert 'qwen' in v.describe() and 'qwen3.5-flash' in v.describe()


def test_build_provider_qwen_reads_extra_body(tmp_path, monkeypatch):
    from app.config import Config
    from app.vlm.base import build_provider
    monkeypatch.setenv('VLM_PROVIDER', 'qwen')
    monkeypatch.setenv('VLM_BASE_URL', '')
    monkeypatch.setenv('VLM_MODEL', 'qwen3-vl-plus')
    monkeypatch.setenv('VLM_EXTRA_BODY', '{"seed": 7}')
    v = build_provider(Config(env_file=tmp_path / 'no.env'))
    assert v.name == 'qwen' and v.model == 'qwen3-vl-plus'
    assert v.extra_body == {'enable_thinking': False, 'seed': 7}
    monkeypatch.setenv('VLM_EXTRA_BODY', '不是 JSON')          # 写坏了只忽略，不炸
    assert build_provider(Config(env_file=tmp_path / 'no.env')).extra_body == {'enable_thinking': False}


def test_openai_compat_surfaces_server_error_message():
    """服务端的 {"error": {"message": ...}} 要原样带出来，否则排查密钥/模型名很痛苦。"""
    import json as _json
    from mock_gateway.server import serve_in_thread
    from tests.conftest import free_port
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from app.vlm.openai_compat import QwenVlm
    app = FastAPI()

    @app.post('/v1/chat/completions')
    def boom():
        return JSONResponse({'error': {'message': 'parameter.enable_thinking must be set to false for non-streaming calls',
                                       'type': 'invalid_request_error', 'code': 'InvalidParameter'}}, status_code=400)
    port = free_port()
    server, _ = serve_in_thread(app, port=port)
    try:
        r = QwenVlm(f'http://127.0.0.1:{port}/v1', 'qwen3.5-flash', 'sk-x').ask_yes_no([(b'i', 'image/jpeg')], 'q')
        assert r.answer == 'error' and 'enable_thinking' in r.error and 'InvalidParameter' in r.error
        assert _json.loads(r.raw)['error']['code'] == 'InvalidParameter'
    finally:
        server.should_exit = True
