# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.deps import bad_request, ctx_of
from app.config import RUNTIME_KEYS, SECRET_KEYS

router = APIRouter(tags=['settings'])


@router.get('/api/settings')
def get_settings(request: Request):
    c = ctx_of(request)
    return {'settings': c.cfg.public(), 'runtime_keys': RUNTIME_KEYS, 'secret_keys': sorted(SECRET_KEYS),
            'adapters': {'vlm': c.vlm.describe(), 'tts': c.tts.describe(), 'snapshot': c.snapshot.describe()},
            'events': c.events.state(), 'mode': c.gateway.mode, 'scene': c.scene}


@router.put('/api/settings')
def put_settings(body: dict, request: Request):
    c = ctx_of(request)
    changed, reconnect = [], False
    for k, v in (body or {}).items():
        if k not in RUNTIME_KEYS:
            raise bad_request(f'不可设置的键：{k}')
        v = '' if v is None else str(v)
        if k in SECRET_KEYS and ('…' in v or v.startswith('****')):
            continue                                   # 掩码原样回传，跳过
        if c.cfg.get(k) == v:
            continue
        c.cfg.set(k, v)
        changed.append(k)
        if k.startswith('CX_') or k == 'RATE_LIMIT_RPS':
            reconnect = True
    if reconnect:
        c.reconnect()
    elif changed:
        c.reload_adapters()
    if changed:
        c.log_event('settings_changed', '设置已更新：' + ', '.join(changed), data={'keys': changed, 'reconnect': reconnect})
    return {'changed': changed, 'reconnected': reconnect, 'settings': c.cfg.public()}


class SceneIn(BaseModel):
    door_open: bool | None = None
    label: str | None = None


@router.post('/api/demo/scene')
def set_scene(body: SceneIn, request: Request):
    """合成全景的场景开关（mock 演示：让「消防栓门」开/关，验证 VLM 与 TTS 分支）。"""
    c = ctx_of(request)
    for k, v in body.model_dump().items():
        if v is not None:
            c.scene[k] = v
    return c.scene
