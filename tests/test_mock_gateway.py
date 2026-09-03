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
