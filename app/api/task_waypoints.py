# -*- coding: utf-8 -*-
"""任务航点（单表）CRUD + 参考图 + 试问 VLM / 试听 TTS。"""
from __future__ import annotations

import io
import time

from fastapi import APIRouter, File, Request, UploadFile
from PIL import Image
from pydantic import BaseModel, Field

from app.api.deps import bad_request, ctx_of, not_found
from app.db import dumps, loads, now_iso
from app.executor.inspection import DEFAULT_TEMPLATE, pick_tts
from app.media import pano
from app.media.snapshot import SnapshotError, save_jpeg
from app.vlm.base import SYSTEM_PROMPT, build_user_prompt

router = APIRouter(tags=['task-waypoints'])


class TaskWaypointIn(BaseModel):
    name: str = Field(min_length=1)
    map_name: str = Field(min_length=1)
    nav_node_id: str | None = None
    x: float | None = None
    y: float | None = None
    z: float = 0.0
    yaw: float | None = None
    prompt: str = ''
    angle_from: float = 150.0
    angle_to: float = 210.0
    answer_template: dict = Field(default_factory=dict)
    reference_image: str | None = None
    enabled: bool = True


def _row(r: dict) -> dict:
    r = dict(r)
    r['answer_template'] = loads(r.get('answer_template'), {})
    r['enabled'] = bool(r.get('enabled', 1))
    r['reference_image_url'] = f"/media/{r['reference_image']}" if r.get('reference_image') else None
    return r


def _get(c, tw_id: int) -> dict:
    r = c.db.query_one('SELECT * FROM task_waypoints WHERE id=?', (tw_id,))
    if not r:
        raise not_found('任务航点不存在')
    return _row(r)


def _resolve_pose(c, body: TaskWaypointIn) -> dict:
    x, y, z, yaw = body.x, body.y, body.z, body.yaw
    if body.nav_node_id:
        nav = c.db.query_one('SELECT * FROM nav_waypoints WHERE map_name=? AND node_id=?', (body.map_name, body.nav_node_id))
        if not nav:
            raise bad_request(f'导航航点 {body.nav_node_id} 不在地图 {body.map_name} 的缓存里（先同步地图）')
        if x is None or y is None:
            x, y, z = nav['x'], nav['y'], nav['z']
        if yaw is None:
            yaw = nav['yaw']
    if x is None or y is None:
        raise bad_request('需要 nav_node_id 或 x/y 坐标')
    return {'x': float(x), 'y': float(y), 'z': float(z or 0), 'yaw': float(yaw or 0)}


@router.get('/api/task-waypoints')
def list_tw(request: Request, map_name: str | None = None):
    c = ctx_of(request)
    if map_name:
        rows = c.db.query('SELECT * FROM task_waypoints WHERE map_name=? ORDER BY id', (map_name,))
    else:
        rows = c.db.query('SELECT * FROM task_waypoints ORDER BY id')
    return {'items': [_row(r) for r in rows]}


@router.post('/api/task-waypoints', status_code=201)
def create_tw(body: TaskWaypointIn, request: Request):
    c = ctx_of(request)
    pose = _resolve_pose(c, body)
    tpl = {**DEFAULT_TEMPLATE, **(body.answer_template or {})}
    now = now_iso()
    tw_id = c.db.execute(
        'INSERT INTO task_waypoints(name,map_name,nav_node_id,x,y,z,yaw,prompt,angle_from,angle_to,answer_template,reference_image,enabled,created_at,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (body.name, body.map_name, body.nav_node_id, pose['x'], pose['y'], pose['z'], pose['yaw'], body.prompt,
         pano.norm_deg(body.angle_from), pano.norm_deg(body.angle_to), dumps(tpl), body.reference_image, int(body.enabled), now, now))
    c.log_event('task_waypoint_created', f'新建任务航点「{body.name}」（导航航点 {body.nav_node_id or "手工坐标"}）', data={'id': tw_id})
    return _get(c, tw_id)


@router.get('/api/task-waypoints/{tw_id}')
def get_tw(tw_id: int, request: Request):
    return _get(ctx_of(request), tw_id)


@router.put('/api/task-waypoints/{tw_id}')
def update_tw(tw_id: int, body: TaskWaypointIn, request: Request):
    c = ctx_of(request)
    old = _get(c, tw_id)
    pose = _resolve_pose(c, body)
    tpl = {**DEFAULT_TEMPLATE, **(body.answer_template or {})}
    c.db.execute(
        'UPDATE task_waypoints SET name=?,map_name=?,nav_node_id=?,x=?,y=?,z=?,yaw=?,prompt=?,angle_from=?,angle_to=?,answer_template=?,reference_image=?,enabled=?,updated_at=? WHERE id=?',
        (body.name, body.map_name, body.nav_node_id, pose['x'], pose['y'], pose['z'], pose['yaw'], body.prompt,
         pano.norm_deg(body.angle_from), pano.norm_deg(body.angle_to), dumps(tpl),
         body.reference_image if body.reference_image is not None else old.get('reference_image'), int(body.enabled), now_iso(), tw_id))
    return _get(c, tw_id)


@router.delete('/api/task-waypoints/{tw_id}')
def delete_tw(tw_id: int, request: Request):
    c = ctx_of(request)
    _get(c, tw_id)
    used = c.db.query_one('SELECT COUNT(*) AS n FROM task_items WHERE task_waypoint_id=?', (tw_id,))
    c.db.execute('DELETE FROM task_waypoints WHERE id=?', (tw_id,))
    return {'deleted': tw_id, 'removed_from_tasks': used['n'] if used else 0}


# ── 参考图 ───────────────────────────────────────────────────────────────────
def _save_reference(c, tw: dict, data: bytes) -> dict:
    p = save_jpeg(data, c.media_dir / 'refs', f"tw{tw['id']}_{time.strftime('%Y%m%d_%H%M%S')}")
    rel = str(p.relative_to(c.media_dir))
    c.db.execute('UPDATE task_waypoints SET reference_image=?, updated_at=? WHERE id=?', (rel, now_iso(), tw['id']))
    img = Image.open(p)
    return {'reference_image': rel, 'url': f'/media/{rel}', 'width': img.width, 'height': img.height}


@router.post('/api/task-waypoints/{tw_id}/reference-image')
def capture_reference(tw_id: int, request: Request):
    c = ctx_of(request)
    tw = _get(c, tw_id)
    try:
        data = c.snapshot.grab({'waypoint': tw, 'reason': 'reference'})
    except SnapshotError as e:
        raise bad_request(f'抓图失败：{e}')
    return _save_reference(c, tw, data)


@router.post('/api/task-waypoints/{tw_id}/reference-image/upload')
async def upload_reference(tw_id: int, request: Request, file: UploadFile = File(...)):
    c = ctx_of(request)
    tw = _get(c, tw_id)
    raw = await file.read()
    try:
        img = Image.open(io.BytesIO(raw)).convert('RGB')
    except OSError:
        raise bad_request('不是可识别的图片')
    return _save_reference(c, tw, pano.to_jpeg(img))


# ── 试问 VLM / 试听 TTS ─────────────────────────────────────────────────────
class TestVlmIn(BaseModel):
    use: str = 'reference'            # reference | capture
    prompt: str | None = None
    angle_from: float | None = None
    angle_to: float | None = None


@router.post('/api/task-waypoints/{tw_id}/test-vlm')
def test_vlm(tw_id: int, body: TestVlmIn, request: Request):
    c = ctx_of(request)
    tw = _get(c, tw_id)
    if body.use == 'capture' or not tw.get('reference_image'):
        try:
            data = c.snapshot.grab({'waypoint': tw, 'reason': 'test'})
        except SnapshotError as e:
            raise bad_request(f'抓图失败：{e}')
        if not tw.get('reference_image'):
            _save_reference(c, tw, data)
    else:
        data = (c.media_dir / tw['reference_image']).read_bytes()
    af = pano.norm_deg(body.angle_from if body.angle_from is not None else tw['angle_from'])
    at = pano.norm_deg(body.angle_to if body.angle_to is not None else tw['angle_to'])
    prompt = (body.prompt if body.prompt is not None else tw['prompt']).strip()
    if not prompt:
        raise bad_request('prompt 为空')
    img = Image.open(io.BytesIO(data)).convert('RGB')
    crop_bytes = pano.to_jpeg(pano.crop_angle_range(img, af, at, pad_deg=5))
    p_crop = save_jpeg(crop_bytes, c.media_dir / 'tests', f"tw{tw_id}_{time.strftime('%H%M%S')}_crop")
    images = [(crop_bytes, 'image/jpeg')]
    if c.cfg.get_bool('VLM_SEND_FULL_PANO'):
        images.append((data, 'image/jpeg'))
    forward = c.cfg.get_float('FORWARD_DEG')
    user = build_user_prompt(prompt, af, at, forward_deg=forward, waypoint_name=tw['name'])
    res = c.vlm.ask_yes_no(images, user, system=SYSTEM_PROMPT)
    text, passed = pick_tts(tw.get('answer_template') or {}, res.answer, tw['name'])
    return {'vlm': res.to_dict(), 'crop_url': f'/media/{p_crop.relative_to(c.media_dir)}', 'prompt_sent': user,
            'tts_text': text, 'passed': passed, 'angle_from': af, 'angle_to': at}


class TtsIn(BaseModel):
    text: str


@router.post('/api/tts/test')
def tts_test(body: TtsIn, request: Request):
    c = ctx_of(request)
    if not body.text.strip():
        raise bad_request('文本为空')
    return c.tts.speak(body.text, {'reason': 'test'})
