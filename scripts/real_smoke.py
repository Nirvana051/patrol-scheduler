#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机只读冒烟：拿到 CX_KEY 后第一件事。只读，不会让机器人动。

    CX_HOST=https://certaintyx.sg:8443 CX_ROBOT=ntu-dog-00001 CX_KEY=cx_... .venv/bin/python scripts/real_smoke.py
    # 或先写好 config/.env 再直接跑

逐项核对本系统依赖的契约点（编号见 docs/task.md §1），任何一项不符都会标出来。
接下来再跑 Sample_web_api/examples/python/04_verify_flow.py（会让机器人动）做完整对照。
"""
from __future__ import annotations

import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.robot.client import RobotError, RobotGateway  # noqa: E402

passed = failed = 0


def ok(cond, msg, extra=''):
    global passed, failed
    if cond:
        passed += 1
        print(f'  ✅ {msg}')
    else:
        failed += 1
        print(f'  ❌ {msg}' + (f'  → {extra}' if extra else ''))


def main() -> int:
    cfg = Config()
    host, robot, key = cfg.get('CX_HOST'), cfg.get('CX_ROBOT'), cfg.get('CX_KEY')
    print(f'云端 {host}  机器人 {robot}  密钥 {key[:11]}…  模式 {cfg.mode}')
    if cfg.mode == 'mock':
        print('  （当前指向本机 mock；真机请设置 CX_HOST/CX_ROBOT/CX_KEY）')
    g = RobotGateway(host, robot, key, rps=3.0)

    print('\n① 免鉴权端点')
    try:
        codes = g.status_codes()
        ok('status' in codes and 'errorCode' in codes, f"GET /v1/status-codes 可取（{len(codes['status']['values'])} 个状态码）")
        ok(3 in {v['code'] for v in codes['status']['values']} and 255 in {v['code'] for v in codes['status']['values']}, '状态码含 3(NAVIGATING) 与 255(PAUSED)')
    except Exception as e:      # noqa: BLE001
        ok(False, 'GET /v1/status-codes', str(e))

    print('\n② 概览（鉴权 + 别名）')
    try:
        info = g.info()
        ok(info.get('alias') == robot or info.get('robotId') == robot, f"别名/ID 解析正确：alias={info.get('alias')} robotId={info.get('robotId')}")
        ok(info.get('online') is True, f"在线 online={info.get('online')}", '机器人不在线：后面每一步都会 502')
        ok(bool(info.get('rtspPath')), f"rtspPath={info.get('rtspPath')}（全景流路径，不硬编码）")
        print(f"     lease={info.get('lease')}  location={info.get('location')}  agent={info.get('agentVersion')}")
    except RobotError as e:
        ok(False, f'GET /v1/robots/{robot}', f'HTTP {e.status}: {e}')
        if e.status == 401:
            print('     401 不区分密钥不存在/吊销/过期 —— 先核对 CX_KEY')
        return 1

    print('\n③ 遥测（隐性状态）')
    try:
        t = g.telemetry()
        ok('telemetry' in t, '嵌套是 data.telemetry.<话题>')
        ok(t.get('ros_available') is not False, f"ros_available={t.get('ros_available')}", 'false 时读到的都是陈旧值')
        ok(t.get('emergency_active') is not True, f"emergency_active={t.get('emergency_active')}", 'true 时巡检指令会和急停竞争，先取消急停')
        gl = (t.get('telemetry') or {}).get('global_localization') or {}
        if gl.get('received'):
            age = time.time() - float(gl.get('received_at') or 0)
            ok(age < 3600, f'定位话题 received_at 年龄 {age:.1f}s（用 received_at 而不是 stamp）')
        else:
            print('     定位话题从未收到数据：还没做 ②③④ 初始化，属正常')
    except RobotError as e:
        ok(False, 'GET /telemetry', str(e))

    print('\n④ 位姿 / 感知')
    try:
        p = g.position()
        ok('x' in p and 'yaw' in p, f"/position 200：x={p['x']:.2f} y={p['y']:.2f} yaw={p['yaw']:.2f}（定位就绪）")
    except RobotError as e:
        ok(e.status == 503, f'/position 返回 {e.status}（503=未定位，属正常；其他码要看）', str(e))
    try:
        pc = g.perception()
        ok('Location' in pc and 'ObsState' in pc, f"/perception Location={pc.get('Location')} ObsState={pc.get('ObsState')}")
        print(f"     注意：Location 已知恒为 1，本系统不用它判断定位；location_valid={pc.get('location_valid')}")
    except RobotError as e:
        ok(False, 'GET /perception', str(e))

    print('\n⑤ 地图与航点')
    try:
        maps = g.maps()
        ok(bool(maps), f'地图 {maps}')
        for m in maps[:1]:
            w = g.waypoints(m)
            ids = list(w)
            ok(len(w) >= 2, f'{m}: {len(w)} 个航点，键顺序前 5 = {ids[:5]}（字典序）')
            first = w[ids[0]]
            ok('neighbors' in first and 'orientation' in first.get('pose', {}), '航点含 neighbors 与四元数 orientation')
    except RobotError as e:
        ok(False, '地图/航点', str(e))

    print('\n⑥ 任务状态语义字段')
    try:
        t = g.task()
        ok('active' in t and 'terminal' in t and 'status_name' in t, f"云端附带 active/terminal/status_name：{t.get('status')} → {t.get('status_name')} active={t.get('active')}")
        ok('error_hex' in t and 'progress' in t, f"error_hex={t.get('error_hex')} progress={t.get('progress')}")
    except RobotError as e:
        ok(False, 'GET /task', str(e))

    print('\n⑦ 事件流')
    try:
        ev = g.events()
        ok('seq' in ev and 'nextSince' in ev, f"GET /events 不带 since：seq={ev.get('seq')}（游标起点）")
        url = f"{host}/v1/robots/{urllib.parse.quote(robot)}/events?since={ev.get('seq')}&stream=1"
        req = urllib.request.Request(url, headers={'X-API-Key': key})
        with urllib.request.urlopen(req, timeout=15) as r:
            first = r.readline().decode('utf-8', 'replace').strip()
            ok(r.headers.get('content-type', '').startswith('text/event-stream') and first.startswith(': connected'), f'SSE 可连：{first}')
    except Exception as e:      # noqa: BLE001
        ok(False, '事件流', str(e))

    print('\n⑧ 全景流')
    try:
        hls = g.hls_url()
        req = urllib.request.Request(hls, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read(2000).decode('utf-8', 'replace')
                ok('#EXTM3U' in body, f'HLS 播放列表可取：{hls}')
                res = [l for l in body.splitlines() if 'RESOLUTION' in l]
                if res:
                    print(f'     {res[0][:100]}')
        except urllib.error.HTTPError as e:
            ok(False, f'HLS {hls}', f'HTTP {e.code}（404 = 云端没有这一路的发布者：相机/推流未起）')
        print(f'     RTSP: {g.rtsp_url()}（抓帧必须 -rtsp_transport tcp）')
    except Exception as e:      # noqa: BLE001
        ok(False, '全景流地址', str(e))

    print(f'\n{passed} 项通过 / {failed} 项不符。限速计数 {g.limiter.total} 次请求。')
    print('下一步：Sample_web_api/examples/python/04_verify_flow.py（会让机器人动）；然后在网页「总览」做初始化并跑一个单航点任务。')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
