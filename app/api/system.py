# -*- coding: utf-8 -*-
"""进程级接口：优雅退出（给桌面壳 / 启动器用）与平台信息。"""
from __future__ import annotations

import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request

from app import platform
from app.api.deps import ctx_of

router = APIRouter(tags=['system'])


@router.post('/api/shutdown')
def shutdown(request: Request):
    """让进程优雅退出：uvicorn 停止接收 → lifespan 收尾 → ctx.stop() 中止执行并 DELETE /task 停机器人（C14）。

    只有启动时设置了 `PS_SHUTDOWN_TOKEN` 才启用（桌面壳每次启动随机生成一个），请求头 `X-Shutdown-Token` 必须一致。
    没有 token 的常规部署（systemd / start.sh）这个接口等于不存在，不多暴露一寸攻击面。
    Windows 没有 SIGTERM、Node 也发不出 Ctrl+Break，所以桌面壳的「退出」只能走这里；Linux 启动器也可以用。
    """
    token = os.environ.get('PS_SHUTDOWN_TOKEN') or ''
    if not token:
        raise HTTPException(status_code=404, detail='未启用：启动时没有设置 PS_SHUTDOWN_TOKEN')
    given = request.headers.get('X-Shutdown-Token') or ''
    if not secrets.compare_digest(given.encode('utf-8'), token.encode('utf-8')):
        raise HTTPException(status_code=403, detail='口令不对')
    server = getattr(request.app.state, 'server', None)
    if server is None:
        raise HTTPException(status_code=501, detail='此进程不是由 app.main 启动，没有可控的服务器对象')
    c = ctx_of(request)
    active = c.runs.active_info()
    c.log_event('shutdown_requested', '收到退出请求，开始优雅停止' + ('（有执行在跑，将先中止并停下机器人）' if active else ''),
                level='warn')
    server.should_exit = True
    c.bus.publish('shutdown', {'ts': time.time()})          # 让所有 SSE 订阅者立刻结束，别拖住 uvicorn 的优雅退出
    return {'ok': True, 'active_run': active}


@router.get('/api/platform')
def platform_info():
    return platform.describe()
