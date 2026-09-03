# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ctx_of
from app.db import loads

router = APIRouter(prefix='/api/events', tags=['events'])


@router.get('')
def list_events(request: Request, before_id: int | None = None, after_id: int | None = None,
                source: str | None = None, type: str | None = None, run_id: int | None = None,
                level: str | None = None, q: str | None = None, limit: int = 100):
    c = ctx_of(request)
    where, params = [], []
    if before_id is not None:
        where.append('id < ?'); params.append(before_id)
    if after_id is not None:
        where.append('id > ?'); params.append(after_id)
    if source:
        where.append('source = ?'); params.append(source)
    if type:
        where.append('type = ?'); params.append(type)
    if run_id is not None:
        where.append('run_id = ?'); params.append(run_id)
    if level:
        where.append('level = ?'); params.append(level)
    if q:
        where.append('(message LIKE ? OR type LIKE ?)'); params.extend([f'%{q}%', f'%{q}%'])
    sql = 'SELECT * FROM events' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id DESC LIMIT ?'
    params.append(max(1, min(limit, 1000)))
    rows = c.db.query(sql, params)
    for r in rows:
        r['data'] = loads(r.get('data'), {})
    types = [r['type'] for r in c.db.query('SELECT DISTINCT type FROM events ORDER BY type')]
    return {'items': rows, 'types': types}
