"""robot 汇出：把播报送到机器人自带扬声器（云端 /tts、/tts/audio），以及 /api/robot/tts 状态与打断。"""
import requests

from app.robot.client import RobotGateway
from app.tts.base import BrowserSink, NullEngine, RobotSpeakerSink, TtsService
from tests.conftest import init_robot
from tests.test_e2e_run import make_task, wait_run
from tests.test_mock_gateway import _wav


def _gw(mock_gw) -> RobotGateway:
    return RobotGateway(mock_gw.url, mock_gw.alias, mock_gw.key, rps=20)


def test_robot_sink_text_mode_queues_and_wait_blocks(mock_gw, mock_robot):
    gw = _gw(mock_gw)
    sink = RobotSpeakerSink(lambda: gw, mode='text')
    out = sink.play('前方施工，请注意安全', None, None, {})
    assert out.startswith('queued（piper:'), out
    assert mock_robot.tts_log[-1] == {**mock_robot.tts_log[-1], 'kind': 'text', 'text': '前方施工，请注意安全', 'wait': False}
    sink2 = RobotSpeakerSink(lambda: gw, mode='text', wait=True, volume=60, interrupt=True)
    out = sink2.play('检查通过', None, None, {})
    assert out.startswith('ok（piper:') and 's）' in out, out
    assert mock_robot.tts_log[-1]['volume'] == 60 and mock_robot.tts_log[-1]['interrupt'] is True and mock_robot.tts_volume == 60
    assert gw.limiter.total == 2                                          # 每次播报都经全局限速器


def test_robot_sink_audio_then_fallback_to_text(mock_gw, mock_robot, tmp_path):
    gw = _gw(mock_gw)
    wav = tmp_path / 'a.wav'
    wav.write_bytes(_wav())
    sink = RobotSpeakerSink(lambda: gw, mode='auto')
    assert sink.play('有音频', wav, '/media/a.wav', {}).startswith('queued（audio:wav')
    assert mock_robot.tts_log[-1]['kind'] == 'audio' and mock_robot.tts_log[-1]['format'] == 'wav'
    # 音频这条路走不通（格式认不出 → 400）→ 自动退回文字，机器人自己念，「永远有声音」
    junk = tmp_path / 'b.bin'
    junk.write_bytes(b'\x00\x01\x02\x03' * 50)
    out = sink.play('退回文字', junk, None, {})
    assert out.startswith('queued（piper:') and '音频被拒 HTTP 400' in out and '已退回文字' in out, out
    assert mock_robot.tts_log[-1]['kind'] == 'text' and mock_robot.tts_log[-1]['text'] == '退回文字'
    # 机器人没有 ffmpeg（mp3 → 503）同样退回
    requests.post(f'{mock_gw.url}/mock/fault', json={'kind': 'tts_no_ffmpeg'})
    mp3 = tmp_path / 'c.mp3'
    mp3.write_bytes(b'ID3' + b'\x00' * 100)
    out = sink.play('没有ffmpeg', mp3, None, {})
    assert '503' in out and '已退回文字' in out and mock_robot.tts_log[-1]['kind'] == 'text'
    # audio 模式不退回；没音频就 skip
    strict = RobotSpeakerSink(lambda: gw, mode='audio')
    assert strict.play('x', None, None, {}) == 'skip（无音频文件）'
    assert strict.play('x', mp3, None, {}).startswith('error: HTTP 503')
    # text 模式即使有音频也只发文字
    assert RobotSpeakerSink(lambda: gw, mode='text').play('只文字', wav, None, {}).startswith('queued（piper:')
    assert mock_robot.tts_log[-1]['kind'] == 'text'


def test_robot_sink_old_firmware_and_lease_conflict_are_explained(mock_gw, mock_robot):
    gw = _gw(mock_gw)
    sink = RobotSpeakerSink(lambda: gw, mode='auto')
    requests.post(f'{mock_gw.url}/mock/preempt', json={'owner': 'admin', 'seconds': 30})
    out = sink.play('被占', None, None, {})
    assert out.startswith('error: HTTP 409') and '现场有人' in out, out
    mock_robot.release_lease('admin')
    requests.post(f'{mock_gw.url}/mock/fault', json={'kind': 'tts_firmware_old'})
    out = sink.play('旧固件', None, None, {})
    assert out.startswith('error: 机器人固件太旧') and 'audio_server' in out and 'html' not in out.lower(), out
    assert sink.last == {'kind': 'text', 'error': 'firmware_old'}
    assert not mock_robot.tts_log


def test_tts_service_describe_includes_robot_options(mock_gw, tmp_path):
    from app.bus import Bus
    gw = _gw(mock_gw)
    svc = TtsService(NullEngine(), [BrowserSink(Bus()), RobotSpeakerSink(lambda: gw, mode='text', volume=70)], tmp_path)
    d = svc.describe()
    assert d['sinks'] == ['browser', 'robot'] and d['robot'] == {'mode': 'text', 'volume': 70, 'wait': False, 'interrupt': False}


def test_api_robot_tts_status_stop_and_test_button(make_client, mock_robot, monkeypatch):
    monkeypatch.setenv('TTS_SINKS', 'browser,robot')
    monkeypatch.setenv('TTS_ROBOT_VOLUME', '55')
    c = make_client()
    s = c.get('/api/settings').json()
    assert s['adapters']['tts']['sinks'] == ['browser', 'robot'] and s['adapters']['tts']['robot']['volume'] == 55
    r = c.post('/api/tts/test', json={'text': '测试播报'})
    assert r.status_code == 200 and r.json()['sinks']['robot'].startswith('queued（piper:'), r.json()
    assert mock_robot.tts_log[-1]['text'] == '测试播报' and mock_robot.tts_log[-1]['volume'] == 55
    st = c.get('/api/robot/tts')
    assert st.status_code == 200 and st.json()['available'] is True and st.json()['volume'] == 55
    r = c.delete('/api/robot/tts')
    assert r.status_code == 200 and 'dropped' in r.json()
    assert any(e['type'] == 'tts_stop' for e in c.get('/api/events?source=system').json()['items'])
    # 设置页改音量 → 适配器重建
    c.put('/api/settings', json={'TTS_ROBOT_VOLUME': '', 'TTS_ROBOT_MODE': 'text'})
    assert c.get('/api/settings').json()['adapters']['tts']['robot'] == {'mode': 'text', 'volume': None, 'wait': False, 'interrupt': False}
    # 旧固件：接口给出明确说明，不是一页 HTML
    requests.post(f'{c.ctx.gateway.host}/mock/fault', json={'kind': 'tts_firmware_old'})
    r = c.get('/api/robot/tts')
    assert r.status_code == 400 and '固件太旧' in r.json()['detail'] and 'html' not in r.json()['detail'].lower()


def test_full_mission_speaks_through_robot(make_client, mock_robot, monkeypatch):
    monkeypatch.setenv('TTS_SINKS', 'robot')
    c = make_client()
    init_robot(c, '1')
    run = wait_run(c, c.post(f'/api/tasks/{make_task(c)}/run').json()['id'])
    assert run['status'] == 'completed', run
    insp = run['inspections']
    assert [i['tts_text'] for i in insp] == ['点5正常', '点20异常', '点43正常']
    assert all(i['tts_status']['robot'].startswith('queued（piper:') for i in insp), [i['tts_status'] for i in insp]
    assert [e['text'] for e in mock_robot.tts_log] == ['点5正常', '点20异常', '点43正常']
    assert all(e['kind'] == 'text' for e in mock_robot.tts_log)           # TTS_ENGINE=none → 机器人本地合成
