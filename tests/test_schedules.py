import datetime as dt
import time

import pytest

from app.scheduler import next_run, parse_spec
from tests.conftest import DEMO_MAP, init_robot, sync_map


def test_parse_and_next_run():
    assert parse_spec('daily', '07:00, 19:30') == [(7, 0), (19, 30)]
    assert parse_spec('interval', '120') == [120.0]
    with pytest.raises(ValueError):
        parse_spec('daily', '25:00')
    with pytest.raises(ValueError):
        parse_spec('interval', '0.5')
    with pytest.raises(ValueError):
        parse_spec('weekly', '1')
    now = dt.datetime(2026, 9, 4, 8, 15, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert next_run('daily', '07:00,19:30', now) == now.replace(hour=19, minute=30, second=0, microsecond=0)
    assert next_run('daily', '07:00', now) == (now + dt.timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    assert next_run('interval', '90', now) == now + dt.timedelta(minutes=90)


def test_schedule_crud_and_fire(app_client, mock_robot):
    init_robot(app_client, '1')
    sync_map(app_client)
    tw = app_client.post('/api/task-waypoints', json={'name': 's', 'map_name': DEMO_MAP, 'nav_node_id': '3', 'prompt': 'q'}).json()
    tid = app_client.post('/api/tasks', json={'name': 'sched', 'map_name': DEMO_MAP, 'waypoint_ids': [tw['id']], 'options': {'settle_seconds': 0}}).json()['id']
    r = app_client.post('/api/schedules', json={'task_id': tid, 'kind': 'daily', 'spec': '07:00,19:00'})
    assert r.status_code == 201 and r.json()['next_run_at'] and r.json()['task_name'] == 'sched'
    sid = r.json()['id']
    assert app_client.post('/api/schedules', json={'task_id': tid, 'kind': 'daily', 'spec': 'x'}).status_code == 400
    assert app_client.post('/api/schedules', json={'task_id': 9999, 'kind': 'interval', 'spec': '5'}).status_code == 404
    r = app_client.put(f'/api/schedules/{sid}', json={'task_id': tid, 'kind': 'interval', 'spec': '60', 'enabled': False})
    assert r.json()['enabled'] == 0 and r.json()['next_run_at'] is None
    assert len(app_client.get(f'/api/schedules?task_id={tid}').json()['items']) == 1
    # 立即触发：调度器 tick 开始一次执行，并把 next_run_at 拨到下一次
    r = app_client.post(f'/api/schedules/{sid}/fire')
    assert r.status_code == 200 and r.json()['fired'] == 1
    sch = r.json()['schedule']
    assert sch['last_result'].startswith('started run') and sch['next_run_at'] > sch['last_run_at']
    run_id = int(sch['last_result'].split()[-1])
    t0 = time.time()
    while time.time() - t0 < 60:
        run = app_client.get(f'/api/runs/{run_id}').json()
        if run['status'] in ('completed', 'failed', 'aborted'):
            break
        time.sleep(0.5)
    assert run['status'] == 'completed'
    ev = app_client.get('/api/events?type=schedule_fired').json()['items']
    assert ev and ev[0]['run_id'] == run_id
    # 有执行在跑时再触发 → 跳过并记录
    mock_robot.speed = 0.5
    try:
        run2 = app_client.post(f'/api/tasks/{tid}/run').json()
        r = app_client.post(f'/api/schedules/{sid}/fire')
        assert r.json()['fired'] == 0 and r.json()['schedule']['last_result'].startswith('skipped')
        assert app_client.post(f"/api/runs/{run2['id']}/abort").status_code == 200
    finally:
        mock_robot.speed = 10.0
    assert app_client.delete(f'/api/schedules/{sid}').status_code == 200
    assert app_client.get('/api/schedules').json()['items'] == []
