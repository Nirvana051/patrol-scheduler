# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.deps import bad_request, ctx_of, not_found
from app.db import now_iso
from app.scheduler import next_run, parse_spec

router = APIRouter(prefix='/api/schedules', tags=['schedules'])


class ScheduleIn(BaseModel):
    task_id: int
    kind: str = 'daily'            # daily | interval
    spec: str = '08:00'
    enabled: bool = True


def _rows(c, where: str = '', params=()):
    return c.db.query('SELECT s.*, t.name AS task_name FROM schedules s JOIN tasks t ON t.id=s.task_id ' + where + ' ORDER BY s.id', params)


@router.get('')
def list_schedules(request: Request, task_id: int | None = None):
    c = ctx_of(request)
    rows = _rows(c, 'WHERE s.task_id=?', (task_id,)) if task_id else _rows(c)
    for r in rows:
        r['enabled'] = bool(r['enabled'])
    return {'items': rows}


@router.post('', status_code=201)
def create_schedule(body: ScheduleIn, request: Request):
    c = ctx_of(request)
    if not c.db.query_one('SELECT 1 FROM tasks WHERE id=?', (body.task_id,)):
        raise not_found('任务不存在')
    try:
        parse_spec(body.kind, body.spec)
    except ValueError as e:
        raise bad_request(str(e))
    nxt = next_run(body.kind, body.spec).isoformat(timespec='milliseconds') if body.enabled else None
    sid = c.db.execute('INSERT INTO schedules(task_id,kind,spec,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (body.task_id, body.kind, body.spec, int(body.enabled), nxt, now_iso(), now_iso()))
    c.log_event('schedule_created', f'新建定时计划 #{sid}（{body.kind} {body.spec}）', data={'schedule_id': sid, 'task_id': body.task_id})
    return _rows(c, 'WHERE s.id=?', (sid,))[0]


@router.put('/{sid}')
def update_schedule(sid: int, body: ScheduleIn, request: Request):
    c = ctx_of(request)
    if not c.db.query_one('SELECT 1 FROM schedules WHERE id=?', (sid,)):
        raise not_found('计划不存在')
    try:
        parse_spec(body.kind, body.spec)
    except ValueError as e:
        raise bad_request(str(e))
    nxt = next_run(body.kind, body.spec).isoformat(timespec='milliseconds') if body.enabled else None
    c.db.execute('UPDATE schedules SET task_id=?,kind=?,spec=?,enabled=?,next_run_at=?,updated_at=? WHERE id=?',
                 (body.task_id, body.kind, body.spec, int(body.enabled), nxt, now_iso(), sid))
    return _rows(c, 'WHERE s.id=?', (sid,))[0]


@router.delete('/{sid}')
def delete_schedule(sid: int, request: Request):
    c = ctx_of(request)
    c.db.execute('DELETE FROM schedules WHERE id=?', (sid,))
    return {'deleted': sid}


@router.post('/{sid}/fire')
def fire_now(sid: int, request: Request):
    """把 next_run_at 拨到现在，下一次 tick 立刻执行（测试/演示用）。"""
    c = ctx_of(request)
    if not c.db.query_one('SELECT 1 FROM schedules WHERE id=?', (sid,)):
        raise not_found('计划不存在')
    c.db.execute('UPDATE schedules SET next_run_at=?, enabled=1, updated_at=? WHERE id=?', (now_iso(), now_iso(), sid))
    fired = c.schedules.tick_once()
    return {'fired': fired, 'schedule': _rows(c, 'WHERE s.id=?', (sid,))[0]}
