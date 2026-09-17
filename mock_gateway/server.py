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
* 语音播报 /tts、/tts/audio、GET/DELETE /tts：文字 ≤300 字、音频 ≤5 MB 按文件头识别格式、队列满 20 → 429（不带 Retry-After）、
  wait 最多 50 s → pending、volume 记住；旧固件（tts_firmware_old）整组端点回一页 HTML 的 404
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

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


AUDIO_MAX_BYTES = 5 * 1024 * 1024
AUDIO_FORMATS = {'mp3', 'wav', 'ogg', 'opus', 'flac', 'm4a', 'aac'}
AUDIO_CT = {'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/wave': 'wav',
            'audio/ogg': 'ogg', 'audio/flac': 'flac', 'audio/mp4': 'm4a', 'audio/aac': 'aac'}
# 机器人端在 2026-09-14 之前的固件没有 /tts：Flask 默认的 404 页
HTML_404 = ('<!doctype html>\n<html lang=en>\n<title>404 Not Found</title>\n<h1>Not Found</h1>\n'
            '<p>The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.</p>\n')


def sniff_audio(data: bytes) -> str | None:
    """按文件头识别音频格式（与机器人端一致：比调用方声明的 format 优先）。"""
    if data.startswith(b'ID3') or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return 'mp3'
    if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
        return 'wav'
    if data.startswith(b'OggS'):
        return 'ogg'
    if data.startswith(b'fLaC'):
        return 'flac'
    if data[4:8] == b'ftyp':
        return 'm4a'
    return None


def truthy(v) -> bool:
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


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

    HTML_502 = '<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n'

    def html_502(is_task: bool = False) -> HTMLResponse | None:
        """真实网关偶发：nginx 层直接回整页 HTML 的 502，不是 JSON 信封。"""
        with robot.lock:
            if robot.html502_left > 0 and (is_task or not robot.html502_only_task):
                robot.html502_left -= 1
                return HTMLResponse(HTML_502, status_code=502)
        return None

    def need_online(is_task: bool = False) -> JSONResponse | None:
        return html_502(is_task) or (None if robot.online else fail(502, '机器人不在线'))

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
        return need_online(is_task=True) or ok(robot.task_view())

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
            if robot.robot_version == 'new' and not isinstance(body.get('active'), bool):
                return fail(400, '急停请求必须携带布尔字段 "active"')   # 2026-09-06 起的机器人端不再猜默认值
            active = bool(body.get('active', False))               # legacy 机器人端就是这么读的：空体 = 取消急停
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

    # ── 语音播报（机器人端固件 ≥ 2026-09-14）───────────────────────────────
    def tts_guard(name: str, request: Request, info: dict, *, write: bool = True):
        """公共前置：机器人存在 → 控制权（写操作）→ 在线 → 旧固件一页 HTML 的 404。返回 Response 即出错。"""
        if not resolve_robot(name):
            return fail(404, '机器人不存在')
        if write:
            g = write_guard(request, info)
            if g:
                return g
        if not robot.online:
            return fail(502, '机器人不在线')
        if robot.tts_firmware_old:
            return HTMLResponse(HTML_404, status_code=404)
        return None

    def parse_volume(v):
        if v is None or v == '':
            return None, None
        try:
            iv = int(float(v))
        except (TypeError, ValueError):
            return None, fail(400, 'volume must be 0-100')
        if not 0 <= iv <= 100:
            return None, fail(400, 'volume must be 0-100')
        return iv, None

    def finish(st: int, data: dict) -> JSONResponse:
        return fail(st, data['error']) if st >= 400 else ok(data, status=st)

    @app.post('/v1/robots/{name}/tts')
    async def tts_post(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        body = await body_json(request)

        def do() -> JSONResponse:
            g = tts_guard(name, request, info)
            if g:
                return g
            # 机器人端：去掉控制字符、连续空白折成一个空格
            text = re.sub(r'\s+', ' ', ''.join(ch for ch in str(body.get('text') or '') if ch >= ' ' or ch in '\t\n')).strip()
            if not text:
                return fail(400, 'text is empty')
            if len(text) > 300:
                return fail(400, 'text too long (>300)')
            lang = body.get('lang')
            if lang not in (None, '', 'zh', 'en'):
                return fail(400, 'lang must be zh or en')
            vol, e = parse_volume(body.get('volume'))
            if e:
                return e
            if not robot.tts_available:
                return fail(503, 'tts engine or audio player unavailable')
            st, data = robot.tts_enqueue('text', text=text, lang=lang or None, volume=vol,
                                         interrupt=truthy(body.get('interrupt', False)), wait=truthy(body.get('wait', False)))
            return finish(st, data)
        return await asyncio.to_thread(idempotent, request, info, do)      # wait=true 会阻塞到 50 s，别占事件循环

    @app.post('/v1/robots/{name}/tts/audio')
    async def tts_audio(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        ct = (request.headers.get('content-type') or '').split(';')[0].strip().lower()
        q = request.query_params
        opts = {k: q.get(k) for k in ('wait', 'volume', 'interrupt', 'format')}
        cl = request.headers.get('content-length')
        if cl and cl.isdigit() and int(cl) > AUDIO_MAX_BYTES:
            return fail(413, 'audio too large (>5MB)')                    # Content-Length 阶段就拒，不等传完
        if ct == 'application/json':                                       # ③ JSON + base64（SDK 用这种）
            body = await body_json(request)
            try:
                data = base64.b64decode(str(body.get('audio_b64') or ''), validate=True)
            except (ValueError, binascii.Error):
                return fail(400, 'audio_b64 不是合法 base64')
            opts.update({k: body.get(k) for k in opts if k in body})
        elif ct.startswith('multipart/form-data'):                         # ② multipart，字段名 file
            form = await request.form()
            up = form.get('file')
            if up is None or not hasattr(up, 'read'):
                return fail(400, 'multipart 缺少 file 字段')
            data = await up.read()
            opts.update({k: form.get(k) for k in opts if k in form})
        else:                                                              # ① 文件字节直接当请求体
            data = await request.body()
            if not opts['format']:
                opts['format'] = AUDIO_CT.get(ct)
        if len(data) > AUDIO_MAX_BYTES:
            return fail(413, 'audio too large (>5MB)')
        declared = str(opts['format'] or '').lower().lstrip('.')
        fmt = sniff_audio(data) or (declared if declared in AUDIO_FORMATS else None)   # 文件头优先于声明；声明也得是认识的格式

        def do() -> JSONResponse:
            g = tts_guard(name, request, info)
            if g:
                return g
            if not data or not fmt:
                return fail(400, 'unrecognized audio format; pass format=…')
            vol, e = parse_volume(opts['volume'])
            if e:
                return e
            if not robot.tts_available:
                return fail(503, 'tts engine or audio player unavailable')
            if fmt != 'wav' and not robot.tts_ffmpeg:
                return fail(503, 'ffmpeg missing on robot; only wav can be played')
            st, d = robot.tts_enqueue('audio', audio=data, fmt=fmt, volume=vol,
                                      interrupt=truthy(opts['interrupt'] or False), wait=truthy(opts['wait'] or False))
            return finish(st, d)
        return await asyncio.to_thread(idempotent, request, info, do)

    @app.get('/v1/robots/{name}/tts')
    def tts_get(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        return tts_guard(name, request, info, write=False) or ok(robot.tts_status())   # viewer 即可

    @app.delete('/v1/robots/{name}/tts')
    def tts_delete(name: str, request: Request):
        info, err = auth(request)
        if err:
            return err
        return idempotent(request, info, lambda: tts_guard(name, request, info) or ok(robot.tts_stop()))

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
        if b.get('kind') == 'freeze_telemetry':
            with robot.lock:
                robot.freeze_telemetry_at = float(b.get('at') or time.time()) if b.get('on', True) else 0.0
            return {'ok': True, 'freeze_telemetry_at': robot.freeze_telemetry_at}
        if b.get('kind') == 'robot_version':
            with robot.lock:
                robot.robot_version = 'new' if b.get('version') == 'new' else 'legacy'
            return {'ok': True, 'robot_version': robot.robot_version}
        if b.get('kind') == 'relocalizing':
            return robot.set_relocalizing(bool(b.get('on', True)))
        if b.get('kind') == 'ignore_stop':
            with robot.lock:
                robot.ignore_stop = bool(b.get('on', True))
            return {'ok': True, 'ignore_stop': robot.ignore_stop}
        if b.get('kind') in ('tts_firmware_old', 'tts_unavailable', 'tts_no_ffmpeg'):
            with robot.lock:
                on = bool(b.get('on', True))
                if b['kind'] == 'tts_firmware_old':
                    robot.tts_firmware_old = on
                elif b['kind'] == 'tts_unavailable':
                    robot.tts_available = not on
                else:
                    robot.tts_ffmpeg = not on
            return {'ok': True, 'tts_firmware_old': robot.tts_firmware_old, 'tts_available': robot.tts_available,
                    'tts_ffmpeg': robot.tts_ffmpeg}
        if b.get('kind') == 'html502':
            with robot.lock:
                robot.html502_left = int(b.get('count') or 1)
                robot.html502_only_task = bool(b.get('only_task', False))
            return {'ok': True, 'html502_left': robot.html502_left, 'only_task': robot.html502_only_task}
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

    @app.post('/mock/relocalizing')
    async def mock_relocalizing(request: Request):
        b = await body_json(request)
        return robot.set_relocalizing(bool(b.get('on', True)))

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
