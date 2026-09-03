# -*- coding: utf-8 -*-
"""机器人：状态、初始化三步、急停、停任务、抓图、视频地址。"""
from __future__ import annotations

import time
import urllib.parse

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.deps import bad_request, ctx_of

router = APIRouter(prefix='/api/robot', tags=['robot'])
_codes_cache: dict = {'ts': 0, 'data': None}


class LocalizeIn(BaseModel):
    map_name: str
    node_id: str
    pose: dict | None = None


class EstopIn(BaseModel):
    active: bool = True


class DeviceIn(BaseModel):
    wait: bool = False


@router.get('/status')
def status(request: Request):
    c = ctx_of(request)
    s = c.status.get()
    s['events'] = c.events.state()
    s['active_run'] = c.runs.active_info()
    s['snapshot_source'] = c.snapshot.describe()
    return s


@router.post('/status/refresh')
def refresh(request: Request):
    return ctx_of(request).status.refresh(full=True)


@router.get('/preflight')
def preflight(request: Request, require_localized: bool = True):
    return ctx_of(request).ops.preflight(require_localized=require_localized)


@router.post('/init/device-start')
def device_start(body: DeviceIn, request: Request):
    return ctx_of(request).ops.device_start(wait=body.wait)


@router.post('/init/device-stop')
def device_stop(body: DeviceIn, request: Request):
    return ctx_of(request).ops.device_stop(wait=body.wait)


@router.get('/init/device-status')
def device_status(task_id: str, request: Request, starting: bool = True):
    c = ctx_of(request)
    st = c.ops.device_status(task_id, starting=starting)
    if st.get('completed'):
        c.status.refresh(full=True)
        c.log_event('device_started' if starting else 'device_stopped',
                    ('设备启动完成' if starting else '设备已停止') + ('' if st.get('result_success', True) else '（脚本报告失败）'),
                    level='info' if st.get('result_success', True) else 'error', data=st)
    return st


@router.post('/init/localize')
def localize(body: LocalizeIn, request: Request):
    if not body.map_name or not body.node_id:
        raise bad_request('map_name 与 node_id 必填')
    return ctx_of(request).ops.localize(body.map_name, body.node_id, body.pose)


@router.post('/estop')
def estop(body: EstopIn, request: Request):
    return ctx_of(request).ops.estop(body.active)


@router.delete('/task')
def stop_task(request: Request):
    return ctx_of(request).ops.stop_task()


@router.post('/snapshot')
def snapshot(request: Request):
    return ctx_of(request).ops.snapshot()


@router.get('/video')
def video(request: Request):
    c = ctx_of(request)
    info = (c.status.get().get('info') or {})
    path = info.get('rtspPath') or ''
    host = urllib.parse.urlparse(c.gateway.host).hostname or ''
    return {'rtsp_path': path, 'hls': f'{c.gateway.host}/hls/{path}/index.m3u8' if path else None,
            'rtsp': f'rtsp://{host}:8554/{path}' if path else None, 'mode': c.gateway.mode}


@router.get('/status-codes')
def status_codes(request: Request):
    c = ctx_of(request)
    if not _codes_cache['data'] or time.time() - _codes_cache['ts'] > 600:
        _codes_cache['data'] = c.gateway.status_codes()
        _codes_cache['ts'] = time.time()
    return _codes_cache['data']
