# -*- coding: utf-8 -*-
"""机器人操作：初始化三步（②③④，透传通道）、急停、停任务、立即抓图、执行前置检查。API 与执行器共用。"""
from __future__ import annotations

import time
from pathlib import Path

from app.media import pano
from app.media.snapshot import SnapshotError, save_jpeg, timestamp_stem
from app.robot.client import RobotError


class OpsError(RuntimeError):
    pass


class RobotOps:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    # ── 初始化三步 ───────────────────────────────────────────────────────────
    def device_start(self, wait: bool = True, timeout: float = 180.0) -> dict:
        g = self.ctx.gateway
        tid = g.device_start()
        self.ctx.log_event('device_start', f'启动设备脚本已下发 task_id={tid}', data={'task_id': tid})
        out = {'task_id': tid, 'completed': False}
        if wait:
            st = g.wait_device(tid, starting=True, timeout=timeout)
            out.update({'completed': True, 'status': st})
            self.ctx.log_event('device_started', '设备启动完成（导航等模块已拉起）', data=st)
        self.ctx.status.refresh(full=True)
        return out

    def device_stop(self, wait: bool = True, timeout: float = 180.0) -> dict:
        g = self.ctx.gateway
        tid = g.device_stop()
        self.ctx.log_event('device_stop', f'停止设备脚本已下发 task_id={tid}', data={'task_id': tid})
        out = {'task_id': tid, 'completed': False}
        if wait:
            st = g.wait_device(tid, starting=False, timeout=timeout)
            out.update({'completed': True, 'status': st})
            self.ctx.log_event('device_stopped', '设备已停止', data=st)
        self.ctx.status.refresh(full=True)
        return out

    def device_status(self, task_id: str, starting: bool = True) -> dict:
        g = self.ctx.gateway
        return g.device_start_status(task_id) if starting else g.device_stop_status(task_id)

    def localize(self, map_name: str, node_id: str, pose: dict | None = None, timeout: float = 60.0) -> dict:
        g = self.ctx.gateway
        self.ctx.log_event('localize', f'定位：地图 {map_name}，初值航点 {node_id}', data={'map_name': map_name, 'node_id': node_id})
        try:
            r = g.localize(map_name, node_id, pose, timeout=timeout)
        except RobotError as e:
            body = e.body if isinstance(e.body, dict) else {}
            data = body.get('data') or {}
            self.ctx.log_event('localize_failed', f'定位失败：{e}', level='error', data=data)
            raise OpsError(str(e)) from e
        self.ctx.log_event('localized', f"定位成功，与初值偏差 {r.get('drift')} m（阈值 {r.get('threshold')} m）", data=r)
        self.ctx.status.refresh(full=True)
        return r

    # ── 安全 / 任务控制 ─────────────────────────────────────────────────────
    def estop(self, active: bool) -> dict:
        g = self.ctx.gateway
        r = g.estop() if active else g.clear_estop()
        self.ctx.log_event('estop' if active else 'estop_clear', '已下发急停（机器人端将持续下发停止指令）' if active else '已取消急停',
                           level='warn' if active else 'info', data=r)
        self.ctx.status.refresh(full=True)
        return r

    def stop_task(self) -> dict:
        r = self.ctx.gateway.stop_task()
        self.ctx.log_event('task_stop_requested', '已请求停止云端任务', level='warn', data=r)
        return r

    # ── 抓图 ────────────────────────────────────────────────────────────────
    def snapshot(self, subdir: str = 'snapshots', prefix: str = 'snap') -> dict:
        try:
            data = self.ctx.snapshot.grab({'reason': 'manual'})
        except SnapshotError as e:
            raise OpsError(str(e)) from e
        p = save_jpeg(data, self.ctx.media_dir / subdir, timestamp_stem(prefix))
        img = pano.Image.open(p)
        rel = p.relative_to(self.ctx.media_dir)
        return {'path': str(rel), 'url': f'/media/{rel}', 'width': img.width, 'height': img.height,
                'source': self.ctx.snapshot.describe(), 'ts': time.time()}

    # ── 执行前置检查（C8/C9/C11）────────────────────────────────────────────
    def preflight(self, *, require_localized: bool = True) -> dict:
        s = self.ctx.status.refresh(full=True)
        checks = []

        def add(key, ok, text, fix=None):
            checks.append({'key': key, 'ok': bool(ok), 'text': text, 'fix': fix})

        add('reachable', s.get('reachable'), '云端网关可达' if s.get('reachable') else f"云端网关不可达：{'; '.join(s.get('errors') or [])}")
        add('online', s.get('online'), '机器人在线' if s.get('online') else '机器人不在线（确认开机并联网）')
        if s.get('online'):
            add('emergency', not s.get('emergency_active'),
                '无急停指令' if not s.get('emergency_active') else '急停指令正在下发，巡检指令会与之竞争', fix='clear_estop')
            add('ros', s.get('ros_available', True), 'ROS 可用' if s.get('ros_available', True) else 'ROS 不可用：读到的都是陈旧值')
            loc = s.get('localization') or {}
            loc_ok = bool(s.get('position_ready')) or bool(loc.get('fresh'))
            add('localized', (not require_localized) or loc_ok,
                f"定位就绪（数据年龄 {loc.get('age')} s）" if loc_ok else '定位未就绪：先做「启动设备 → 定位」', fix='init')
            t = s.get('task') or {}
            add('idle', not t.get('active'), '云端无进行中的任务' if not t.get('active') else f"云端有任务在跑：{t.get('status_name')}", fix='stop_task')
        add('lease', not self._lease_held_by_human(s), '控制权可用' if not self._lease_held_by_human(s)
            else f"控制权被 {((s.get('lease') or {}).get('owner'))} 持有（现场有人在操作）")
        return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'status': s}

    @staticmethod
    def _lease_held_by_human(s: dict) -> bool:
        lease = s.get('lease') or {}
        owner = str(lease.get('owner') or '')
        if not owner:
            return False
        expires = lease.get('expiresAt') or 0
        return (not owner.startswith('api:')) and expires > time.time() * 1000
