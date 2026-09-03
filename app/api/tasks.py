# -*- coding: utf-8 -*-
"""任务规划：任务 CRUD、路线预览、执行。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import bad_request, ctx_of, not_found
from app.db import dumps, loads, now_iso
from app.executor.runner import DEFAULT_OPTIONS

router = APIRouter(prefix='/api/tasks', tags=['tasks'])


class TaskIn(BaseModel):
    name: str = Field(min_length=1)
    map_name: str = Field(min_length=1)
    description: str = ''
    options: dict = Field(default_factory=dict)
    waypoint_ids: list[int] = Field(default_factory=list)


def _task(c, task_id: int) -> dict:
    t = c.db.query_one('SELECT * FROM tasks WHERE id=?', (task_id,))
    if not t:
        raise not_found('任务不存在')
    t['options'] = {**DEFAULT_OPTIONS, **loads(t.get('options'), {})}
    items = c.db.query('SELECT ti.seq, ti.task_waypoint_id, tw.name, tw.nav_node_id, tw.x, tw.y, tw.yaw, tw.prompt, '
                       'tw.angle_from, tw.angle_to, tw.enabled, tw.answer_template FROM task_items ti '
                       'JOIN task_waypoints tw ON tw.id=ti.task_waypoint_id WHERE ti.task_id=? ORDER BY ti.seq', (task_id,))
    for it in items:
        it['answer_template'] = loads(it.get('answer_template'), {})
        it['enabled'] = bool(it['enabled'])
    t['items'] = items
    last = c.db.query_one('SELECT id, status, started_at, ended_at FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,))
    t['last_run'] = last
    return t


def _write_items(c, task_id: int, ids: list[int]) -> None:
    with c.db.transaction() as conn:
        conn.execute('DELETE FROM task_items WHERE task_id=?', (task_id,))
        for i, tw_id in enumerate(ids, start=1):
            if not conn.execute('SELECT 1 FROM task_waypoints WHERE id=?', (tw_id,)).fetchone():
                raise bad_request(f'任务航点 {tw_id} 不存在')
            conn.execute('INSERT INTO task_items(task_id,seq,task_waypoint_id) VALUES(?,?,?)', (task_id, i, tw_id))


@router.get('')
def list_tasks(request: Request):
    c = ctx_of(request)
    rows = c.db.query('SELECT t.*, (SELECT COUNT(*) FROM task_items ti WHERE ti.task_id=t.id) AS item_count, '
                      '(SELECT status FROM runs r WHERE r.task_id=t.id ORDER BY r.id DESC LIMIT 1) AS last_status, '
                      '(SELECT started_at FROM runs r WHERE r.task_id=t.id ORDER BY r.id DESC LIMIT 1) AS last_started '
                      'FROM tasks t ORDER BY t.id')
    for r in rows:
        r['options'] = {**DEFAULT_OPTIONS, **loads(r.get('options'), {})}
    return {'items': rows}


@router.post('', status_code=201)
def create_task(body: TaskIn, request: Request):
    c = ctx_of(request)
    now = now_iso()
    task_id = c.db.execute('INSERT INTO tasks(name,map_name,description,options,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                           (body.name, body.map_name, body.description, dumps(body.options), now, now))
    _write_items(c, task_id, body.waypoint_ids)
    c.log_event('task_created', f'新建任务「{body.name}」（{len(body.waypoint_ids)} 个任务航点）', data={'task_id': task_id})
    return _task(c, task_id)


@router.get('/{task_id}')
def get_task(task_id: int, request: Request):
    return _task(ctx_of(request), task_id)


@router.put('/{task_id}')
def update_task(task_id: int, body: TaskIn, request: Request):
    c = ctx_of(request)
    _task(c, task_id)
    c.db.execute('UPDATE tasks SET name=?,map_name=?,description=?,options=?,updated_at=? WHERE id=?',
                 (body.name, body.map_name, body.description, dumps(body.options), now_iso(), task_id))
    _write_items(c, task_id, body.waypoint_ids)
    return _task(c, task_id)


@router.delete('/{task_id}')
def delete_task(task_id: int, request: Request):
    c = ctx_of(request)
    _task(c, task_id)
    c.db.execute('DELETE FROM tasks WHERE id=?', (task_id,))
    return {'deleted': task_id}


@router.get('/{task_id}/plan')
def plan(task_id: int, request: Request, from_node: str | None = None):
    c = ctx_of(request)
    t = _task(c, task_id)
    g = c.graph(t['map_name'])
    if g is None:
        raise bad_request(f"地图 {t['map_name']} 尚未同步导航航点")
    start, start_note = from_node, None
    if not start:
        pos = (c.status.get().get('position') or {})
        if pos.get('x') is not None:
            start, d = g.nearest(float(pos['x']), float(pos['y']))
            start_note = f'按当前位置取最近航点 {start}（{d:.2f} m）'
        elif t['items']:
            start = t['items'][0].get('nav_node_id')
            start_note = f'读不到位置，假定从首个任务航点 {start} 出发'
    legs, cur, total = [], start, 0.0
    for it in t['items']:
        if not it['enabled']:
            continue
        target = it.get('nav_node_id')
        if not target or target not in g:
            target, _ = g.nearest(float(it['x']), float(it['y']))
        path = g.shortest_path(cur, target) if cur else None
        length = g.path_length(path) if path else None
        legs.append({'seq': len(legs) + 1, 'name': it['name'], 'task_waypoint_id': it['task_waypoint_id'],
                     'from_node': cur, 'to_node': target, 'path': path, 'length': None if length is None else round(length, 2),
                     'reachable': path is not None})
        if path:
            total += length
        cur = target
    if t['options'].get('return_to_start') and start and legs:
        path = g.shortest_path(cur, start)
        legs.append({'seq': len(legs) + 1, 'name': '返回起点', 'task_waypoint_id': None, 'from_node': cur, 'to_node': start,
                     'path': path, 'length': None if not path else round(g.path_length(path), 2), 'reachable': path is not None})
        if path:
            total += g.path_length(path)
    return {'task_id': task_id, 'start_node': start, 'start_note': start_note, 'legs': legs,
            'total_length': round(total, 2), 'unreachable': [l['seq'] for l in legs if not l['reachable']]}


@router.post('/{task_id}/run', status_code=202)
def run_task(task_id: int, request: Request):
    c = ctx_of(request)
    try:
        return c.runs.start(task_id)
    except ValueError as e:
        raise bad_request(str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
