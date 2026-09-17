"""断言 mock 网关复刻了文档里写明的每个「坑」。真机接入前拿 04_verify_flow.py 对照的就是这些点。"""
import time

import requests

from tests.conftest import DEMO_MAP


def H(gw, **extra):
    return {'X-API-Key': gw.key, **extra}


def test_auth_and_envelope(mock_gw, mock_robot):
    r = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.alias}')
    assert r.status_code == 401 and r.json()['success'] is False
    r = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.alias}', headers=H(mock_gw))
    assert r.status_code == 200 and r.json()['data']['alias'] == mock_gw.alias
    r = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.robot_id}', headers=H(mock_gw))
    assert r.status_code == 200                                    # /v1 也认 ID
    assert requests.get(f'{mock_gw.url}/v1').json()['data']['version'] == 'v1'
    assert 'status' in requests.get(f'{mock_gw.url}/v1/status-codes').json()['data']


def test_position_503_until_localized_and_location_always_1(mock_gw, mock_robot):
    r = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.alias}/position', headers=H(mock_gw))
    assert r.status_code == 503
    p = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.alias}/perception', headers=H(mock_gw)).json()['data']
    assert p['Location'] == 1 and p['location_valid'] is False
    mock_robot.device_started = True
    res = mock_robot.localize(DEMO_MAP, '1', None)
    assert res['success'] is True
    r = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.alias}/position', headers=H(mock_gw))
    assert r.status_code == 200 and r.json()['source'] == 'global_localization'
    p = requests.get(f'{mock_gw.url}/v1/robots/{mock_gw.alias}/perception', headers=H(mock_gw)).json()['data']
    assert p['Location'] == 1                                      # 定位正常时仍为 1（已知 bug）


def test_passthrough_alias_gives_502(mock_gw, mock_robot):
    r = requests.post(f'{mock_gw.url}/api/robots/{mock_gw.alias}/api/device/start', headers=H(mock_gw))
    assert r.status_code == 502 and '不在线' in r.json()['error']
    r = requests.post(f'{mock_gw.url}/api/robots/{mock_gw.robot_id}/api/device/start', headers=H(mock_gw))
    assert r.status_code == 200 and r.json()['task_id']
    r = requests.get(f'{mock_gw.url}/api/robots/{mock_gw.robot_id}/api/device/start_status?task_id=nope', headers=H(mock_gw))
    assert r.status_code == 400 and '任务 ID 不存在' in r.json()['error']


def test_localize_failure_modes(mock_gw, mock_robot):
    res = mock_robot.localize(DEMO_MAP, '1', None)
    assert res['success'] is False and res['data']['reason'] == 'timeout'     # 设备没起来
    mock_robot.device_started = True
    res = mock_robot.localize(DEMO_MAP, '20', None)
    assert res['success'] is False and res['data']['reason'] == 'drift_exceeded' and res['data']['drift'] > 3


def test_task_validation_and_status_words(mock_gw, mock_robot):
    R = f'{mock_gw.url}/v1/robots/{mock_gw.alias}'
    r = requests.post(f'{R}/task', headers=H(mock_gw), json={'map': DEMO_MAP, 'waypoints': ['1', '2']})
    assert r.status_code == 400 and 'map_name' in r.json()['error']
    r = requests.post(f'{R}/task', headers=H(mock_gw), json={'map_name': DEMO_MAP, 'path': ['1']})
    assert r.status_code == 400 and '两个航点' in r.json()['error']
    # 未初始化：200 但不动
    r = requests.post(f'{R}/task', headers=H(mock_gw), json={'map_name': DEMO_MAP, 'path': ['1', '2']})
    assert r.status_code == 200
    assert requests.get(f'{R}/task', headers=H(mock_gw)).json()['data']['status'] == 'idle'
    # 初始化后：写 running 读 navigating，起点立刻在 visited 里
    mock_robot.device_started = True
    mock_robot.localize(DEMO_MAP, '1', None)
    cursor = requests.get(f'{R}/events', headers=H(mock_gw)).json()['data']['seq']
    r = requests.post(f'{R}/task', headers=H(mock_gw, **{'Idempotency-Key': 'k1'}), json={'map_name': DEMO_MAP, 'path': ['1', '2', '3']})
    assert r.status_code == 200
    t = requests.get(f'{R}/task', headers=H(mock_gw)).json()['data']
    assert t['status'] == 'nav_preprocess' and t['active'] is True and t['visited'] == ['1'] and t['status_name'] == 'NAV_PREPROCESS'
    for _ in range(30):                                     # 预处理结束后才是 navigating（写 running 读不回 running）
        t = requests.get(f'{R}/task', headers=H(mock_gw)).json()['data']
        if t['status'] == 'navigating':
            break
        time.sleep(0.1)
    assert t['status'] == 'navigating' and t['status_name'] == 'NAVIGATING'
    # 幂等重放
    r2 = requests.post(f'{R}/task', headers=H(mock_gw, **{'Idempotency-Key': 'k1'}), json={'map_name': DEMO_MAP, 'path': ['1', '2', '3']})
    assert r2.headers.get('Idempotent-Replay') == 'true' and r2.json() == r.json()
    # 等跑完
    for _ in range(100):
        t = requests.get(f'{R}/task', headers=H(mock_gw)).json()['data']
        if t['terminal']:
            break
        time.sleep(0.1)
    assert t['status'] == 'completed' and t['status_code'] == 4 and t['visited'] == ['1', '2', '3']
    evs = requests.get(f'{R}/events?since={cursor}', headers=H(mock_gw)).json()['data']['events']
    types = [e['type'] for e in evs]
    assert types[:2] == ['waypoint_reached', 'task_started'] and types[-1] == 'task_completed'
    reached = [e['data']['waypoint'] for e in evs if e['type'] == 'waypoint_reached']
    assert reached == ['1', '2', '3']
    last = [e for e in evs if e['type'] == 'waypoint_reached'][-1]['data']
    assert last['nextTarget'] is None and last['index'] == 2 and last['total'] == 3


def test_task_failed_reads_paused(mock_gw, mock_robot):
    R = f'{mock_gw.url}/v1/robots/{mock_gw.alias}'
    mock_robot.device_started = True
    mock_robot.localize(DEMO_MAP, '1', None)
    mock_robot.inject_fault('obstacle')
    requests.post(f'{R}/task', headers=H(mock_gw), json={'map_name': DEMO_MAP, 'path': ['1', '2']})
    for _ in range(50):
        t = requests.get(f'{R}/task', headers=H(mock_gw)).json()['data']
        if t['terminal']:
            break
        time.sleep(0.1)
    assert t['status'] == 'paused' and t['status_code'] == 255 and t['error_hex'] == '0x234B' and t['error_name'] == 'OBSTACLE_FAILURE'


def test_estop_empty_body_means_clear(mock_gw, mock_robot):
    R = f'{mock_gw.url}/v1/robots/{mock_gw.alias}'
    r = requests.post(f'{R}/estop', headers=H(mock_gw), json={'active': True})
    assert r.json()['data']['active'] is True
    assert requests.get(f'{R}/telemetry', headers=H(mock_gw)).json()['data']['emergency_active'] is True
    r = requests.post(f'{R}/estop', headers=H(mock_gw), json={})
    assert r.json()['data']['active'] is False                       # 空体 = 取消急停


def test_viewer_key_forbidden_and_lease_conflict(mock_gw, mock_robot):
    from mock_gateway.server import VIEWER_KEY
    R = f'{mock_gw.url}/v1/robots/{mock_gw.alias}'
    r = requests.delete(f'{R}/task', headers={'X-API-Key': VIEWER_KEY})
    assert r.status_code == 403
    requests.post(f'{mock_gw.url}/mock/preempt', json={'owner': 'admin', 'seconds': 30})
    r = requests.delete(f'{R}/task', headers=H(mock_gw))
    assert r.status_code == 409 and r.json()['holder'] == 'admin'
    requests.post(f'{mock_gw.url}/mock/preempt', json={'owner': 'admin', 'seconds': 0})
    assert requests.delete(f'{R}/task', headers=H(mock_gw)).status_code == 200


def test_sse_stream_delivers_events(mock_gw, mock_robot):
    R = f'{mock_gw.url}/v1/robots/{mock_gw.alias}'
    cursor = requests.get(f'{R}/events', headers=H(mock_gw)).json()['data']['seq']
    with requests.get(f'{R}/events?since={cursor}&stream=1', headers=H(mock_gw), stream=True, timeout=10) as r:
        assert r.headers['content-type'].startswith('text/event-stream')
        it = r.iter_lines(decode_unicode=True)
        first = next(l for l in it if l)
        assert first.startswith(': connected')
        mock_robot.set_estop(True)
        lines = []
        for line in it:
            lines.append(line)
            if line.startswith('data:'):
                break
        assert any(l == 'event: emergency' for l in lines)


def test_human_preempts_api_lease_but_not_reverse(mock_gw, mock_robot):
    ok, holder = mock_robot.acquire_lease('api:mock0001')
    assert ok
    ok, holder = mock_robot.acquire_lease('admin')            # 程序方式拿不到别人的租约
    assert not ok and holder == 'api:mock0001'
    lease = mock_robot.preempt('admin', 30)                   # 人可以直接抢
    assert lease['owner'] == 'admin'
    ok, holder = mock_robot.acquire_lease('api:mock0001')
    assert not ok and holder == 'admin'
    mock_robot.preempt('admin', 0)
    assert mock_robot.acquire_lease('api:mock0001')[0]


# ── 语音播报（机器人端固件 ≥ 2026-09-14）───────────────────────────────────

def _R(gw):
    return f'{gw.url}/v1/robots/{gw.alias}'


def test_tts_text_validation_queue_and_wait(mock_gw, mock_robot):
    R = _R(mock_gw)
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '  \n '})
    assert r.status_code == 400 and r.json()['error'] == 'text is empty'
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '长' * 301})
    assert r.status_code == 400 and 'too long' in r.json()['error']
    # 不带 wait → 202 入队即返回；中文走 piper，英文走 espeak 兜底
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '前方施工，请注意安全'})
    assert r.status_code == 202 and r.json()['data']['queued'] == 0 and r.json()['data']['engine'].startswith('piper:')
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': 'caution'})
    assert r.status_code == 202 and r.json()['data']['queued'] == 1 and r.json()['data']['engine'] == 'espeak:en'
    # wait=true → 200 + seconds；volume 被机器人记住
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '检查通过', 'wait': True, 'volume': 80})
    assert r.status_code == 200 and r.json()['data']['ok'] is True and r.json()['data']['seconds'] > 0
    st = requests.get(f'{R}/tts', headers=H(mock_gw)).json()['data']
    assert st['available'] is True and st['volume'] == 80 and st['played'] == 3 and st['queue'] == 0 and st['speaking'] is None
    assert st['last']['text'] == '检查通过' and st['engines']['ffmpeg'] is True and st['max_text_len'] == 300
    assert [e['text'] for e in mock_robot.tts_log] == ['前方施工，请注意安全', 'caution', '检查通过']


def test_tts_queue_full_429_interrupt_and_stop(mock_gw, mock_robot):
    R = _R(mock_gw)
    mock_robot.tts_seconds_per_char = 10.0                      # 让队列排住不动
    try:
        for i in range(21):                                     # 1 条在播 + 20 条排队
            assert requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': f'第{i}条'}).status_code == 202
        r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '满了'})
        assert r.status_code == 429 and r.json()['error'] == 'tts queue full' and 'Retry-After' not in r.headers   # 与限流的 429 不同
        st = requests.get(f'{R}/tts', headers=H(mock_gw)).json()['data']
        assert st['queue'] == 20 and st['speaking']['text'] == '第0条'
        # interrupt=true：先清空再播这条
        r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '紧急', 'interrupt': True})
        assert r.status_code == 202 and r.json()['data']['queued'] == 0
        st = requests.get(f'{R}/tts', headers=H(mock_gw)).json()['data']
        assert st['queue'] == 0 and st['speaking']['text'] == '紧急'
        r = requests.delete(f'{R}/tts', headers=H(mock_gw))
        assert r.status_code == 200 and r.json()['data'] == {'dropped': 1}
        assert requests.get(f'{R}/tts', headers=H(mock_gw)).json()['data']['speaking'] is None
    finally:
        mock_robot.tts_seconds_per_char = 0.12


def _wav(n: int = 2000) -> bytes:
    return b'RIFF' + (36 + n).to_bytes(4, 'little') + b'WAVEfmt ' + b'\x10\x00\x00\x00\x01\x00\x01\x00' + \
        (16000).to_bytes(4, 'little') + (32000).to_bytes(4, 'little') + b'\x02\x00\x10\x00data' + n.to_bytes(4, 'little') + b'\x00' * n


def test_tts_audio_three_upload_styles_and_format_sniffing(mock_gw, mock_robot):
    import base64
    R = _R(mock_gw)
    # ① 文件字节直接当请求体，选项走 query
    r = requests.post(f'{R}/tts/audio?wait=1&volume=100', headers=H(mock_gw, **{'Content-Type': 'audio/wav'}), data=_wav())
    assert r.status_code == 200 and r.json()['data']['engine'] == 'audio:wav' and r.json()['data']['seconds'] > 0
    # ② multipart，字段名 file
    r = requests.post(f'{R}/tts/audio', headers=H(mock_gw), files={'file': ('hello.wav', _wav(), 'audio/wav')}, data={'wait': '0'})
    assert r.status_code == 202 and r.json()['data']['engine'] == 'audio:wav'
    # ③ JSON + base64（SDK 用这种）；文件头是 wav，声明 mp3 也按文件头认
    mp3 = b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\xff\xfb\x90\x00' * 200
    r = requests.post(f'{R}/tts/audio', headers=H(mock_gw), json={'audio_b64': base64.b64encode(mp3).decode(), 'format': 'wav'})
    assert r.status_code == 202 and r.json()['data']['engine'] == 'audio:mp3'
    r = requests.post(f'{R}/tts/audio', headers=H(mock_gw), json={'audio_b64': '!!not base64!!'})
    assert r.status_code == 400 and 'base64' in r.json()['error']
    # 文件头认不出且没声明 → 400；声明了就按声明
    junk = b'\x00\x01\x02\x03' * 100
    r = requests.post(f'{R}/tts/audio', headers=H(mock_gw, **{'Content-Type': 'application/octet-stream'}), data=junk)
    assert r.status_code == 400 and 'unrecognized audio format' in r.json()['error']
    r = requests.post(f'{R}/tts/audio?format=aac', headers=H(mock_gw, **{'Content-Type': 'application/octet-stream'}), data=junk)
    assert r.status_code == 202 and r.json()['data']['engine'] == 'audio:aac'
    # 超 5 MB → 413
    r = requests.post(f'{R}/tts/audio', headers=H(mock_gw, **{'Content-Type': 'audio/wav'}), data=b'RIFF' + b'\x00' * (5 * 1024 * 1024))
    assert r.status_code == 413 and '>5MB' in r.json()['error']
    st = requests.get(f'{R}/tts', headers=H(mock_gw)).json()['data']
    assert st['volume'] == 100
    kinds = [(e['kind'], e['format']) for e in mock_robot.tts_log]
    assert kinds == [('audio', 'wav'), ('audio', 'wav'), ('audio', 'mp3'), ('audio', 'aac')]


def test_tts_permissions_lease_firmware_and_idempotency(mock_gw, mock_robot):
    from mock_gateway.server import VIEWER_KEY
    R = _R(mock_gw)
    # viewer 能看状态，不能播
    assert requests.get(f'{R}/tts', headers={'X-API-Key': VIEWER_KEY}).status_code == 200
    assert requests.post(f'{R}/tts', headers={'X-API-Key': VIEWER_KEY}, json={'text': 'x'}).status_code == 403
    # 控制权被现场的人占着 → 409
    requests.post(f'{mock_gw.url}/mock/preempt', json={'owner': 'admin', 'seconds': 30})
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '被占'})
    assert r.status_code == 409 and 'admin' in r.json()['error']
    mock_robot.release_lease('admin')
    # 幂等重放：同键 → 同一个 id + Idempotent-Replay
    hd = H(mock_gw, **{'Idempotency-Key': 'tts-fixed-1'})
    a = requests.post(f'{R}/tts', headers=hd, json={'text': '重放'})
    b = requests.post(f'{R}/tts', headers=hd, json={'text': '重放'})
    assert a.json()['data']['id'] == b.json()['data']['id'] and b.headers.get('Idempotent-Replay') == 'true'
    assert sum(1 for e in mock_robot.tts_log if e['text'] == '重放') == 1
    # 没有 ffmpeg → mp3 503、wav 照常
    requests.post(f'{mock_gw.url}/mock/fault', json={'kind': 'tts_no_ffmpeg'})
    r = requests.post(f'{R}/tts/audio', headers=H(mock_gw, **{'Content-Type': 'audio/mpeg'}), data=b'ID3' + b'\x00' * 100)
    assert r.status_code == 503 and 'ffmpeg missing' in r.json()['error']
    assert requests.post(f'{R}/tts/audio', headers=H(mock_gw, **{'Content-Type': 'audio/wav'}), data=_wav()).status_code == 202
    requests.post(f'{mock_gw.url}/mock/fault', json={'kind': 'tts_unavailable'})
    r = requests.post(f'{R}/tts', headers=H(mock_gw), json={'text': '没声卡'})
    assert r.status_code == 503 and 'unavailable' in r.json()['error']
    # 旧固件：整组端点回一页 HTML 的 404（不是 JSON 信封）
    requests.post(f'{mock_gw.url}/mock/fault', json={'kind': 'tts_firmware_old'})
    for method, path in (('post', '/tts'), ('get', '/tts'), ('delete', '/tts'), ('post', '/tts/audio')):
        r = getattr(requests, method)(f'{R}{path}', headers=H(mock_gw), json={'text': 'x'} if path == '/tts' and method == 'post' else None)
        assert r.status_code == 404 and r.headers['content-type'].startswith('text/html') and 'was not found on the server' in r.text
    assert len(requests.get(f'{mock_gw.url}/v1').json()['data']['endpoints']) == 17
