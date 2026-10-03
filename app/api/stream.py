# -*- coding: utf-8 -*-
"""服务端 → 浏览器的 SSE：机器人状态、事件、执行/段更新、检查结果、TTS 指令。"""
from __future__ import annotations

import json
import queue
import time

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ctx_of

router = APIRouter(tags=['stream'])
KEEPALIVE_SECONDS = 15.0


@router.get('/api/stream')
async def stream(request: Request):
    c = ctx_of(request)
    q = c.bus.subscribe()
    c.status.hold_active()
    server = getattr(request.app.state, 'server', None)      # app.main 启动时放进来的 uvicorn.Server；测试里没有

    async def gen():
        """异步生成器 + 短超时轮询队列：客户端一断开，Starlette 取消响应任务，CancelledError 立刻在这里的 await 处抛出，
        finally 马上释放订阅与「有页面在看」计数。

        原来是同步生成器（跑在线程池里、q.get 一等 15 s），断开后要等到下一次 keepalive 把线程放出来，
        然后还得靠解释器回收生成器对象才会跑到 finally —— 这件事 Python 3.10 碰巧很快，3.12 下实测 25 s 都等不到
        （三平台统一到 3.12 时由 tests/test_stream.py 抓到）。订阅不释放 = 状态轮询一直按「有页面」的高频率跑。
        """
        try:
            hello = {'kind': 'hello', 'payload': {'status': c.status.get(), 'events': c.events.state(),
                                                  'active_run': c.runs.active_info(), 'mode': c.gateway.mode,
                                                  'ts': time.time()}}
            yield f"event: hello\ndata: {json.dumps(hello, ensure_ascii=False, default=str)}\n\n"
            idle = 0.0
            while True:
                # 进程要退出（SIGTERM 或 /api/shutdown）时主动结束：uvicorn 优雅退出会等在途请求结束，
                # SSE 是永不结束的请求 —— 不自己退，lifespan 收尾（ctx.stop() 停机器人）就要等到超时强杀之后
                if server is not None and getattr(server, 'should_exit', False):
                    break
                try:
                    # 等队列仍在工作线程里做，但取消时立刻放弃那个线程（abandon_on_cancel），不等它的 1 s 超时
                    msg = await anyio.to_thread.run_sync(q.get, True, 1.0, abandon_on_cancel=True)
                except queue.Empty:
                    idle += 1.0
                    if idle >= KEEPALIVE_SECONDS:
                        idle = 0.0
                        yield ': keepalive\n\n'
                    continue
                idle = 0.0
                yield f"event: {msg['kind']}\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                if msg['kind'] == 'shutdown':
                    break
        finally:
            c.bus.unsubscribe(q)
            c.status.release_active()
    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
