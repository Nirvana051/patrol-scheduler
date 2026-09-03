"""失败通知 webhook：检查不通过 / 执行失败时 POST JSON。"""
import time

import pytest
from fastapi import FastAPI, Request

from tests.conftest import init_robot
from tests.test_e2e_run import make_task, wait_run


@pytest.fixture(scope='module')
def hook():
    from mock_gateway.server import serve_in_thread
    from tests.conftest import free_port
    app = FastAPI()
    got = []

    @app.post('/hook')
    async def h(request: Request):
        got.append(await request.json())
        return {'ok': True}
    port = free_port()
    server, _ = serve_in_thread(app, port=port)
    yield f'http://127.0.0.1:{port}/hook', got
    server.should_exit = True


def test_webhook_on_failed_inspection_and_failed_run(app_client, mock_robot, hook):
    url, got = hook
    app_client.put('/api/settings', json={'NOTIFY_WEBHOOK_URL': url, 'VLM_MOCK_ANSWER': 'no'})
    init_robot(app_client, '1')
    tid = make_task(app_client, nodes=('3',))
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'])
    assert run['status'] == 'completed' and run['inspections'][0]['passed'] == 0
    t0 = time.time()
    while len(got) < 1 and time.time() - t0 < 5:
        time.sleep(0.1)
    assert got and got[0]['kind'] == 'inspection_failed' and got[0]['waypoint'] == '点3' and got[0]['run_id'] == run['id']
    mock_robot.inject_fault('obstacle')
    tid2 = make_task(app_client, nodes=('5',))
    run2 = wait_run(app_client, app_client.post(f'/api/tasks/{tid2}/run').json()['id'])
    assert run2['status'] == 'failed'
    t0 = time.time()
    while not any(g['kind'] == 'run_failed' for g in got) and time.time() - t0 < 5:
        time.sleep(0.1)
    assert any(g['kind'] == 'run_failed' and g['run_id'] == run2['id'] for g in got)
    assert any(e['type'] == 'notify_sent' for e in app_client.get('/api/events?type=notify_sent').json()['items'])
