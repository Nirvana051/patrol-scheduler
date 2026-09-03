"""服务端 → 浏览器 SSE：hello 帧、状态推送、TTS 指令、断开后释放订阅。

TestClient 对不结束的流式响应会挂住（关闭时等生成器结束），所以这里把同一个 app 再用 uvicorn 起在随机端口，用 requests 流式读。
"""
import json
import threading
import time

import pytest
import requests


@pytest.fixture()
def served(app_client):
    from mock_gateway.server import serve_in_thread
    from tests.conftest import free_port
    port = free_port()
    server, _ = serve_in_thread(app_client.app, port=port)
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True


def _read_events(base: str, want: set, timeout: float = 12.0) -> dict:
    got, kind = {}, None
    with requests.get(f'{base}/api/stream', stream=True, timeout=(5, timeout)) as r:
        assert r.headers['content-type'].startswith('text/event-stream')
        t0 = time.time()
        for raw in r.iter_lines(decode_unicode=True):
            if time.time() - t0 > timeout:
                break
            line = raw or ''
            if line.startswith('event: '):
                kind = line[7:].strip()
            elif line.startswith('data: ') and kind:
                if kind in want and kind not in got:
                    got[kind] = json.loads(line[6:])
                if want <= set(got):
                    break
    return got


def test_stream_hello_status_and_tts(served, app_client):
    threading.Thread(target=lambda: (time.sleep(1.0), app_client.post('/api/tts/test', json={'text': '播报测试'})), daemon=True).start()
    got = _read_events(served, {'hello', 'robot_status', 'tts'})
    assert set(got) == {'hello', 'robot_status', 'tts'}, set(got)
    assert got['hello']['payload']['mode'] == 'mock' and 'status' in got['hello']['payload']
    assert got['robot_status']['payload']['online'] is True
    assert got['tts']['payload']['text'] == '播报测试' and got['tts']['payload']['audio_url'] is None   # TTS_ENGINE=none → 浏览器朗读


def test_stream_run_updates_and_cleanup(served, app_client, mock_robot):
    from tests.conftest import init_robot
    from tests.test_e2e_run import make_task
    init_robot(app_client, '1')
    tid = make_task(app_client, nodes=('3',))
    threading.Thread(target=lambda: (time.sleep(0.8), app_client.post(f'/api/tasks/{tid}/run')), daemon=True).start()
    got = _read_events(served, {'run', 'leg', 'inspection'}, timeout=40)
    assert {'run', 'leg', 'inspection'} <= set(got), set(got)
    assert got['inspection']['payload']['waypoint_name'] == '点3'
    # 客户端断开后，服务端在下一次写入/心跳时释放订阅与活跃计数
    t0 = time.time()
    while (app_client.ctx.bus.subscriber_count or app_client.ctx.status._active_users) and time.time() - t0 < 25:
        time.sleep(0.5)
    assert app_client.ctx.bus.subscriber_count == 0 and app_client.ctx.status._active_users == 0
