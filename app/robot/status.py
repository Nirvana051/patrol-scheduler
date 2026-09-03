# -*- coding: utf-8 -*-
"""状态轮询线程：把 telemetry / position / task /（间或）overview、perception 聚合成一份快照。

节奏：有人看页面或有执行在跑时 STATUS_POLL_ACTIVE（默认 2s），否则 STATUS_POLL_IDLE。
定位就绪只看 global_localization.received_at 的新鲜度和 /position 是否 200（C9），
Location 字段只展示。
"""
from __future__ import annotations

import threading
import time

from app.robot.client import RobotError


class StatusPoller(threading.Thread):
    def __init__(self, gateway, db, bus, cfg) -> None:
        super().__init__(name='status-poller', daemon=True)
        self.gateway, self.db, self.bus, self.cfg = gateway, db, bus, cfg
        self.lock = threading.Lock()
        self.snapshot: dict = {'ts': 0, 'reachable': None, 'online': None, 'mode': gateway.mode}
        self._active_users = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cycle = 0
        self._prev_online = None
        self._prev_emergency = None

    # ── 节奏控制 ─────────────────────────────────────────────────────────────
    def hold_active(self) -> None:
        with self.lock:
            self._active_users += 1
        self._wake.set()

    def release_active(self) -> None:
        with self.lock:
            self._active_users = max(0, self._active_users - 1)

    def _interval(self) -> float:
        active = self._active_users > 0 or self.bus.subscriber_count > 0
        key = 'STATUS_POLL_ACTIVE' if active else 'STATUS_POLL_IDLE'
        return max(1.0, self.cfg.get_float(key))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ── 主循环 ───────────────────────────────────────────────────────────────
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as e:      # noqa: BLE001 —— 轮询线程不能死
                with self.lock:
                    self.snapshot['errors'] = [f'poll: {e}']
            self._wake.wait(self._interval())
            self._wake.clear()

    def refresh(self, full: bool = False) -> dict:
        g = self.gateway
        snap: dict = {'ts': time.time(), 'mode': g.mode, 'host': g.host, 'robot': g.robot, 'errors': []}
        self._cycle += 1
        prev = self.snapshot
        try:
            if full or self._cycle % 3 == 1 or not prev.get('info'):
                info = g.info()
                snap['info'] = info
            else:
                snap['info'] = prev.get('info')
            snap['reachable'] = True
            snap['online'] = bool((snap['info'] or {}).get('online'))
            snap['lease'] = (snap['info'] or {}).get('lease')
        except RobotError as e:
            snap['reachable'] = e.status != 0
            snap['online'] = None
            snap['errors'].append(f'info: {e}')
            snap['info'] = prev.get('info')
            self._publish(snap)
            return snap

        if snap['online']:
            try:
                tele = g.telemetry()
                gl = (tele.get('telemetry') or {}).get('global_localization') or {}
                age = time.time() - float(gl.get('received_at') or 0) if gl.get('received') else None
                snap['emergency_active'] = bool(tele.get('emergency_active'))
                snap['ros_available'] = bool(tele.get('ros_available', True))
                snap['robot_status_text'] = ((tele.get('telemetry') or {}).get('robot_info') or {}).get('status')
                snap['localization'] = {'received': bool(gl.get('received')), 'age': None if age is None else round(age, 1),
                                        'fresh': bool(gl.get('received')) and age is not None and age < 5.0,
                                        'x': gl.get('x'), 'y': gl.get('y'), 'yaw': gl.get('yaw')}
                odom = (tele.get('telemetry') or {}).get('odom') or {}
                snap['speed'] = odom.get('linear')
            except RobotError as e:
                snap['errors'].append(f'telemetry: {e}')
            try:
                pos = g.position()
                snap['position'] = {k: pos.get(k) for k in ('x', 'y', 'z', 'yaw')}
                snap['position_ready'] = True
            except RobotError as e:
                snap['position'] = None
                snap['position_ready'] = False
                if e.status != 503:
                    snap['errors'].append(f'position: {e}')
            try:
                snap['task'] = g.task()
            except RobotError as e:
                snap['task'] = prev.get('task')
                snap['errors'].append(f'task: {e}')
            if full or self._cycle % 3 == 0 or not prev.get('perception'):
                try:
                    snap['perception'] = g.perception()
                except RobotError as e:
                    snap['perception'] = prev.get('perception')
                    snap['errors'].append(f'perception: {e}')
            else:
                snap['perception'] = prev.get('perception')
        snap['localized'] = bool(snap.get('position_ready')) or bool((snap.get('localization') or {}).get('fresh'))
        snap['rate_limiter_total'] = g.limiter.total
        self._publish(snap)
        return snap

    def _publish(self, snap: dict) -> None:
        with self.lock:
            self.snapshot = snap
        # 在线/急停变化落一条系统事件，方便事后查
        if self._prev_online is not None and snap.get('online') is not None and snap['online'] != self._prev_online:
            self.db.add_event('system', 'robot_online' if snap['online'] else 'robot_offline',
                              message='机器人上线' if snap['online'] else '机器人掉线', level='info' if snap['online'] else 'warn')
        if snap.get('online') is not None:
            self._prev_online = snap['online']
        em = snap.get('emergency_active')
        if em is not None and self._prev_emergency is not None and em != self._prev_emergency:
            self.db.add_event('system', 'emergency_changed', message='急停指令开始下发' if em else '急停已取消',
                              level='warn' if em else 'info', data={'active': em})
        if em is not None:
            self._prev_emergency = em
        self.bus.publish('robot_status', snap)

    # ── 便捷读取 ─────────────────────────────────────────────────────────────
    def get(self) -> dict:
        with self.lock:
            return dict(self.snapshot)

    def pose(self) -> dict | None:
        s = self.get()
        return s.get('position') or ((s.get('localization') or {}) if (s.get('localization') or {}).get('received') else None)
