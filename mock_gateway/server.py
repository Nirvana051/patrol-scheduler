# -*- coding: utf-8 -*-
"""certaintyX 云端网关的 mock —— 与 Sample_web_api/docs 描述的契约逐条对齐。

    python -m mock_gateway.server --port 18443 --speed 1.0 [--prelocalized] [--prestarted]

复刻的「坑」（每条都在测试里有断言）：
* 401 不区分密钥不存在/吊销/过期；viewer 密钥写操作 403
* 透传通道传别名 → 502「机器人不在线」；/v1 别名与 ID 都认
* POST /task 字段名必须是 map_name / path，写错 400；path 至少两个
* 同一 Idempotency-Key 重放 → 相同响应 + `Idempotent-Replay: true`
* 5 rps 令牌桶限流 → 429 + Retry-After
* 急停体 active 缺省为 False（空体 = 取消急停）
* 未知 task_id → 400「任务 ID 不存在」（不是 404）
* /position 未定位 → 503「机器人位置信息未就绪」
* 控制权：auto 密钥写操作自动接管；被人抢占 → 409 + holder
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .robot_sim import MockRobot, load_maps

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
DEFAULT_KEY = 'cx_mock0001_' + '0' * 48
VIEWER_KEY = 'cx_mockview_' + '1' * 48


def ok(data: Any, status: int = 200, headers: dict | None = None, **extra) -> JSONResponse:
    body = {'success': True, **extra, 'data': data}
    return JSONResponse(body, status_code=status, headers=headers)


def fail(status: int, error: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({'success': False, 'error': error, **extra}, status_code=status, headers=headers)


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, burst
        self.tokens = float(burst)
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


def create_app(robot: MockRobot, *, api_key: str = DEFAULT_KEY, viewer_key: str = VIEWER_KEY,
               rps: float = 5.0) -> FastAPI:
    app = FastAPI(title='certaintyX cloud gateway (mock)', docs_url=None, redoc_url=None)
    app.state.robot = robot
    keys = {api_key: {'id': api_key.split('_')[1] if '_' in api_key else 'mock', 'role': 'operator', 'lease': 'auto'},
            viewer_key: {'id': 'view', 'role': 'viewer', 'lease': 'none'}}
    buckets: dict[str, TokenBucket] = {}
    app.state.buckets = buckets
    idem: dict[tuple, tuple[float, int, Any]] = {}
    idem_lock = threading.Lock()
    v1_index = json.loads((FIXTURES / 'v1_index.json').read_text(encoding='utf-8'))
    status_codes = json.loads((FIXTURES / 'status_codes.json').read_text(encoding='utf-8'))

    # ── 鉴权 / 限流 / 幂等 ─────────────────────────────────────────────────
    def auth(request: Request) -> tuple[dict | None, JSONResponse | None]:
        key = request.headers.get('x-api-key') or ''
        if not key:
            a = request.headers.get('authorization') or ''
            if a.lower().startswith('bearer '):
                key = a[7:].strip()
        info = keys.get(key)
        if not info:
            return None, fail(401, '未登录、令牌已过期或密钥无效')
        b = buckets.setdefault(key, TokenBucket(rps, int(rps)))
        if not b.take():
            return None, fail(429, '请求过于频繁', headers={'Retry-After': '1'})
        return {**info, 'key': key}, None

    def resolve_robot(name: str) -> bool:
        return name in (robot.alias, robot.robot_id)

    def need_online() -> JSONResponse | None:
        return None if robot.online else fail(502, '机器人不在线')

    def write_guard(request: Request, info: dict, *, need_lease: bool = True) -> JSONResponse | None:
        if info['role'] != 'operator':
            return fail(403, '无控制权限（viewer 只读）')
        if info['lease'] == 'none':
            return fail(403, '该密钥为只读（leaseMode=none），无法执行写操作')
        if need_lease:
            okk, holder = robot.acquire_lease(f"api:{info['id']}")
            if not okk:
                return fail(409, f'机器人已被 {holder} 控制，请先接管控制权', holder=holder)
        return None

    def idempotent(request: Request, info: dict, fn: Callable[[], JSONResponse]) -> JSONResponse:
        key = request.headers.get('idempotency-key')
        if not key:
            return fn()
        ik = (info['key'], request.url.path, request.method, key)
        with idem_lock:
            hit = idem.get(ik)
            if hit and time.time() - hit[0] < 600:
                _, st, body = hit
                return JSONResponse(json.loads(body), status_code=st, headers={'Idempotent-Replay': 'true'})
        resp = fn()
        with idem_lock:
            idem[ik] = (time.time(), resp.status_code, resp.body.decode('utf-8'))
        return resp

    async def body_json(request: Request) -> dict:
        try:
            raw = await request.body()
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    # ── 免鉴权 ───────────────────────────────────────────────────────────────
    @app.get('/healthz')
    def healthz():
        return {'ok': True, 'robots': 1}

    @app.get('/v1')
    def v1():
        return JSONResponse(v1_index)

    @app.get('/v1/status-codes')
    def v1_status_codes():
        return JSONResponse(status_codes)

    @app.get('/hls/{path}/index.m3u8')
    def hls(path: str):
        # 没有真实推流者时 mediamtx 返回 404 —— 这里同样
        return JSONResponse({'error': 'no publisher'}, status_code=404)

    # ── /v1 冻结契约 ─────────────────────────────────────────────────────────
    @app.get('/v1/robots')
    def robots(request: Request):
        info, err = auth(request)
        if err:
            return err
        return ok([robot.overview()])

    @app.get('/v1/robots/{name}')
    def robot_overview(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        return ok(robot.overview())

    @app.get('/v1/robots/{name}/telemetry')
    def telemetry(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        return need_online() or ok(robot.telemetry())

    @app.get('/v1/robots/{name}/position')
    def position(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        if not robot.online:
            return fail(502, '机器人不在线')
        p = robot.position()
        if p is None:
            return fail(503, '机器人位置信息未就绪')
        return ok(p, source='global_localization')

    @app.get('/v1/robots/{name}/perception')
    def perception(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        return need_online() or ok(robot.perception())

    @app.get('/v1/robots/{name}/maps')
    def maps(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        return need_online() or ok(list(robot.maps))

    @app.get('/v1/robots/{name}/maps/{map_name}/waypoints')
    def waypoints(name: str, map_name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        if not robot.online:
            return fail(502, '机器人不在线')
        wps = robot.maps.get(map_name)
        if wps is None:
            return fail(404, '地图不存在')
        # 真实网关按字典序返回键（"1","10","11"…），这里保持一致
        return ok({k: wps[k] for k in sorted(wps)})

    @app.get('/v1/robots/{name}/task')
    def task_get(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        return need_online() or ok(robot.task_view())

    @app.post('/v1/robots/{name}/task')
    async def task_post(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        body = await body_json(request)

        def do() -> JSONResponse:
            g = write_guard(request, info)
            if g:
                return g
            if not robot.online:
                return fail(502, '机器人不在线')
            map_name = body.get('map_name') or ''
            path = body.get('path') or []
            if not map_name:
                return fail(400, 'map_name 不能为空')
            if not isinstance(path, list) or len(path) < 2:
                return fail(400, 'path 至少需要两个航点')
            wps = robot.maps.get(map_name)
            if wps is None:
                return fail(404, f'地图不存在: {map_name}')
            path = [str(p) for p in path]
            for p in path:
                if p not in wps:
                    return fail(404, f'航点不存在: {p}')
            return ok(robot.start_task(map_name, path, body))
        return idempotent(request, info, do)

    @app.delete('/v1/robots/{name}/task')
    def task_delete(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')

        def do() -> JSONResponse:
            g = write_guard(request, info)
            if g:
                return g
            if not robot.online:
                return fail(502, '机器人不在线')
            return ok(robot.stop_task())
        return idempotent(request, info, do)

    @app.post('/v1/robots/{name}/estop')
    async def estop(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        body = await body_json(request)

        def do() -> JSONResponse:
            g = write_guard(request, info, need_lease=False)      # 急停免控制权
            if g:
                return g
            if not robot.online:
                return fail(502, '机器人不在线')
            active = bool(body.get('active', False))               # 机器人端就是这么读的：空体 = 取消急停
            return ok(robot.set_estop(active))
        return idempotent(request, info, do)

    @app.get('/v1/robots/{name}/events')
    def events(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        q = request.query_params
        since_raw = q.get('since') or request.headers.get('last-event-id')
        since = int(since_raw) if since_raw not in (None, '') else None
        if q.get('stream') not in ('1', 'true'):
            return ok(robot.events_since(since))

        def gen():
            cursor = since if since is not None else robot.seq
            yield f': connected seq={cursor}\n\n'
            while True:
                evs = robot.wait_events(cursor, timeout=15.0)
                if not evs:
                    yield ': keepalive\n\n'
                    continue
                for e in evs:
                    cursor = max(cursor, e['seq'])
                    yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
        return StreamingResponse(gen(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    def _control(name: str, action: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        if info['role'] != 'operator':
            return fail(403, '无控制权限（viewer 只读）')
        owner = f"api:{info['id']}"
        if action in ('acquire', 'renew'):
            okk, holder = robot.acquire_lease(owner)
            if not okk:
                return fail(409, f'机器人已被 {holder} 控制，请先接管控制权', holder=holder)
            return ok(robot.overview()['lease'])
        if action == 'release':
            robot.release_lease(owner)
            return ok({'released': True})
        return fail(404, '未知操作')

    @app.post('/v1/robots/{name}/control/{action}')
    def control_v1(name: str, action: str, request: Request):
        return _control(name, action, request)

    @app.post('/api/robots/{name}/control/{action}')
    def control_api(name: str, action: str, request: Request):
        return _control(name, action, request)

    # ── 透传通道：只认机器人 ID ───────────────────────────────────────────────
    @app.api_route('/api/robots/{rid}/api/{rest:path}', methods=['GET', 'POST'])
    async def passthrough(rid: str, rest: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        if rid != robot.robot_id or not robot.online:
            return fail(502, '机器人不在线')          # 传别名也会到这里 —— 最容易浪费半天的坑
        body = await body_json(request) if request.method == 'POST' else {}
        q = request.query_params
        if rest == 'device/start' and request.method == 'POST':
            if info['role'] != 'operator':
                return fail(403, '无控制权限（viewer 只读）')
            return JSONResponse({'success': True, 'task_id': robot.device_start(), 'message': '启动脚本已下发'})
        if rest == 'device/stop' and request.method == 'POST':
            if info['role'] != 'operator':
                return fail(403, '无控制权限（viewer 只读）')
            return JSONResponse({'success': True, 'task_id': robot.device_stop(), 'message': '停止脚本已下发'})
        if rest in ('device/start_status', 'device/stop_status'):
            st = robot.device_status(q.get('task_id') or '')
            if st is None:
                return JSONResponse({'success': False, 'error': '任务 ID 不存在'}, status_code=400)
            return JSONResponse(st)
        if rest == 'device/topic_ready':
            return JSONResponse({'success': True, 'data': {'topic': q.get('topic'), 'ready': robot.topic_ready(q.get('topic') or '')}})
        if rest == 'localization/execute' and request.method == 'POST':
            if info['role'] != 'operator':
                return fail(403, '无控制权限（viewer 只读）')
            map_name, node_id = body.get('map_name') or '', str(body.get('node_id') or '')
            if map_name not in robot.maps:
                return JSONResponse({'success': False, 'error': '地图不存在或缺少 pcd 文件'}, status_code=404)
            if node_id not in robot.maps[map_name] and body.get('pose') is None:
                return JSONResponse({'success': False, 'error': f'航点不存在: {node_id}'}, status_code=404)
            if body.get('verify') is False:
                robot.localized = True
                robot.loc_received_at = time.time()
                return JSONResponse({'success': True, 'data': {'sent': True, 'verified': False}})
            return JSONResponse(robot.localize(map_name, node_id, body.get('pose')))
        return JSONResponse({'success': False, 'error': f'未知接口: {rest}'}, status_code=404)

    # ── 仿真控制（无鉴权，仅测试用）─────────────────────────────────────────
    @app.get('/mock/state')
    def mock_state():
        return robot.snapshot_state()

    @app.post('/mock/fault')
    async def mock_fault(request: Request):
        b = await body_json(request)
        robot.inject_fault(b.get('kind'))
        return {'ok': True, 'fault_next_leg': robot.fault_next_leg}

    @app.post('/mock/offline')
    async def mock_offline(request: Request):
        b = await body_json(request)
        robot.set_online(bool(b.get('online', False)))
        return {'ok': True, 'online': robot.online}

    @app.post('/mock/ros')
    async def mock_ros(request: Request):
        b = await body_json(request)
        robot.set_ros(bool(b.get('available', True)))
        return {'ok': True}

    @app.post('/mock/preempt')
    async def mock_preempt(request: Request):
        b = await body_json(request)
        owner = b.get('owner') or 'admin'
        secs = float(b['seconds']) if 'seconds' in b else 60.0
        return {'ok': True, 'lease': robot.preempt(owner, secs)}

    @app.post('/mock/teleport')
    async def mock_teleport(request: Request):
        b = await body_json(request)
        robot.teleport(node_id=b.get('node_id'), x=b.get('x'), y=b.get('y'), map_name=b.get('map_name'))
        return {'ok': True, **robot.snapshot_state()}

    @app.post('/mock/obstacle')
    async def mock_obstacle(request: Request):
        b = await body_json(request)
        robot.obstacle(float(b.get('seconds') or 3))
        return {'ok': True}

    @app.post('/mock/lose-localization')
    async def mock_lose_loc(request: Request):
        b = await body_json(request)
        robot.lose_localization(float(b.get('seconds') or 3))
        return {'ok': True}

    @app.post('/mock/drop-events')
    async def mock_drop(request: Request):
        b = await body_json(request)
        robot.drop_events = bool(b.get('drop', False))
        return {'ok': True, 'drop': robot.drop_events}

    @app.post('/mock/speed')
    async def mock_speed(request: Request):
        b = await body_json(request)
        robot.speed = float(b.get('speed') or 1.0)
        return {'ok': True, 'speed': robot.speed}

    @app.post('/mock/reset')
    async def mock_reset(request: Request):
        b = await body_json(request)
        robot.reset(prelocalized=bool(b.get('prelocalized', False)), prestarted=bool(b.get('prestarted', False)))
        if b.get('node_id'):
            robot.teleport(node_id=str(b['node_id']), map_name=b.get('map_name'))
        return {'ok': True, **robot.snapshot_state()}

    return app


def serve_in_thread(app: FastAPI, host: str = '127.0.0.1', port: int = 18443):
    """在后台线程里跑 uvicorn（测试用）。返回 (server, thread)；停用 server.should_exit = True。"""
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level='warning', access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None      # 非主线程不能装信号处理
    th = threading.Thread(target=server.run, name='mock-gateway', daemon=True)
    th.start()
    t0 = time.time()
    while not server.started and time.time() - t0 < 15:
        time.sleep(0.05)
    return server, th


def main() -> None:
    ap = argparse.ArgumentParser(description='certaintyX 云端网关 mock')
    ap.add_argument('--host', default=os.environ.get('MOCK_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('MOCK_PORT', '18443')))
    ap.add_argument('--speed', type=float, default=float(os.environ.get('MOCK_SPEED', '1.0')), help='机器人速度 m/s')
    ap.add_argument('--prelocalized', action='store_true', default=os.environ.get('MOCK_PRELOCALIZED') == '1')
    ap.add_argument('--prestarted', action='store_true', default=os.environ.get('MOCK_PRESTARTED') == '1')
    ap.add_argument('--device-delay', type=float, default=float(os.environ.get('MOCK_DEVICE_DELAY', '3')))
    ap.add_argument('--localize-delay', type=float, default=float(os.environ.get('MOCK_LOCALIZE_DELAY', '1.5')))
    ap.add_argument('--preprocess', type=float, default=float(os.environ.get('MOCK_PREPROCESS', '0.5')), help='导航预处理秒数（status 2）')
    ap.add_argument('--key', default=os.environ.get('MOCK_API_KEY', DEFAULT_KEY))
    ap.add_argument('--maps', default=os.environ.get('MOCK_MAPS', ''))
    args = ap.parse_args()

    robot = MockRobot(load_maps(args.maps or None), speed=args.speed, prelocalized=args.prelocalized,
                      prestarted=args.prestarted, device_delay=args.device_delay,
                      localize_delay=args.localize_delay, preprocess_seconds=args.preprocess)
    app = create_app(robot, api_key=args.key)
    import uvicorn
    print(f'mock 网关: http://{args.host}:{args.port}  别名 {robot.alias}  ID {robot.robot_id}')
    print(f'operator 密钥: {args.key}\nviewer 密钥:   {VIEWER_KEY}')
    uvicorn.run(app, host=args.host, port=args.port, log_level='info')


if __name__ == '__main__':
    main()
