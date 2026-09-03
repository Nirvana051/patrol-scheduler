# -*- coding: utf-8 -*-
"""应用上下文：把配置、DB、总线、网关、后台线程与各适配器装配在一起，供 API 路由与执行器使用。"""
from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path

from app.bus import Bus
from app.config import Config
from app.db import Database, dumps, loads, now_iso
from app.executor.runner import RunManager
from app.media.pointcloud import PointCloudProvider
from app.media.snapshot import build_source
from app.planning.graph import NavGraph
from app.robot.client import RobotGateway
from app.robot.events import CloudEventListener
from app.robot.ops import RobotOps
from app.robot.status import StatusPoller
from app.tts.base import build_tts
from app.vlm.base import build_provider


log = logging.getLogger('scheduler')


class AppContext:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg, self.db = cfg, db
        cfg.db = db
        self.instance_id = db.get_setting('PS_INSTANCE_ID')
        if not self.instance_id:
            # 幂等键 ps-{instance}-r{run}-l{leg}-a{attempt} 里的实例段：同一 DB 内 run 自增保证不重复，
            # 换 DB / 多套部署靠这个随机段区分 —— 否则 10 分钟内同键会被云端当成重放而不执行（C3）
            self.instance_id = secrets.token_hex(3)
            db.set_setting('PS_INSTANCE_ID', self.instance_id)
        self.bus = Bus()
        self.media_dir: Path = cfg.media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._graphs: dict[str, NavGraph] = {}
        self._glock = threading.Lock()
        self.scene: dict = {}                 # 合成全景的场景状态（mock 演示用：door_open 等）
        self.gateway: RobotGateway | None = None
        self.status: StatusPoller | None = None
        self.events: CloudEventListener | None = None
        self._build_gateway()
        self.reload_adapters()
        self.ops = RobotOps(self)
        self.runs = RunManager(self)
        self.pointclouds = PointCloudProvider(self.media_dir / 'pointclouds')

    # ── 装配 ────────────────────────────────────────────────────────────────
    def _build_gateway(self) -> None:
        cfg = self.cfg
        self.gateway = RobotGateway(cfg.get('CX_HOST'), cfg.get('CX_ROBOT'), cfg.get('CX_KEY'),
                                    rps=cfg.get_float('RATE_LIMIT_RPS'))
        self.status = StatusPoller(self.gateway, self.db, self.bus, cfg)
        self.events = CloudEventListener(self.gateway, self.db, self.bus, cfg,
                                         run_ref=lambda: self.runs.current_ref() if getattr(self, 'runs', None) else None)

    def reload_adapters(self) -> None:
        cfg = self.cfg
        self.vlm = build_provider(cfg)
        self.tts = build_tts(cfg, self.bus, self.media_dir)
        self.snapshot = build_source(cfg.get('SNAPSHOT_SOURCE'),
                                     rtsp_url_provider=lambda: self.gateway.rtsp_url(),
                                     hls_url_provider=lambda: self.gateway.hls_url(),
                                     pose_provider=lambda: self.status.pose() if self.status else None,
                                     scene_provider=lambda: self.scene)

    def reconnect(self) -> None:
        """CX_* 改了之后重建网关与后台线程（不中止执行；有执行在跑时应先中止再改设置）。"""
        self.status.stop()
        self.events.stop()
        self._build_gateway()
        self.reload_adapters()
        self.ops = RobotOps(self)
        self.start()

    def start(self) -> None:
        if not self.status.is_alive():
            self.status.start()
        if not self.events.is_alive():
            self.events.start()

    def stop(self) -> None:
        if getattr(self, 'runs', None):
            self.runs.shutdown()
        if self.status:
            self.status.stop()
        if self.events:
            self.events.stop()

    # ── 导航图缓存 ───────────────────────────────────────────────────────────
    def graph(self, map_name: str) -> NavGraph | None:
        with self._glock:
            g = self._graphs.get(map_name)
            if g is not None:
                return g
        rows = self.db.query('SELECT * FROM nav_waypoints WHERE map_name=?', (map_name,))
        if not rows:
            return None
        g = NavGraph.from_rows(rows)
        with self._glock:
            self._graphs[map_name] = g
        return g

    def invalidate_graph(self, map_name: str | None = None) -> None:
        with self._glock:
            if map_name is None:
                self._graphs.clear()
            else:
                self._graphs.pop(map_name, None)

    # ── 系统事件 ─────────────────────────────────────────────────────────────
    def log_event(self, etype: str, message: str, *, level: str = 'info', run_id: int | None = None,
                  leg_id: int | None = None, data=None) -> dict:
        ts = now_iso()
        row_id = self.db.add_event('system', etype, message=message, level=level, run_id=run_id, leg_id=leg_id, data=data, ts=ts)
        row = {'id': row_id, 'ts': ts, 'source': 'system', 'type': etype, 'level': level, 'message': message,
               'run_id': run_id, 'leg_id': leg_id, 'data': data or {}}
        self.bus.publish('event', row)
        log.log({'warn': logging.WARNING, 'error': logging.ERROR}.get(level, logging.INFO), '[%s] %s', etype, message)
        return row


__all__ = ['AppContext', 'dumps', 'loads']
