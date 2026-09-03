# -*- coding: utf-8 -*-
"""云端事件监听线程：SSE（stream=1）为主，失败退回轮询；始终用 since 游标续接（C10）。

每条云端事件：落库（events 表，按 cloud_seq 去重）→ 广播给浏览器（bus）→ 分发给执行器等本地订阅者。
云端只留 500 条内存缓冲、网关重启即清空，所以这里的落库才是持久记录。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app.db import dumps
from app.robot.client import RobotError

LABEL = {
    'waypoint_reached': '到达航点', 'task_started': '任务开始', 'task_completed': '任务完成',
    'task_failed': '任务失败', 'task_stopped': '任务被停止', 'obstacle': '避障状态变化',
    'localization': '定位状态变化', 'emergency': '急停状态变化', 'online': '机器人上线', 'offline': '机器人掉线',
}


def describe(ev: dict) -> str:
    d = ev.get('data') or {}
    t = ev.get('type')
    if t == 'waypoint_reached':
        nxt = d.get('nextTarget')
        return (f"到达航点 {d.get('waypoint')}（第 {int(d.get('index', 0)) + 1}/{d.get('total', '?')} 个）"
                + (f'，下一个 {nxt}' if nxt else '，这是最后一个'))
    if t == 'task_completed':
        return f"任务完成，共走了 {len(d.get('visited') or [])}/{d.get('total')} 个点"
    if t == 'task_failed':
        return f"任务失败，错误码 {d.get('errorHex')}"
    if t == 'task_started':
        return f"任务开始：{d.get('map')} 共 {len(d.get('path') or [])} 个点"
    if t == 'task_stopped':
        return '任务被停止'
    if t == 'obstacle':
        return '开始避障' if d.get('avoiding') else '避障结束，继续前进'
    if t == 'localization':
        return '定位恢复' if d.get('valid') else '丢定位了'
    if t == 'emergency':
        return '急停指令开始下发' if d.get('active') else '急停已取消'
    if t in ('online', 'offline'):
        return LABEL[t]
    return json.dumps(d, ensure_ascii=False)


def level_of(ev: dict) -> str:
    t, d = ev.get('type'), ev.get('data') or {}
    if t in ('task_failed', 'offline') or (t == 'localization' and not d.get('valid')):
        return 'error' if t == 'task_failed' else 'warn'
    if t in ('obstacle', 'emergency', 'task_stopped'):
        return 'warn'
    return 'info'


class CloudEventListener(threading.Thread):
    def __init__(self, gateway, db, bus, cfg, run_ref=None) -> None:
        super().__init__(name='cloud-events', daemon=True)
        self.gateway, self.db, self.bus, self.cfg = gateway, db, bus, cfg
        self.run_ref = run_ref            # callable → (run_id, leg_id) | None：把云端事件挂到正在跑的执行上
        self.cursor: int | None = None
        self.connected = False
        self.transport = 'sse'
        self.last_error: str | None = None
        self.received = 0
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sse_failures = 0

    # ── 订阅（执行器用）──────────────────────────────────────────────────────
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def stop(self) -> None:
        self._stop.set()

    def state(self) -> dict:
        return {'connected': self.connected, 'transport': self.transport, 'cursor': self.cursor,
                'received': self.received, 'last_error': self.last_error}

    # ── 主循环 ───────────────────────────────────────────────────────────────
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._sse_failures >= 3:
                    self.transport = 'poll'
                    self._poll_for(60.0)
                    self._sse_failures = 0
                    self.transport = 'sse'
                else:
                    self._stream_sse()
            except RobotError as e:
                self.connected = False
                self.last_error = str(e)
                self._sse_failures += 1
                self._stop.wait(2.0 if e.status != 0 else 5.0)
            except Exception as e:      # noqa: BLE001
                self.connected = False
                self.last_error = str(e)
                self._sse_failures += 1
                self._stop.wait(2.0)

    def _sync_cursor_with_server(self) -> None:
        """服务端 seq 比我们的游标小 → 网关重启过（seq 归零），继续用旧游标会静默漏事件；重置到服务端当前 seq。"""
        server_seq = int(self.gateway.events().get('seq') or 0)
        if self.cursor is None or server_seq < self.cursor:
            old = self.cursor
            self.cursor = server_seq
            if old is not None:
                self.db.add_event('system', 'event_cursor_reset', level='warn',
                                  message=f'云端事件序号回退（{old} → {server_seq}），网关可能重启过；游标已重置，中间的事件无法补回',
                                  data={'old': old, 'server_seq': server_seq})
                self.bus.publish('event', {'source': 'system', 'type': 'event_cursor_reset', 'level': 'warn',
                                           'message': f'云端事件序号回退（{old} → {server_seq}），游标已重置', 'data': {}})

    def _stream_sse(self) -> None:
        g = self.gateway
        self._sync_cursor_with_server()
        url = f'{g.host}/v1/robots/{urllib.parse.quote(g.robot)}/events?since={self.cursor}&stream=1'
        req = urllib.request.Request(url, headers={'X-API-Key': g.raw.api_key, 'Accept': 'text/event-stream'})
        g.limiter.acquire()
        try:
            resp = urllib.request.urlopen(req, timeout=60, context=g.raw._ctx)
        except urllib.error.HTTPError as e:
            raise RobotError(f'事件流 HTTP {e.code}', e.code)
        except urllib.error.URLError as e:
            raise RobotError(f'事件流网络失败: {e}', 0)
        with resp:
            self.connected = True
            self._sse_failures = 0
            data_lines: list[str] = []
            while not self._stop.is_set():
                try:
                    raw = resp.readline()
                except (TimeoutError, OSError) as e:
                    raise RobotError(f'事件流读超时/中断: {e}', 0)
                if not raw:
                    raise RobotError('事件流被服务端关闭', 0)
                line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                if line == '':
                    if data_lines:
                        try:
                            ev = json.loads('\n'.join(data_lines))
                            self._handle(ev)
                        except ValueError:
                            pass
                        data_lines = []
                    continue
                if line.startswith(':'):
                    continue                         # 心跳/连接注释
                if line.startswith('data:'):
                    data_lines.append(line[5:].strip())
        self.connected = False

    def _poll_for(self, seconds: float) -> None:
        t0 = time.time()
        self._sync_cursor_with_server()
        while not self._stop.is_set() and time.time() - t0 < seconds:
            batch = self.gateway.events(since=self.cursor)
            self.connected = True
            for ev in batch.get('events') or []:
                self._handle(ev)
            nxt = batch.get('nextSince')
            if isinstance(nxt, int):
                self.cursor = max(self.cursor or 0, nxt)
            self._stop.wait(1.5)

    # ── 事件处理 ─────────────────────────────────────────────────────────────
    def _handle(self, ev: dict) -> None:
        seq = int(ev.get('seq') or 0)
        if self.cursor is not None and seq <= self.cursor:
            return                                   # 重连补漏时的重复
        self.cursor = seq
        self.received += 1
        ts_ms = ev.get('ts')
        ts = None
        if ts_ms:
            import datetime as _dt
            ts = _dt.datetime.fromtimestamp(ts_ms / 1000).astimezone().isoformat(timespec='milliseconds')
        run_id = leg_id = None
        if self.run_ref is not None:
            try:
                ref = self.run_ref()
                if ref:
                    run_id, leg_id = ref
            except Exception:      # noqa: BLE001
                pass
        row_id = self.db.add_event('cloud', ev.get('type', '?'), cloud_seq=seq, data=ev.get('data') or {},
                                   message=describe(ev), level=level_of(ev), ts=ts, run_id=run_id, leg_id=leg_id)
        row = {'id': row_id, 'ts': ts, 'source': 'cloud', 'type': ev.get('type'), 'cloud_seq': seq,
               'level': level_of(ev), 'message': describe(ev), 'data': ev.get('data') or {}, 'run_id': run_id, 'leg_id': leg_id}
        self.bus.publish('event', row)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


__all__ = ['CloudEventListener', 'describe', 'LABEL', 'dumps']
