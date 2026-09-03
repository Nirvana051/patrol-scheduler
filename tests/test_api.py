"""调度系统自身 API：地图同步、任务航点/任务 CRUD、规划、设置、事件、TTS、抓图。"""
from tests.conftest import DEMO_MAP, sync_map


def test_health_and_status(app_client):
    r = app_client.get('/api/health')
    assert r.json()['mode'] == 'mock'
    s = app_client.post('/api/robot/status/refresh').json()
    assert s['reachable'] is True and s['online'] is True and s['localized'] is False


def test_map_sync_and_waypoints(app_client):
    maps = app_client.get('/api/maps').json()['maps']
    assert any(m['name'] == DEMO_MAP and m['on_cloud'] for m in maps)
    assert sync_map(app_client) == 45
    d = app_client.get(f'/api/maps/{DEMO_MAP}/waypoints').json()
    assert len(d['waypoints']) == 45 and d['components'] == 1 and d['waypoints'][1]['node_id'] == '2'   # 按编号排序
    r = app_client.get(f'/api/maps/{DEMO_MAP}/route?from_node=1&to_node=3').json()
    assert r['path'] == ['1', '2', '3'] and r['reachable']
    assert app_client.get('/api/maps/nope/waypoints').status_code == 404


def test_task_waypoint_crud(app_client):
    sync_map(app_client)
    r = app_client.post('/api/task-waypoints', json={'name': 'A', 'map_name': DEMO_MAP, 'nav_node_id': '20', 'prompt': '门关了吗', 'angle_from': 190, 'angle_to': 230})
    assert r.status_code == 201, r.text
    tw = r.json()
    assert tw['x'] == 30.0 and abs(tw['y'] - 14.0) < 1e-6 and tw['answer_template']['expected'] == 'yes'   # 位姿自动带出
    r = app_client.post('/api/task-waypoints', json={'name': 'B', 'map_name': DEMO_MAP, 'x': 1.0, 'y': 2.0, 'angle_from': -30, 'angle_to': 30})
    assert r.status_code == 201 and r.json()['angle_from'] == 330                                        # 归一化
    assert app_client.post('/api/task-waypoints', json={'name': 'C', 'map_name': DEMO_MAP}).status_code == 400   # 缺坐标
    r = app_client.put(f"/api/task-waypoints/{tw['id']}", json={**tw, 'prompt': '改了', 'answer_template': {'expected': 'no'}})
    assert r.json()['prompt'] == '改了' and r.json()['answer_template']['expected'] == 'no'
    assert len(app_client.get(f'/api/task-waypoints?map_name={DEMO_MAP}').json()['items']) == 2
    # 参考图（合成源）+ 试问 VLM（mock）
    r = app_client.post(f"/api/task-waypoints/{tw['id']}/reference-image")
    assert r.status_code == 200 and r.json()['width'] == 1280
    r = app_client.post(f"/api/task-waypoints/{tw['id']}/test-vlm", json={'use': 'reference'})
    assert r.status_code == 200 and r.json()['vlm']['answer'] in ('yes', 'no') and r.json()['crop_url'].startswith('/media/')
    assert app_client.get(r.json()['crop_url']).status_code == 200
    r = app_client.delete(f"/api/task-waypoints/{tw['id']}")
    assert r.status_code == 200 and app_client.get(f"/api/task-waypoints/{tw['id']}").status_code == 404


def test_task_crud_and_plan(app_client):
    sync_map(app_client)
    ids = [app_client.post('/api/task-waypoints', json={'name': n, 'map_name': DEMO_MAP, 'nav_node_id': nid}).json()['id'] for n, nid in (('a', '5'), ('b', '20'))]
    r = app_client.post('/api/tasks', json={'name': 'T', 'map_name': DEMO_MAP, 'waypoint_ids': ids, 'options': {'return_to_start': True}})
    assert r.status_code == 201 and [i['task_waypoint_id'] for i in r.json()['items']] == ids
    tid = r.json()['id']
    p = app_client.get(f'/api/tasks/{tid}/plan?from_node=1').json()
    assert [l['to_node'] for l in p['legs']] == ['5', '20', '1'] and p['legs'][0]['path'][0] == '1' and p['unreachable'] == []
    r = app_client.put(f'/api/tasks/{tid}', json={'name': 'T2', 'map_name': DEMO_MAP, 'waypoint_ids': ids[::-1], 'options': {}})
    assert [i['task_waypoint_id'] for i in r.json()['items']] == ids[::-1]
    assert app_client.get('/api/tasks').json()['items'][0]['item_count'] == 2
    assert app_client.post('/api/tasks', json={'name': 'bad', 'map_name': DEMO_MAP, 'waypoint_ids': [9999]}).status_code == 400
    assert app_client.delete(f'/api/tasks/{tid}').status_code == 200


def test_settings_events_tts_snapshot(app_client):
    s = app_client.get('/api/settings').json()
    assert s['settings']['CX_KEY'].startswith('cx_mock0') and '…' in s['settings']['CX_KEY'] and s['mode'] == 'mock'
    r = app_client.put('/api/settings', json={'FORWARD_DEG': '90', 'CX_KEY': s['settings']['CX_KEY']})
    assert r.json()['changed'] == ['FORWARD_DEG'] and r.json()['reconnected'] is False
    assert app_client.get('/api/settings').json()['settings']['FORWARD_DEG'] == '90'
    assert app_client.put('/api/settings', json={'PS_PORT': '1'}).status_code == 400
    ev = app_client.get('/api/events?source=system').json()
    assert any(e['type'] == 'settings_changed' for e in ev['items']) and 'app_started' in ev['types']
    r = app_client.post('/api/tts/test', json={'text': '测试播报'})
    assert r.status_code == 200 and r.json()['engine'] == 'none' and 'browser' in r.json()['sinks']
    r = app_client.post('/api/robot/snapshot')
    assert r.status_code == 200 and r.json()['width'] == 1280 and app_client.get(r.json()['url']).status_code == 200
    assert app_client.get('/api/robot/status-codes').json()['status']['values'][0]['name'] == 'IDLE'
    assert app_client.get('/api/robot/video').json()['rtsp_path'] == 'cam-1c697ada870c'


def test_preflight_reports_missing_localization(app_client):
    pf = app_client.get('/api/robot/preflight').json()
    assert pf['ok'] is False
    bad = {c['key'] for c in pf['checks'] if not c['ok']}
    assert bad == {'localized'}


def test_settings_cx_change_reconnects(app_client):
    old_status = app_client.ctx.status
    r = app_client.put('/api/settings', json={'RATE_LIMIT_RPS': '10'})
    assert r.json()['reconnected'] is True and 'RATE_LIMIT_RPS' in r.json()['changed']
    assert app_client.ctx.status is not old_status and app_client.ctx.status.is_alive() and app_client.ctx.events.is_alive()
    s = app_client.post('/api/robot/status/refresh').json()
    assert s['online'] is True
    assert app_client.ctx.gateway.limiter.rate == 10.0


def test_instance_id_persisted_and_unique(app_client, tmp_path):
    from app.config import Config
    from app.context import AppContext
    from app.db import Database
    iid = app_client.ctx.instance_id
    assert len(iid) == 6 and app_client.ctx.db.get_setting('PS_INSTANCE_ID') == iid
    other = AppContext(Config(env_file=tmp_path / 'none.env'), Database(tmp_path / 'other.db'))
    try:
        assert other.instance_id != iid           # 另一套 DB 另一个实例段：幂等键不会和这套撞
    finally:
        other.stop()


def test_schema_migration_from_v1(tmp_path):
    """旧库（v1，run_legs 没有 item_seq）打开时应被增量迁移到当前版本。"""
    import sqlite3
    from app.db import SCHEMA_VERSION, Database
    p = tmp_path / 'old.db'
    c = sqlite3.connect(p)
    c.executescript('CREATE TABLE schema_version(version INTEGER NOT NULL); INSERT INTO schema_version VALUES (1);'
                    'CREATE TABLE run_legs(id INTEGER PRIMARY KEY, run_id INTEGER, seq INTEGER, status TEXT);'
                    'CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);')
    c.commit(); c.close()
    db = Database(p)
    assert db.version() == SCHEMA_VERSION
    cols = {r['name'] for r in db.query('PRAGMA table_info(run_legs)')}
    assert 'item_seq' in cols
    db2 = Database(tmp_path / 'fresh.db')
    assert db2.version() == SCHEMA_VERSION and 'item_seq' in {r['name'] for r in db2.query('PRAGMA table_info(run_legs)')}
