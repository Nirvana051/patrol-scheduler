"""本项目自带的播报服务（audio_server，纯标准库）与调度系统的 http 汇出。播放器用 dry 模式，不出声。"""
import threading
import time
from pathlib import Path

import pytest
import requests

from audio_server.server import Engine, serve
from tests.conftest import free_port


@pytest.fixture(scope='module')
def audio_srv(tmp_path_factory):
    port = free_port()
    srv = serve('127.0.0.1', port, token='s3cret', player='dry', engine=Engine('none'), work_dir=tmp_path_factory.mktemp('audio'))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f'http://127.0.0.1:{port}', srv
    srv.shutdown()


def test_health_and_auth(audio_srv):
    base, srv = audio_srv
    h = requests.get(f'{base}/health', timeout=5).json()
    assert h['ok'] and h['player'] == 'dry' and h['engine'] is None and h['queue'] == 0
    r = requests.post(f'{base}/play-audio', data=b'xx', timeout=5)
    assert r.status_code == 401
    r = requests.post(f'{base}/play', json={'text': '你好'}, headers={'X-Audio-Token': 's3cret'}, timeout=5)
    assert r.status_code == 501 and '引擎' in r.json()['error']          # 没配引擎：请直接推音频


def test_play_audio_queue_and_wait(audio_srv):
    base, srv = audio_srv
    H = {'X-Audio-Token': 's3cret', 'Content-Type': 'audio/mpeg'}
    r = requests.post(f'{base}/play-audio?text=第一句&wait=1', data=b'\xff\xfb' * 100, headers=H, timeout=10).json()
    assert r['ok'] and r['finished'] is True
    r2 = requests.post(f'{base}/play-audio?text=第二句', data=b'\xff\xfb' * 100, headers=H, timeout=10).json()
    assert r2['ok']
    t0 = time.time()
    while time.time() - t0 < 5 and not any(p['text'] == '第二句' for p in srv.player.played):
        time.sleep(0.05)
    texts = [p['text'] for p in srv.player.played]
    assert '第一句' in texts and '第二句' in texts and texts.index('第一句') < texts.index('第二句')   # 顺序播放
    assert not list(Path(srv.player.work_dir).glob('play_*'))                                      # 播完即删临时文件
    assert requests.post(f'{base}/stop', headers={'X-Audio-Token': 's3cret'}, timeout=5).json()['ok']
    assert requests.post(f'{base}/play-audio', data=b'', headers=H, timeout=5).status_code == 400


def test_scheduler_http_sink_pushes_audio_then_text(audio_srv, tmp_path):
    base, srv = audio_srv
    from app.tts.base import AudioServerSink, NullEngine, TtsEngine, TtsService

    class WavEngine(TtsEngine):
        name, ext = 'fake', 'wav'

        def synthesize(self, text, out_path):
            out_path.write_bytes(b'RIFF' + b'\x00' * 64)
            return out_path
    sink = AudioServerSink(base, token='s3cret')
    svc = TtsService(WavEngine(), [sink], tmp_path)
    r = svc.speak('消防栓门已关闭')
    assert r['sinks']['http'] == 'ok' and r['audio_url'].endswith('.wav')
    t0 = time.time()
    while time.time() - t0 < 5 and not any(p['text'] == '消防栓门已关闭' for p in srv.player.played):
        time.sleep(0.05)
    assert any(p['text'] == '消防栓门已关闭' and p['file'].endswith('.wav') for p in srv.player.played)
    # 没有音频（引擎 none）→ 走 /play，对方没引擎 → 如实报 501
    svc2 = TtsService(NullEngine(), [sink], tmp_path)
    assert svc2.speak('只有文本')['sinks']['http'].startswith('error: HTTP 501')
    # 口令错
    bad = TtsService(WavEngine(), [AudioServerSink(base, token='wrong')], tmp_path)
    assert bad.speak('x')['sinks']['http'].startswith('error: HTTP 401')


def test_build_tts_http_sink_from_config(tmp_path, monkeypatch):
    from app.bus import Bus
    from app.config import Config
    from app.tts.base import build_tts
    monkeypatch.setenv('TTS_ENGINE', 'none')
    monkeypatch.setenv('TTS_SINKS', 'browser,http')
    monkeypatch.setenv('TTS_AUDIO_SERVER_URL', 'http://127.0.0.1:1/')
    svc = build_tts(Config(env_file=tmp_path / 'no.env'), Bus(), tmp_path)
    assert [s.name for s in svc.sinks] == ['browser', 'http'] and svc.sinks[1].url == 'http://127.0.0.1:1'
    assert svc.speak('x')['sinks']['http'].startswith('error:')          # 连不上如实报错，不抛异常
