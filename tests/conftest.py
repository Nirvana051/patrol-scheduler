# -*- coding: utf-8 -*-
"""测试夹具：进程内起一个 mock 云端网关（随机端口，快速仿真参数）+ 用临时目录起调度系统 TestClient。"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mock_gateway.robot_sim import MockRobot, load_maps  # noqa: E402
from mock_gateway.server import DEFAULT_KEY, create_app as create_mock, serve_in_thread  # noqa: E402

DEMO_MAP = 'map_demo_20260903_220000'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class MockGateway:
    def __init__(self, robot: MockRobot, port: int, server) -> None:
        self.robot, self.port, self.server = robot, port, server
        self.url = f'http://127.0.0.1:{port}'
        self.key = DEFAULT_KEY
        self.alias = robot.alias
        self.robot_id = robot.robot_id


@pytest.fixture(scope='session')
def mock_gw():
    robot = MockRobot(load_maps(), speed=10.0, device_delay=0.3, localize_delay=0.1)
    port = free_port()
    server, th = serve_in_thread(create_mock(robot, rps=50.0), port=port)   # 限流放宽：测试里并发请求多
    gw = MockGateway(robot, port, server)
    yield gw
    server.should_exit = True
    robot.close()


@pytest.fixture()
def mock_robot(mock_gw):
    """每个用例前把仿真机器人复位到未初始化、站在航点 1。"""
    mock_gw.robot.reset()
    mock_gw.robot.teleport(node_id='1', map_name=DEMO_MAP)
    return mock_gw.robot


@pytest.fixture()
def app_client(mock_gw, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Config
    from app.db import Database
    from app.main import create_app

    env = {'CX_HOST': mock_gw.url, 'CX_ROBOT': mock_gw.alias, 'CX_KEY': mock_gw.key,
           'PS_DATA_DIR': str(tmp_path / 'data'), 'PS_DB_PATH': str(tmp_path / 'data' / 'test.db'),
           'TTS_ENGINE': 'none', 'TTS_SINKS': 'browser', 'VLM_PROVIDER': 'mock', 'VLM_MOCK_ANSWER': 'alternate',
           'SNAPSHOT_SOURCE': 'synthetic', 'STATUS_POLL_ACTIVE': '1', 'STATUS_POLL_IDLE': '1', 'RATE_LIMIT_RPS': '20'}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = Config(env_file=tmp_path / 'no.env')
    db = Database(cfg.db_path)
    app = create_app(cfg, db)
    with TestClient(app) as c:
        c.ctx = app.state.ctx
        yield c
    app.state.ctx.stop()


def init_robot(client, node: str = '1') -> None:
    """走真机必需的 ②③④：启动设备（等完成）+ 定位。"""
    r = client.post('/api/robot/init/device-start', json={'wait': True})
    assert r.status_code == 200, r.text
    r = client.post('/api/robot/init/localize', json={'map_name': DEMO_MAP, 'node_id': node})
    assert r.status_code == 200, r.text


def sync_map(client, name: str = DEMO_MAP) -> int:
    r = client.post(f'/api/maps/{name}/sync')
    assert r.status_code == 200, r.text
    return r.json()['count']
