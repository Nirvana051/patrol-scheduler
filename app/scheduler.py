# -*- coding: utf-8 -*-
"""定时计划：每天固定时刻（daily "07:00,19:30"）或每 N 分钟（interval "120"）自动开始一次执行。

刻意不用 cron 表达式与第三方库：现场需要的就这两种；到点时若已有执行在跑则跳过并记事件，
前置检查由执行本身完成（不通过就 aborted，同样留下记录）。
"""
from __future__ import annotations

import datetime as dt
import threading

from app.db import now_iso


def parse_spec(kind: str, spec: str) -> list:
    kind = (kind or '').strip()
    spec = (spec or '').strip()
    if kind == 'daily':
        times = []
        for part in spec.replace('，', ',').split(','):
            part = part.strip()
            if not part:
                continue
            hh, mm = part.split(':')
            h, m = int(hh), int(mm)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f'时刻不合法：{part}')
            times.append((h, m))
        if not times:
            raise ValueError('daily 需要至少一个 HH:MM')
        return sorted(set(times))
    if kind == 'interval':
        minutes = float(spec)
        if minutes < 1:
            raise ValueError('interval 至少 1 分钟')
        return [minutes]
    raise ValueError(f'未知计划类型：{kind}')


def next_run(kind: str, spec: str, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now().astimezone()
    parsed = parse_spec(kind, spec)
    if kind == 'interval':
        return now + dt.timedelta(minutes=parsed[0])
    today = now.date()
    for day_offset in (0, 1):
        d = today + dt.timedelta(days=day_offset)
        for h, m in parsed:
            cand = dt.datetime.combine(d, dt.time(h, m), tzinfo=now.tzinfo)
            if cand > now:
                return cand
    return dt.datetime.combine(today + dt.timedelta(days=1), dt.time(*parsed[0]), tzinfo=now.tzinfo)


class ScheduleRunner(threading.Thread):
    def __init__(self, ctx, tick_seconds: float = 20.0) -> None:
        super().__init__(name='scheduler', daemon=True)
        self.ctx = ctx
        self.tick = max(0.5, float(tick_seconds))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as e:      # noqa: BLE001 —— 调度线程不能死
                self.ctx.log_event('schedule_error', f'定时器异常：{e}', level='error')
            self._stop.wait(self.tick)

    def tick_once(self) -> int:
        db = self.ctx.db
        now = dt.datetime.now().astimezone()
        due = db.query("SELECT s.*, t.name AS task_name FROM schedules s JOIN tasks t ON t.id=s.task_id "
                       "WHERE s.enabled=1 AND s.next_run_at IS NOT NULL AND s.next_run_at <= ? ORDER BY s.next_run_at", (now.isoformat(timespec='milliseconds'),))
        fired = 0
        for sch in due:
            nxt = next_run(sch['kind'], sch['spec'], now).isoformat(timespec='milliseconds')
            try:
                run = self.ctx.runs.start(sch['task_id'])
                result = f"started run {run['id']}"
                self.ctx.log_event('schedule_fired', f"定时计划 #{sch['id']}：开始执行任务「{sch['task_name']}」（run #{run['id']}）",
                                   run_id=run['id'], data={'schedule_id': sch['id']})
                fired += 1
            except RuntimeError as e:          # 已有执行在跑
                result = f'skipped: {e}'
                self.ctx.log_event('schedule_skipped', f"定时计划 #{sch['id']}：跳过（{e}）", level='warn', data={'schedule_id': sch['id']})
            except ValueError as e:            # 任务被删/没有航点
                result = f'error: {e}'
                self.ctx.log_event('schedule_error', f"定时计划 #{sch['id']}：无法执行（{e}）", level='error', data={'schedule_id': sch['id']})
            db.execute('UPDATE schedules SET last_run_at=?, last_result=?, next_run_at=?, updated_at=? WHERE id=?',
                       (now_iso(), result, nxt, now_iso(), sch['id']))
            self.ctx.bus.publish('schedule', {'id': sch['id'], 'last_result': result, 'next_run_at': nxt})
        return fired
