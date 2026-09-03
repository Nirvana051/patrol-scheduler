"""导出 / 导入 / 重建图后重定向。"""
from tests.conftest import DEMO_MAP, sync_map

SMALL = 'map_small_20260901_000000'


def test_export_import_roundtrip(app_client):
    sync_map(app_client)
    a = app_client.post('/api/task-waypoints', json={'name': 'A', 'map_name': DEMO_MAP, 'nav_node_id': '2', 'prompt': 'pa', 'answer_template': {'expected': 'no'}}).json()['id']
    b = app_client.post('/api/task-waypoints', json={'name': 'B', 'map_name': DEMO_MAP, 'nav_node_id': '5'}).json()['id']
    tid = app_client.post('/api/tasks', json={'name': 'T', 'map_name': DEMO_MAP, 'waypoint_ids': [b, a], 'options': {'settle_seconds': 1}}).json()['id']
    app_client.post('/api/schedules', json={'task_id': tid, 'kind': 'daily', 'spec': '06:00'})
    exp = app_client.get('/api/export').json()
    assert exp['version'] == 1 and [t['name'] for t in exp['task_waypoints']] == ['A', 'B']
    assert exp['tasks'][0]['waypoint_names'] == ['B', 'A'] and exp['schedules'][0]['spec'] == '06:00'
    # 清空后导入
    for i in (a, b):
        app_client.delete(f'/api/task-waypoints/{i}')
    app_client.delete(f'/api/tasks/{tid}')
    r = app_client.post('/api/import', json={'data': exp})
    assert r.status_code == 200 and r.json() == {'task_waypoints': 2, 'tasks': 1, 'schedules': 1, 'skipped': 0}
    t = app_client.get('/api/tasks').json()['items'][0]
    full = app_client.get(f"/api/tasks/{t['id']}").json()
    assert [i['name'] for i in full['items']] == ['B', 'A'] and full['options']['settle_seconds'] == 1
    assert next(w for w in app_client.get('/api/task-waypoints').json()['items'] if w['name'] == 'A')['answer_template']['expected'] == 'no'
    # 再导入一次：同名覆盖不重复；不覆盖则跳过
    assert app_client.post('/api/import', json={'data': exp}).json()['task_waypoints'] == 2
    assert len(app_client.get('/api/task-waypoints').json()['items']) == 2
    assert app_client.post('/api/import', json={'data': exp, 'overwrite': False}).json()['skipped'] >= 3
    assert app_client.post('/api/import', json={'data': {'version': 9}}).status_code == 400


def test_retarget_to_new_map(app_client):
    sync_map(app_client)
    sync_map(app_client, SMALL)
    # 小地图是 x=0..8 的一条线；旧图上 (2.3, 0) 附近的航点应落到小地图的航点 2 (x=2)
    a = app_client.post('/api/task-waypoints', json={'name': 'near', 'map_name': DEMO_MAP, 'nav_node_id': '2'}).json()   # (2.31, 0)
    far = app_client.post('/api/task-waypoints', json={'name': 'far', 'map_name': DEMO_MAP, 'nav_node_id': '20'}).json()  # (30, 14)
    tid = app_client.post('/api/tasks', json={'name': 'T', 'map_name': DEMO_MAP, 'waypoint_ids': [a['id'], far['id']]}).json()['id']
    r = app_client.post('/api/task-waypoints/retarget', json={'from_map': DEMO_MAP, 'to_map': SMALL, 'max_distance': 3.0})
    assert r.status_code == 200 and r.json()['moved'] == 1
    rep = {x['name']: x for x in r.json()['report']}
    assert rep['near']['new_node'] == '2' and rep['near']['moved'] and rep['far']['moved'] is False and rep['far']['distance'] > 3
    got = app_client.get(f"/api/task-waypoints/{a['id']}").json()
    assert got['map_name'] == SMALL and got['nav_node_id'] == '2' and abs(got['x'] - 2.0) < 1e-6
    assert app_client.get(f'/api/tasks/{tid}').json()['map_name'] == SMALL
    assert app_client.post('/api/task-waypoints/retarget', json={'from_map': DEMO_MAP, 'to_map': 'nope'}).status_code == 404
