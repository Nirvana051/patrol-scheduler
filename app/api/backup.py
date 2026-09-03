# -*- coding: utf-8 -*-
"""导出 / 导入任务航点、任务、定时计划（JSON），以及重建图后把任务航点重定向到新地图的最近导航航点。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import bad_request, ctx_of, not_found
from app.db import dumps, loads, now_iso

router = APIRouter(tags=['backup'])
EXPORT_VERSION = 1


@router.get('/api/export')
def export_all(request: Request, map_name: str | None = None):
    c = ctx_of(request)
    q = ' WHERE map_name=?' if map_name else ''
    params = (map_name,) if map_name else ()
    tws = c.db.query('SELECT * FROM task_waypoints' + q + ' ORDER BY id', params)
    for t in tws:
        t['answer_template'] = loads(t.get('answer_template'), {})
    tasks = c.db.query('SELECT * FROM tasks' + q + ' ORDER BY id', params)
    for t in tasks:
        t['options'] = loads(t.get('options'), {})
        t['waypoint_names'] = [r['name'] for r in c.db.query(
            'SELECT tw.name FROM task_items ti JOIN task_waypoints tw ON tw.id=ti.task_waypoint_id WHERE ti.task_id=? ORDER BY ti.seq', (t['id'],))]
    scheds = c.db.query('SELECT s.*, t.name AS task_name FROM schedules s JOIN tasks t ON t.id=s.task_id' + (' WHERE t.map_name=?' if map_name else '') + ' ORDER BY s.id', params)
    body = {'version': EXPORT_VERSION, 'exported_at': now_iso(), 'robot': c.gateway.robot,
            'task_waypoints': [{k: t[k] for k in ('name', 'map_name', 'nav_node_id', 'x', 'y', 'z', 'yaw', 'prompt', 'angle_from', 'angle_to', 'answer_template', 'enabled')} for t in tws],
            'tasks': [{k: t[k] for k in ('name', 'map_name', 'description', 'options', 'waypoint_names')} for t in tasks],
            'schedules': [{k: s[k] for k in ('task_name', 'kind', 'spec', 'enabled')} for s in scheds]}
    return JSONResponse(body, headers={'Content-Disposition': 'attachment; filename="patrol-export.json"'})


class ImportIn(BaseModel):
    data: dict
    map_name: str | None = None          # 覆盖导出文件里的地图名（重建图后换到新图）
    overwrite: bool = True               # 同名任务航点/任务覆盖，否则跳过


@router.post('/api/import')
def import_all(body: ImportIn, request: Request):
    c = ctx_of(request)
    d = body.data or {}
    if int(d.get('version', 0)) != EXPORT_VERSION:
        raise bad_request('不支持的导出文件版本')
    now = now_iso()
    stats = {'task_waypoints': 0, 'tasks': 0, 'schedules': 0, 'skipped': 0}
    name_to_id: dict[str, int] = {}
    with c.db.transaction() as conn:
        for tw in d.get('task_waypoints') or []:
            mp = body.map_name or tw['map_name']
            row = conn.execute('SELECT id FROM task_waypoints WHERE name=? AND map_name=?', (tw['name'], mp)).fetchone()
            vals = (tw['name'], mp, tw.get('nav_node_id'), float(tw['x']), float(tw['y']), float(tw.get('z') or 0), float(tw.get('yaw') or 0),
                    tw.get('prompt') or '', float(tw.get('angle_from', 150)), float(tw.get('angle_to', 210)), dumps(tw.get('answer_template') or {}),
                    int(bool(tw.get('enabled', True))))
            if row and not body.overwrite:
                stats['skipped'] += 1
                name_to_id[tw['name']] = row['id']
                continue
            if row:
                conn.execute('UPDATE task_waypoints SET name=?,map_name=?,nav_node_id=?,x=?,y=?,z=?,yaw=?,prompt=?,angle_from=?,angle_to=?,answer_template=?,enabled=?,updated_at=? WHERE id=?',
                             (*vals, now, row['id']))
                name_to_id[tw['name']] = row['id']
            else:
                cur = conn.execute('INSERT INTO task_waypoints(name,map_name,nav_node_id,x,y,z,yaw,prompt,angle_from,angle_to,answer_template,enabled,created_at,updated_at) '
                                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (*vals, now, now))
                name_to_id[tw['name']] = cur.lastrowid
            stats['task_waypoints'] += 1
        task_ids: dict[str, int] = {}
        for t in d.get('tasks') or []:
            mp = body.map_name or t['map_name']
            ids = [name_to_id[n] for n in (t.get('waypoint_names') or []) if n in name_to_id]
            row = conn.execute('SELECT id FROM tasks WHERE name=? AND map_name=?', (t['name'], mp)).fetchone()
            if row and not body.overwrite:
                stats['skipped'] += 1
                task_ids[t['name']] = row['id']
                continue
            if row:
                tid = row['id']
                conn.execute('UPDATE tasks SET description=?, options=?, updated_at=? WHERE id=?', (t.get('description') or '', dumps(t.get('options') or {}), now, tid))
                conn.execute('DELETE FROM task_items WHERE task_id=?', (tid,))
            else:
                tid = conn.execute('INSERT INTO tasks(name,map_name,description,options,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                                   (t['name'], mp, t.get('description') or '', dumps(t.get('options') or {}), now, now)).lastrowid
            for i, tw_id in enumerate(ids, start=1):
                conn.execute('INSERT INTO task_items(task_id,seq,task_waypoint_id) VALUES(?,?,?)', (tid, i, tw_id))
            task_ids[t['name']] = tid
            stats['tasks'] += 1
        for s in d.get('schedules') or []:
            tid = task_ids.get(s.get('task_name'))
            if not tid:
                stats['skipped'] += 1
                continue
            from app.scheduler import next_run, parse_spec
            try:
                parse_spec(s['kind'], s['spec'])
            except ValueError:
                stats['skipped'] += 1
                continue
            nxt = next_run(s['kind'], s['spec']).isoformat(timespec='milliseconds') if s.get('enabled', True) else None
            conn.execute('INSERT INTO schedules(task_id,kind,spec,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                         (tid, s['kind'], s['spec'], int(bool(s.get('enabled', True))), nxt, now, now))
            stats['schedules'] += 1
    c.log_event('imported', f"导入：{stats['task_waypoints']} 个任务航点、{stats['tasks']} 个任务、{stats['schedules']} 个定时计划（跳过 {stats['skipped']}）", data=stats)
    return stats


class RetargetIn(BaseModel):
    from_map: str
    to_map: str
    max_distance: float = 3.0            # 超过这个距离的航点不改，只报告
    move_tasks: bool = True              # 把 from_map 的任务也改到 to_map


@router.post('/api/task-waypoints/retarget')
def retarget(body: RetargetIn, request: Request):
    """重建图后：按 x,y 把旧图上的任务航点重定向到新图最近的导航航点。"""
    c = ctx_of(request)
    g = c.graph(body.to_map)
    if g is None:
        raise not_found(f'新地图 {body.to_map} 尚未同步导航航点')
    rows = c.db.query('SELECT * FROM task_waypoints WHERE map_name=?', (body.from_map,))
    if not rows:
        raise not_found(f'地图 {body.from_map} 上没有任务航点')
    report, moved = [], 0
    now = now_iso()
    for tw in rows:
        nid, d = g.nearest(float(tw['x']), float(tw['y']))
        ok = d <= body.max_distance
        if ok:
            node = g.nodes[nid]
            c.db.execute('UPDATE task_waypoints SET map_name=?, nav_node_id=?, x=?, y=?, z=?, yaw=?, updated_at=? WHERE id=?',
                         (body.to_map, nid, node['x'], node['y'], node['z'], node['yaw'], now, tw['id']))
            moved += 1
        report.append({'id': tw['id'], 'name': tw['name'], 'old_node': tw['nav_node_id'], 'new_node': nid if ok else None,
                       'distance': round(d, 2), 'moved': ok})
    if body.move_tasks:
        c.db.execute('UPDATE tasks SET map_name=?, updated_at=? WHERE map_name=?', (body.to_map, now, body.from_map))
    c.log_event('retargeted', f'任务航点重定向 {body.from_map} → {body.to_map}：{moved}/{len(rows)} 个已改到最近导航航点', level='warn' if moved < len(rows) else 'info',
                data={'moved': moved, 'total': len(rows)})
    return {'moved': moved, 'total': len(rows), 'report': report}

