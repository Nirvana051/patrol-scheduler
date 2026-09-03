"""机器人操作接口：初始化两步（不等待 + 轮询）、急停/取消、停任务、状态码表缓存、视频地址。"""
import time

from tests.conftest import DEMO_MAP


def test_device_start_poll_then_localize(app_client, mock_robot):
    r = app_client.post('/api/robot/init/device-start', json={'wait': False}).json()
    assert r['task_id'] and r['completed'] is False
    t0 = time.time()
    while time.time() - t0 < 10:
        st = app_client.get(f"/api/robot/init/device-status?task_id={r['task_id']}&starting=true").json()
        if st['completed']:
            break
        time.sleep(0.3)
    assert st['completed'] and st['result_success'] is True and mock_robot.device_started
    assert app_client.get('/api/robot/init/device-status?task_id=nope').status_code == 400        # 云端：400 不是 404
    bad = app_client.post('/api/robot/init/localize', json={'map_name': DEMO_MAP, 'node_id': '20'})   # 机器人在 1，初值给 20 → drift
    assert bad.status_code == 400 and 'drift_exceeded' in bad.json()['detail']
    ok = app_client.post('/api/robot/init/localize', json={'map_name': DEMO_MAP, 'node_id': '1'})
    assert ok.status_code == 200 and ok.json()['drift'] == 0.0
    s = app_client.post('/api/robot/status/refresh').json()
    assert s['localized'] is True and s['position']['x'] == 0.0
    types = [e['type'] for e in app_client.get('/api/events?source=system&limit=50').json()['items']]
    assert 'localize_failed' in types and 'localized' in types and 'device_start' in types


def test_estop_roundtrip_and_stop_task(app_client, mock_robot):
    r = app_client.post('/api/robot/estop', json={'active': True})
    assert r.status_code == 200 and mock_robot.emergency_active is True
    s = app_client.post('/api/robot/status/refresh').json()
    assert s['emergency_active'] is True
    pf = app_client.get('/api/robot/preflight').json()
    assert any(c['key'] == 'emergency' and not c['ok'] and c['fix'] == 'clear_estop' for c in pf['checks'])
    app_client.post('/api/robot/estop', json={'active': False})
    assert mock_robot.emergency_active is False
    assert app_client.delete('/api/robot/task').status_code == 200
    ev = app_client.get('/api/events?type=estop').json()['items']
    assert ev and ev[0]['level'] == 'warn'


def test_device_stop_clears_localization(app_client, mock_robot):
    mock_robot.device_started = True
    mock_robot.localize(DEMO_MAP, '1', None)
    r = app_client.post('/api/robot/init/device-stop', json={'wait': True})
    assert r.status_code == 200 and r.json()['completed']
    assert mock_robot.device_started is False and mock_robot.localized is False
    assert app_client.post('/api/robot/status/refresh').json()['localized'] is False
