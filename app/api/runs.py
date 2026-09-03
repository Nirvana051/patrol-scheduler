# -*- coding: utf-8 -*-
"""执行记录：列表、详情（段 + 检查）、控制（暂停/继续/跳过/中止）。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import bad_request, ctx_of, not_found
from app.db import loads

router = APIRouter(tags=['runs'])


def _insp(r: dict, media='/media') -> dict:
    r = dict(r)
    for k in ('image_path', 'crop_path', 'tts_audio_path'):
        r[k.replace('_path', '_url')] = f"{media}/{r[k]}" if r.get(k) else None
    if r.get('image_path'):
        r['annot_url'] = f"{media}/{r['image_path'].replace('_pano.jpg', '_annot.jpg')}"
    r['tts_status'] = loads(r.get('tts_status'), r.get('tts_status'))
    return r


def _leg(r: dict) -> dict:
    r = dict(r)
    r['path'] = loads(r.get('path'), [])
    r['cloud_task'] = loads(r.get('cloud_task'), None)
    return r


@router.get('/api/runs')
def list_runs(request: Request, limit: int = 50):
    c = ctx_of(request)
    rows = c.db.query('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (max(1, min(limit, 500)),))
    for r in rows:
        r['summary'] = loads(r.get('summary'), {})
    return {'items': rows, 'active': c.runs.active_info()}


@router.get('/api/runs/active')
def active_run(request: Request):
    c = ctx_of(request)
    info = c.runs.active_info()
    return {'active': info, 'run': c.runs.get_run(info['run_id']) if info else None}


@router.get('/api/runs/{run_id}')
def get_run(run_id: int, request: Request):
    c = ctx_of(request)
    r = c.runs.get_run(run_id)
    if not r:
        raise not_found('执行记录不存在')
    r['legs'] = [_leg(x) for x in c.db.query('SELECT * FROM run_legs WHERE run_id=? ORDER BY seq', (run_id,))]
    r['inspections'] = [_insp(x) for x in c.db.query('SELECT * FROM inspections WHERE run_id=? ORDER BY id', (run_id,))]
    r['events'] = c.db.query('SELECT * FROM events WHERE run_id=? ORDER BY id', (run_id,))
    for e in r['events']:
        e['data'] = loads(e.get('data'), {})
    return r


@router.get('/api/runs/{run_id}/inspections')
def run_inspections(run_id: int, request: Request):
    c = ctx_of(request)
    return {'items': [_insp(x) for x in c.db.query('SELECT * FROM inspections WHERE run_id=? ORDER BY id', (run_id,))]}


@router.get('/api/inspections')
def list_inspections(request: Request, limit: int = 50):
    c = ctx_of(request)
    return {'items': [_insp(x) for x in c.db.query('SELECT * FROM inspections ORDER BY id DESC LIMIT ?', (max(1, min(limit, 500)),))]}


@router.get('/api/inspections/{insp_id}')
def get_inspection(insp_id: int, request: Request):
    c = ctx_of(request)
    r = c.db.query_one('SELECT * FROM inspections WHERE id=?', (insp_id,))
    if not r:
        raise not_found('检查记录不存在')
    return _insp(r)


@router.post('/api/runs/{run_id}/{action}')
def control(run_id: int, action: str, request: Request):
    c = ctx_of(request)
    if action not in ('pause', 'resume', 'skip', 'abort'):
        raise bad_request('未知操作')
    try:
        return c.runs.control(run_id, action)
    except ValueError as e:
        raise bad_request(str(e))
