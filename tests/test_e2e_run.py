"""端到端：初始化 → 建任务 → 分段执行 → 每个任务航点抓图/裁切/VLM/TTS → 事件落库。全部对着 mock 网关。"""
import re
import time

from tests.conftest import DEMO_MAP, init_robot, sync_map


def make_task(client, nodes=('5', '20', '43'), **options):
    sync_map(client)
    ids = []
    for i, nid in enumerate(nodes):
        r = client.post('/api/task-waypoints', json={'name': f'点{nid}', 'map_name': DEMO_MAP, 'nav_node_id': nid, 'prompt': '目标是否正常？',
                                                     'angle_from': 160, 'angle_to': 220, 'answer_template': {'expected': 'yes', 'on_pass': '{name}正常', 'on_fail': '{name}异常'}})
        assert r.status_code == 201, r.text
        ids.append(r.json()['id'])
    opts = {'settle_seconds': 0, 'leg_timeout': 60, 'not_started_timeout': 6, 'max_retries': 0, **options}
    r = client.post('/api/tasks', json={'name': 'e2e', 'map_name': DEMO_MAP, 'waypoint_ids': ids, 'options': opts})
    assert r.status_code == 201, r.text
    return r.json()['id']


def wait_run(client, run_id, timeout=90):
    from tests.conftest import mock_state
    t0 = time.time()
    printed = False
    while time.time() - t0 < timeout:
        r = client.get(f'/api/runs/{run_id}').json()
        if r['status'] in ('completed', 'failed', 'aborted'):
            st = mock_state(client)
            print('[debug] run end:', r['status'], r.get('error'), '| mock:', {k: st[k] for k in ('device_started', 'localized', 'x', 'y')}, 'task', st['task']['status'], st['task']['visited'])
            return r
        if not printed and r['legs'] and r['legs'][0]['status'] in ('dispatched', 'navigating'):
            printed = True
            st = mock_state(client)
            print('[debug] after dispatch:', {k: st[k] for k in ('device_started', 'localized', 'x', 'y')}, 'task', st['task']['status'], st['task']['path'], 'lease', st['lease'])
        time.sleep(0.5)
    raise AssertionError(f'run {run_id} 未在 {timeout}s 内结束: {r["status"]} {[l["status"] for l in r["legs"]]}')


def test_full_mission_completes_with_inspections(app_client, mock_robot):
    init_robot(app_client, '1')
    tid = make_task(app_client)
    r = app_client.post(f'/api/tasks/{tid}/run')
    assert r.status_code == 202, r.text
    run_id = r.json()['id']
    assert app_client.post(f'/api/tasks/{tid}/run').status_code == 409          # 同时只能一个执行
    run = wait_run(app_client, run_id)
    assert run['status'] == 'completed', run
    assert [l['status'] for l in run['legs']] == ['done', 'done', 'done']
    assert [l['to_node'] for l in run['legs']] == ['5', '20', '43']
    assert run['legs'][0]['from_node'] == '1' and run['legs'][0]['path'][0] == '1' and run['legs'][0]['path'][-1] == '5'
    assert run['legs'][1]['from_node'] == '5'                                   # 下一段从上一段终点出发
    for leg in run['legs']:
        assert re.fullmatch(rf'ps-[0-9a-f]{{6}}-r{run_id}-l\d+-a1', leg['idempotency_key'])
        assert leg['cloud_task'] is None or leg['cloud_task'].get('status') in ('navigating', 'completed', None)
    insp = run['inspections']
    assert len(insp) == 3
    assert [i['answer'] for i in insp] == ['yes', 'no', 'yes']                    # mock 交替
    assert [i['passed'] for i in insp] == [1, 0, 1]
    assert insp[0]['tts_text'] == '点5正常' and insp[1]['tts_text'] == '点20异常'
    for i in insp:
        assert i['capture_pose'] and 'yaw' in i['capture_pose']            # 抓图时位姿已记录
        assert app_client.get(i['image_url']).status_code == 200
        assert app_client.get(i['crop_url']).status_code == 200
        assert app_client.get(i['annot_url']).status_code == 200
    types = [e['type'] for e in run['events']]
    assert 'preflight' in types and types.count('leg_dispatched') == 3 and types.count('leg_arrived') == 3
    assert types.count('vlm_answer') == 3 and types.count('tts') == 3 and types[-1] == 'run_finished'
    cloud = app_client.get('/api/events?source=cloud&limit=200').json()['items']
    reached = [e for e in cloud if e['type'] == 'waypoint_reached']
    assert reached and all(e['cloud_seq'] for e in reached)
    assert any(e['type'] == 'task_completed' for e in cloud)
    assert run['summary'] == {'legs': 3, 'legs_done': 3, 'inspections': 3, 'passed': 2, 'failed': 1, 'unknown': 0}
    st = app_client.get('/api/stats?days=1').json()
    assert st['totals']['inspections'] == 3 and st['totals']['failed'] == 1 and any(w['pass_rate'] == 0 for w in st['per_waypoint'])
    # 人工改判：把 VLM 判「不通过」的那条改成通过 → 统计跟着变，原结论保留
    bad = next(i for i in insp if i['passed'] == 0)
    r = app_client.put(f"/api/inspections/{bad['id']}/verdict", json={'passed': True, 'note': '门其实是关着的'})
    assert r.status_code == 200 and r.json()['effective_passed'] == 1 and r.json()['passed'] == 0 and r.json()['human_note'] == '门其实是关着的'
    st = app_client.get('/api/stats?days=1').json()
    assert st['totals']['failed'] == 0 and sum(w['overturned'] for w in st['per_waypoint']) == 1
    assert app_client.put(f"/api/inspections/{bad['id']}/verdict", json={'passed': None}).json()['effective_passed'] == 0
    # 机器人最终停在最后一个任务航点
    st = mock_robot.snapshot_state()
    assert abs(st['x'] - 32.0) < 0.2 and abs(st['y'] - 7.0) < 0.2   # 航点 43 = 东侧支路 (32, 7)


def test_preflight_blocks_when_not_localized(app_client, mock_robot):
    tid = make_task(app_client, nodes=('3',))
    r = app_client.post(f'/api/tasks/{tid}/run')
    assert r.status_code == 202
    run = wait_run(app_client, r.json()['id'], timeout=30)
    assert run['status'] == 'aborted' and '定位未就绪' in run['error']
    assert run['legs'] == []


def test_task_failed_event_marks_run_failed(app_client, mock_robot):
    init_robot(app_client, '1')
    tid = make_task(app_client, nodes=('5', '20'))
    mock_robot.inject_fault('obstacle')
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'])
    assert run['status'] == 'failed' and '0x234B' in run['error'] and 'OBSTACLE_FAILURE' in run['error']
    assert run['legs'][0]['status'] == 'failed' and run['legs'][1]['status'] == 'aborted'
    cloud_types = [e['type'] for e in run['events'] if e['source'] == 'cloud']
    assert 'task_failed' in cloud_types and 'waypoint_reached' in cloud_types      # 云端事件挂到了本次执行上
    assert all(e['leg_id'] == run['legs'][0]['id'] for e in run['events'] if e['source'] == 'cloud' and e['type'] == 'task_failed')
    assert run['inspections'] == []
    assert app_client.get('/api/robot/status').json()['task']['terminal'] is True


def test_retry_after_failure_uses_new_idempotency_key(app_client, mock_robot):
    init_robot(app_client, '1')
    tid = make_task(app_client, nodes=('5',), max_retries=1)
    mock_robot.inject_fault('planning')
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'])
    assert run['status'] == 'completed', (run['error'], [(l['status'], l['attempt'], l['error']) for l in run['legs']], [e['message'] for e in run['events']])
    leg = run['legs'][0]
    assert leg['attempt'] == 2 and leg['idempotency_key'].endswith('-a2') and leg['status'] == 'done'
    assert leg['idempotency_key'].split('-')[1] == app_client.ctx.instance_id
    types = [e['type'] for e in run['events']]
    assert 'leg_retry' in types


def test_abort_stops_cloud_task(app_client, mock_robot):
    init_robot(app_client, '1')
    mock_robot.speed = 0.5                                                      # 慢一点，来得及中止
    try:
        tid = make_task(app_client, nodes=('20',))
        run_id = app_client.post(f'/api/tasks/{tid}/run').json()['id']
        for _ in range(40):
            r = app_client.get(f'/api/runs/{run_id}').json()
            if r['legs'] and r['legs'][0]['status'] in ('dispatched', 'navigating'):
                break
            time.sleep(0.25)
        assert app_client.post(f'/api/runs/{run_id}/abort').status_code == 200
        run = wait_run(app_client, run_id, timeout=20)
        assert run['status'] == 'aborted'
        assert mock_robot.snapshot_state()['task']['status'] in ('idle', 'stopped')
        assert app_client.post(f'/api/runs/{run_id}/abort').status_code == 400   # 已结束
    finally:
        mock_robot.speed = 10.0


def test_not_started_detected_when_robot_ignores_task(app_client, mock_robot):
    """定位就绪但设备栈没跑（真机常见）：任务 200 却不动 → 执行器应识别为「任务未执行」而不是干等超时。"""
    init_robot(app_client, '1')
    mock_robot.device_started = False                     # 定位标志还在，但导航栈已停
    tid = make_task(app_client, nodes=('5',))
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'], timeout=40)
    assert run['status'] == 'failed' and '没有动' in run['error']


def test_manual_coordinate_waypoint_and_return_to_start_and_full_pano(app_client, mock_robot, monkeypatch):
    """手工坐标的任务航点（无导航航点 → 取最近的）、返回起点、附整张全景给 VLM。"""
    init_robot(app_client, '1')
    app_client.put('/api/settings', json={'VLM_SEND_FULL_PANO': '1'})
    sync_map(app_client)
    r = app_client.post('/api/task-waypoints', json={'name': '手工点', 'map_name': DEMO_MAP, 'x': 6.7, 'y': 0.3, 'prompt': '有人吗', 'angle_from': 330, 'angle_to': 30})
    assert r.status_code == 201 and r.json()['nav_node_id'] is None
    r = app_client.post('/api/tasks', json={'name': 'manual', 'map_name': DEMO_MAP, 'waypoint_ids': [r.json()['id']],
                                            'options': {'settle_seconds': 0, 'return_to_start': True, 'not_started_timeout': 6}})
    tid = r.json()['id']
    plan = app_client.get(f'/api/tasks/{tid}/plan').json()
    assert plan['legs'][0]['to_node'] == '4' and plan['legs'][-1]['name'] == '返回起点' and plan['legs'][-1]['to_node'] == '1'
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'])
    assert run['status'] == 'completed', run['error']
    assert [l['to_node'] for l in run['legs']] == ['4', '1'] and [l['status'] for l in run['legs']] == ['done', 'done']
    assert len(run['inspections']) == 1
    insp = run['inspections'][0]
    assert insp['angle_from'] == 330 and insp['angle_to'] == 30       # 跨缝范围原样记录
    st = mock_robot.snapshot_state()
    assert abs(st['x']) < 0.2 and abs(st['y']) < 0.2                    # 回到起点
    assert any(e['type'] == 'plan_note' for e in run['events'])         # 记录了「取最近导航航点」


def test_rerun_from_failed_item(app_client, mock_robot):
    """第 1 段失败后，用 from_seq 从第 2 个航点重跑：新执行只走剩余航点。"""
    init_robot(app_client, '1')
    tid = make_task(app_client, nodes=('5', '20'))
    mock_robot.inject_fault('obstacle')
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'])
    assert run['status'] == 'failed' and run['legs'][0]['item_seq'] == 1
    r = app_client.post(f'/api/tasks/{tid}/run?from_seq=2')
    assert r.status_code == 202, r.text
    run2 = wait_run(app_client, r.json()['id'])
    assert run2['status'] == 'completed' and [l['to_node'] for l in run2['legs']] == ['20']
    assert '重跑' in run2['task_name'] and len(run2['inspections']) == 1
    assert app_client.post(f'/api/tasks/{tid}/run?from_seq=9').status_code == 400
