"""故障场景：掉线、控制权被抢、丢定位、暂停/继续、跳过。"""
import time

from tests.conftest import init_robot
from tests.test_e2e_run import make_task, wait_run


def _wait_leg_status(client, run_id, statuses, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f'/api/runs/{run_id}').json()
        if r['legs'] and r['legs'][0]['status'] in statuses:
            return r
        time.sleep(0.2)
    raise AssertionError(f'leg 未进入 {statuses}: {r["status"]} {[l["status"] for l in r["legs"]]}')


def test_offline_mid_leg_fails_fast(app_client, mock_robot):
    init_robot(app_client, '1')
    mock_robot.speed = 1.0
    try:
        tid = make_task(app_client, nodes=('20',), offline_timeout=6)
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('dispatched', 'navigating'))
        mock_robot.set_online(False)
        run = wait_run(app_client, run_id, timeout=60)
        assert run['status'] == 'failed' and '掉线' in run['error'], run['error']
        assert any(e['type'] == 'offline' for e in app_client.get('/api/events?source=cloud').json()['items'])
    finally:
        mock_robot.set_online(True)
        mock_robot.speed = 10.0


def test_lease_held_by_human_blocks_preflight(app_client, mock_robot):
    init_robot(app_client, '1')
    mock_robot.preempt('admin', 5)
    tid = make_task(app_client, nodes=('5',))
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'], timeout=30)
    assert run['status'] == 'aborted' and '控制权被 admin 持有' in run['error']


def test_lease_preempted_mid_run_gives_409_then_abort(app_client, mock_robot):
    """第一段导航途中现场有人抢走控制权 → 第二段下发 409 → 执行器记录并等待重试 → 人工中止。"""
    init_robot(app_client, '1')
    mock_robot.speed = 3.0
    try:
        tid = make_task(app_client, nodes=('5', '20'), max_retries=0)
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('dispatched', 'navigating'))
        mock_robot.preempt('admin', 60)                                     # 人抢走控制权（程序抢不回来）
        t0 = time.time()
        while time.time() - t0 < 30:
            r = app_client.get(f'/api/runs/{run_id}').json()
            if any(e['type'] == 'leg_dispatch_409' for e in r['events']):
                break
            time.sleep(0.3)
        assert any(e['type'] == 'leg_dispatch_409' for e in r['events']), [e['type'] for e in r['events']]
        assert r['legs'][0]['status'] == 'done' and r['legs'][1]['status'] == 'failed'
        assert app_client.post(f'/api/runs/{run_id}/abort').status_code == 200
        run = wait_run(app_client, run_id, timeout=40)
        assert run['status'] == 'aborted'
    finally:
        mock_robot.preempt('admin', 0)
        mock_robot.speed = 10.0


def test_pause_resume_and_skip(app_client, mock_robot):
    init_robot(app_client, '1')
    mock_robot.speed = 2.0
    try:
        tid = make_task(app_client, nodes=('3', '20', '5'))
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('dispatched', 'navigating'))
        assert app_client.post(f'/api/runs/{run_id}/pause').status_code == 200
        # 第 1 段（很短）完成后应停在 paused
        t0 = time.time()
        while time.time() - t0 < 30:
            r = app_client.get(f'/api/runs/{run_id}').json()
            if r['status'] == 'paused':
                break
            time.sleep(0.3)
        assert r['status'] == 'paused' and r['legs'][0]['status'] == 'done'
        assert app_client.post(f'/api/runs/{run_id}/resume').status_code == 200
        # 第 2 段（长）导航中跳过
        t0 = time.time()
        while time.time() - t0 < 30:
            r = app_client.get(f'/api/runs/{run_id}').json()
            if r['legs'][1]['status'] in ('dispatched', 'navigating'):
                break
            time.sleep(0.2)
        assert app_client.post(f'/api/runs/{run_id}/skip').status_code == 200
        run = wait_run(app_client, run_id, timeout=90)
        assert run['status'] == 'completed'
        assert [l['status'] for l in run['legs']] == ['done', 'skipped', 'done']
        assert len(run['inspections']) == 2                      # 跳过的段不检查
        assert run['legs'][2]['from_node'] not in (None, '')      # 跳过后重新定位当前航点
    finally:
        mock_robot.speed = 10.0


def test_lost_localization_logged_but_continues(app_client, mock_robot):
    init_robot(app_client, '1')
    mock_robot.speed = 3.0
    try:
        tid = make_task(app_client, nodes=('20',), lost_localization_action='continue')
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('dispatched', 'navigating'))
        mock_robot.lose_localization(1.0)
        run = wait_run(app_client, run_id, timeout=60)
        assert run['status'] == 'completed'
        assert any(e['type'] == 'lost_localization' for e in run['events'])
    finally:
        mock_robot.speed = 10.0


def test_stall_detection_when_robot_keeps_avoiding(app_client, mock_robot):
    """任务 active 但一直避障不前进 → 超过 stall_timeout 判段失败，而不是等满 leg_timeout。"""
    init_robot(app_client, '1')
    mock_robot.speed = 2.0
    try:
        tid = make_task(app_client, nodes=('20',), stall_timeout=4, leg_timeout=120)
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        mock_robot.obstacle(60)                                   # 持续避障，不再产生到达
        run = wait_run(app_client, run_id, timeout=40)
        assert run['status'] == 'failed' and '停滞' in run['error'], run['error']
        assert any(e['type'] == 'obstacle' for e in app_client.get('/api/events?source=cloud').json()['items'])
    finally:
        mock_robot.avoiding_until = 0
        mock_robot.speed = 10.0


def test_external_task_replaces_ours_aborts_without_stopping_it(app_client, mock_robot):
    """导航途中现场下发了别的任务（task_started 的 path 不是本段的）→ 本次执行中止，且不去停对方的任务。"""
    from tests.conftest import DEMO_MAP
    init_robot(app_client, '1')
    mock_robot.speed = 1.5
    try:
        tid = make_task(app_client, nodes=('20',))
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        mock_robot.start_task(DEMO_MAP, ['3', '4', '5'], {})            # 现场直接下发了另一条路径
        run = wait_run(app_client, run_id, timeout=30)
        assert run['status'] == 'aborted' and '外部替换' in run['error'], run['error']
        assert any(e['type'] == 'external_task' for e in run['events'])
        st = mock_robot.snapshot_state()['task']
        assert st['path'] == ['3', '4', '5'] and st['status'] in ('navigating', 'completed')   # 没有被我们停掉
    finally:
        mock_robot.speed = 10.0


def test_timestamps_carry_timezone(app_client):
    ev = app_client.get('/api/events?limit=1').json()['items'][0]
    assert ev['ts'][-6] in '+-' and ev['ts'][-3] == ':'                     # …+08:00


def test_lost_localization_pauses_until_relocalized(app_client, mock_robot):
    """默认策略：丢定位 → 停云端任务、段回到 pending、执行暂停；人工重新定位后「继续」→ 从当前最近航点重规划并完成。"""
    from tests.conftest import DEMO_MAP
    init_robot(app_client, '1')
    mock_robot.speed = 2.0
    try:
        tid = make_task(app_client, nodes=('20',))
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        mock_robot.lose_localization(2.0)
        t0 = time.time()
        while time.time() - t0 < 20:
            r = app_client.get(f'/api/runs/{run_id}').json()
            if r['status'] == 'paused':
                break
            time.sleep(0.2)
        assert r['status'] == 'paused' and r['legs'][0]['status'] == 'pending' and '丢定位' in (r['legs'][0]['error'] or '')
        assert mock_robot.snapshot_state()['task']['status'] in ('idle', 'stopped')           # 云端任务已被停下
        assert any(e['type'] == 'run_pause_lost_localization' for e in r['events'])
        time.sleep(2.2)                                                                        # 定位恢复
        pos = mock_robot.snapshot_state()
        near = min(mock_robot.maps[DEMO_MAP], key=lambda k: (mock_robot.maps[DEMO_MAP][k]['pose']['position']['x'] - pos['x']) ** 2 + (mock_robot.maps[DEMO_MAP][k]['pose']['position']['y'] - pos['y']) ** 2)
        assert app_client.post('/api/robot/init/localize', json={'map_name': DEMO_MAP, 'node_id': near}).status_code == 200
        mock_robot.speed = 10.0
        assert app_client.post(f'/api/runs/{run_id}/resume').status_code == 200
        run = wait_run(app_client, run_id, timeout=60)
        assert run['status'] == 'completed' and run['legs'][0]['status'] == 'done'
        assert run['legs'][0]['attempt'] == 2 and run['legs'][0]['idempotency_key'].endswith('-a2')   # 重新下发必须换新幂等键
        assert run['legs'][0]['from_node'] == near and run['legs'][0]['path'][0] == near   # 从重新定位时的最近航点重规划
    finally:
        mock_robot.speed = 10.0


def test_gateway_html_502_during_leg_is_tolerated(app_client, mock_robot):
    """真实网关偶发 nginx 层 HTML 502：对账失败只记 warn（且不把 HTML 写进事件），下一次对账正常，段照常到达。"""
    init_robot(app_client, '1')
    mock_robot.speed = 2.0
    try:
        tid = make_task(app_client, nodes=('20',))
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        mock_robot.html502_only_task = True                  # 只让 GET /task 回 HTML 502：状态轮询每秒一次 + 执行器 5 s 一次对账
        mock_robot.html502_left = 30
        run = wait_run(app_client, run_id, timeout=90)
        assert run['status'] == 'completed'
        msgs = [e['message'] for e in run['events'] if e['type'] == 'reconcile_failed']
        assert msgs and all('<' not in m for m in msgs) and any('502' in m for m in msgs)
    finally:
        mock_robot.speed = 10.0
        mock_robot.html502_left = 0
        mock_robot.html502_only_task = False


def test_abort_confirms_cloud_task_stopped(app_client, mock_robot):
    init_robot(app_client, '1')
    mock_robot.speed = 1.0
    try:
        tid = make_task(app_client, nodes=('20',))
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        app_client.post(f'/api/runs/{run_id}/abort')
        run = wait_run(app_client, run_id, timeout=40)
        assert run['status'] == 'aborted'
        assert any(e['type'] == 'task_stop_confirmed' for e in run['events'])
    finally:
        mock_robot.speed = 10.0


def test_abort_when_robot_ignores_stop_raises_alarm(app_client, mock_robot):
    """机器人端对 DELETE /task 回 200 却不停（Gazebo 实测）：中止后要记 error 事件提醒现场，而不是假装停了。"""
    init_robot(app_client, '1')
    mock_robot.speed = 0.6
    mock_robot.ignore_stop = True
    try:
        tid = make_task(app_client, nodes=('20',), stop_wait_seconds=4)
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        app_client.post(f'/api/runs/{run_id}/abort')
        run = wait_run(app_client, run_id, timeout=60)
        assert run['status'] == 'aborted'
        assert any(e['type'] == 'task_stop_unconfirmed' and e['level'] == 'error' for e in run['events'])
        assert mock_robot.snapshot_state()['task']['active'] is True                 # 机器人确实还在走
    finally:
        mock_robot.ignore_stop = False
        mock_robot.task = mock_robot._idle_task()
        mock_robot.speed = 10.0


def test_auto_estop_when_stop_unconfirmed(app_client, mock_robot):
    """任务选项 estop_if_stop_unconfirmed：停止指令被机器人忽略 → 自动软件急停，机器人停止移动。"""
    init_robot(app_client, '1')
    mock_robot.speed = 0.6
    mock_robot.ignore_stop = True
    try:
        tid = make_task(app_client, nodes=('20',), stop_wait_seconds=4, estop_if_stop_unconfirmed=True)
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        _wait_leg_status(app_client, run_id, ('navigating',))
        app_client.post(f'/api/runs/{run_id}/abort')
        run = wait_run(app_client, run_id, timeout=60)
        assert run['status'] == 'aborted'
        types = [e['type'] for e in run['events']]
        assert 'task_stop_unconfirmed' in types and 'estop' in types
        assert mock_robot.emergency_active is True
        x1 = mock_robot.snapshot_state()['x']; time.sleep(1.0); x2 = mock_robot.snapshot_state()['x']
        assert abs(x2 - x1) < 1e-6                                                   # 急停后不再移动
    finally:
        mock_robot.set_estop(False)
        mock_robot.ignore_stop = False
        mock_robot.task = mock_robot._idle_task()
        mock_robot.speed = 10.0
