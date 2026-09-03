# -*- coding: utf-8 -*-
"""服务端 → 浏览器的 SSE：机器人状态、事件、执行/段更新、检查结果、TTS 指令。"""
from __future__ import annotations

import json
import queue
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ctx_of

router = APIRouter(tags=['stream'])


@router.get('/api/stream')
def stream(request: Request):
    c = ctx_of(request)
    q = c.bus.subscribe()
    c.status.hold_active()

    def gen():
        try:
            hello = {'kind': 'hello', 'payload': {'status': c.status.get(), 'events': c.events.state(),
                                                  'active_run': c.runs.active_info(), 'mode': c.gateway.mode,
                                                  'ts': time.time()}}
            yield f"event: hello\ndata: {json.dumps(hello, ensure_ascii=False, default=str)}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                yield f"event: {msg['kind']}\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
        finally:
            c.bus.unsubscribe(q)
            c.status.release_active()
    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
