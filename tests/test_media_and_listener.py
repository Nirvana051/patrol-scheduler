"""ffmpeg 抓帧管道（lavfi 合成源）与云端事件监听的轮询回退路径。"""
import io
import shutil
import time

import pytest
from PIL import Image

from app.media.snapshot import LavfiSource, SnapshotError, build_source


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='需要 ffmpeg')
def test_lavfi_source_grabs_jpeg_via_pipe():
    data = LavfiSource('testsrc=size=640x320:rate=1').grab()
    img = Image.open(io.BytesIO(data))
    assert img.size == (640, 320) and img.format == 'JPEG'


def test_build_source_variants(tmp_path):
    assert build_source('synthetic').name == 'synthetic'
    assert build_source('lavfi:testsrc').name == 'lavfi'
    assert build_source('rtsp://h:8554/x').describe().startswith('rtsp')
    p = tmp_path / 'a.jpg'
    Image.new('RGB', (64, 32), (1, 2, 3)).save(p)
    src = build_source(f'file:{p}')
    assert Image.open(io.BytesIO(src.grab())).size == (64, 32)
    with pytest.raises(SnapshotError):
        build_source('rtsp')                      # rtsp 需要地址提供者


def test_file_source_missing(tmp_path):
    with pytest.raises(SnapshotError):
        build_source(f'file:{tmp_path / "nope.jpg"}').grab()


def test_listener_poll_fallback_persists_events(mock_gw, mock_robot, tmp_path):
    from app.bus import Bus
    from app.config import Config
    from app.db import Database
    from app.robot.client import RobotGateway
    from app.robot.events import CloudEventListener
    db = Database(tmp_path / 'ev.db')
    bus = Bus()
    q = bus.subscribe()
    g = RobotGateway(mock_gw.url, mock_gw.alias, mock_gw.key, rps=20)
    lst = CloudEventListener(g, db, bus, Config(env_file=tmp_path / 'no.env'))
    lst.cursor = int(g.events().get('seq') or 0)
    sub = lst.subscribe()
    mock_robot.set_estop(True)
    mock_robot.set_estop(False)
    lst._poll_for(2.0)                                  # 直接走轮询分支（SSE 三次失败后的回退）
    rows = db.query("SELECT type, cloud_seq FROM events WHERE source='cloud' ORDER BY cloud_seq")
    assert [r['type'] for r in rows] == ['emergency', 'emergency']
    assert lst.cursor == rows[-1]['cloud_seq'] and lst.received == 2
    assert sub.qsize() == 2 and q.qsize() == 2
    # 重放同一批不会重复落库
    lst.cursor = rows[0]['cloud_seq'] - 1
    lst._poll_for(1.0)
    assert db.query_one("SELECT COUNT(*) AS n FROM events")['n'] == 2


def test_listener_sse_receives_and_dedups(mock_gw, mock_robot, tmp_path):
    from app.bus import Bus
    from app.config import Config
    from app.db import Database
    from app.robot.client import RobotGateway
    from app.robot.events import CloudEventListener
    db = Database(tmp_path / 'ev2.db')
    g = RobotGateway(mock_gw.url, mock_gw.alias, mock_gw.key, rps=20)
    lst = CloudEventListener(g, db, Bus(), Config(env_file=tmp_path / 'no.env'))
    lst.start()
    t0 = time.time()
    while not lst.connected and time.time() - t0 < 10:
        time.sleep(0.05)
    assert lst.connected and lst.transport == 'sse'
    mock_robot.obstacle(0.2)
    t0 = time.time()
    while lst.received < 1 and time.time() - t0 < 5:
        time.sleep(0.05)
    assert lst.received >= 1
    lst.stop()
    assert db.query_one("SELECT type FROM events WHERE source='cloud' ORDER BY id LIMIT 1")['type'] == 'obstacle'
