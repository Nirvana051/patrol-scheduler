#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向运行中的调度系统灌演示数据（对着 mock 网关）：同步地图 → 3 个任务航点 → 1 个任务；可选初始化机器人并执行。

    .venv/bin/python scripts/seed_demo.py --base http://127.0.0.1:8088 [--init] [--run] [--mock-speed 6]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

MAP = 'map_demo_20260903_220000'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:8088')
    ap.add_argument('--mock', default='http://127.0.0.1:18443')
    ap.add_argument('--init', action='store_true', help='启动设备 + 以航点 1 定位')
    ap.add_argument('--run', action='store_true', help='执行任务并等结束')
    ap.add_argument('--mock-speed', type=float, default=0)
    ap.add_argument('--reset', action='store_true', help='（mock）把仿真机器人复位到航点 1、清空初始化状态')
    a = ap.parse_args()
    B = a.base.rstrip('/')

    def call(method, path, **kw):
        r = requests.request(method, B + path, timeout=90, **kw)
        if r.status_code >= 400:
            raise SystemExit(f'{method} {path} → {r.status_code} {r.text[:300]}')
        return r.json() if r.text else None

    for _ in range(30):
        try:
            requests.get(B + '/api/health', timeout=2).raise_for_status()
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit('调度系统未就绪')

    health = requests.get(B + '/api/health', timeout=5).json()
    if health.get('mode') != 'mock' and (a.init or a.run or a.reset):
        raise SystemExit('当前调度系统连接的是真机（REAL），演示脚本拒绝执行 --init/--run/--reset；请在网页上手工操作。')
    if a.reset:
        requests.post(a.mock.rstrip('/') + '/mock/reset', json={'node_id': '1', 'map_name': MAP}, timeout=5)
        print('mock 机器人已复位到航点 1')
    if a.mock_speed:
        requests.post(a.mock.rstrip('/') + '/mock/speed', json={'speed': a.mock_speed}, timeout=5)

    print('同步地图 …', call('POST', f'/api/maps/{MAP}/sync'))
    pcd = Path(__file__).resolve().parent.parent / 'mock_gateway' / 'fixtures' / 'map_demo_walls.pcd'
    if pcd.exists():
        with pcd.open('rb') as f:
            r = requests.post(f'{B}/api/maps/{MAP}/pointcloud', files={'file': ('map_demo_walls.pcd', f, 'application/octet-stream')}, timeout=120)
        print('上传演示点云 …', r.status_code, r.text[:120])
    existing = {t['name']: t for t in call('GET', f'/api/task-waypoints?map_name={MAP}')['items']}
    specs = [
        {'name': '3 号消防栓', 'nav_node_id': '20', 'angle_from': 190, 'angle_to': 232,
         'prompt': '画面中红色消防栓柜的柜门是否关好（门扇贴合、没有敞开）？',
         'answer_template': {'expected': 'yes', 'on_pass': '3 号消防栓柜门已关闭，检查通过。',
                             'on_fail': '注意：3 号消防栓柜门未关好，请及时处理。', 'on_unknown': '3 号消防栓状态无法判断，请人工复核。'}},
        {'name': '消防通道方格', 'nav_node_id': '30', 'angle_from': 150, 'angle_to': 185,
         'prompt': '地面黄色方格区域内是否有物品堆放或被遮挡？',
         'answer_template': {'expected': 'no', 'on_pass': '消防通道畅通。', 'on_fail': '警告：消防通道被占用，请立即清理。',
                             'on_unknown': '消防通道状态无法判断，请人工复核。'}},
        {'name': '北侧安全出口', 'nav_node_id': '42', 'angle_from': 75, 'angle_to': 110,
         'prompt': '安全出口指示牌是否点亮且清晰可见？',
         'answer_template': {'expected': 'yes', 'on_pass': '北侧安全出口指示正常。', 'on_fail': '注意：北侧安全出口指示牌异常。'}},
    ]
    ids = []
    for s in specs:
        if s['name'] in existing:
            ids.append(existing[s['name']]['id'])
            continue
        r = call('POST', '/api/task-waypoints', json={**s, 'map_name': MAP})
        ids.append(r['id'])
        print(f"任务航点 #{r['id']} {r['name']} @ 航点 {r['nav_node_id']} ({r['x']:.1f},{r['y']:.1f})")
    # 给还没有参考图的任务航点抓一张（合成源），任务航点表与编辑器里就有图可看
    for tw in call('GET', f'/api/task-waypoints?map_name={MAP}')['items']:
        if tw['id'] in ids and not tw.get('reference_image'):
            r = call('POST', f"/api/task-waypoints/{tw['id']}/reference-image")
            print(f"参考图 #{tw['id']} {tw['name']}: {r['width']}x{r['height']}")
    tasks = {t['name']: t for t in call('GET', '/api/tasks')['items']}
    if '一层夜间巡检（演示）' in tasks:
        task = call('GET', f"/api/tasks/{tasks['一层夜间巡检（演示）']['id']}")
    else:
        task = call('POST', '/api/tasks', json={'name': '一层夜间巡检（演示）', 'map_name': MAP, 'description': '消防栓 → 通道方格 → 安全出口',
                                                 'options': {'settle_seconds': 0.5, 'leg_timeout': 300, 'max_retries': 1, 'return_to_start': True},
                                                 'waypoint_ids': ids})
    print(f"任务 #{task['id']} {task['name']}，{len(task['items'])} 个航点")
    plan = call('GET', f"/api/tasks/{task['id']}/plan")
    print('路线预览:', plan['start_node'], [(l['seq'], l['to_node'], l['length']) for l in plan['legs']], '总长', plan['total_length'])

    if a.init:
        print('启动设备 …'); print(call('POST', '/api/robot/init/device-start', json={'wait': True}))
        print('定位（航点 1）…'); print(call('POST', '/api/robot/init/localize', json={'map_name': MAP, 'node_id': '1'}))
    if a.run:
        run = call('POST', f"/api/tasks/{task['id']}/run")
        print(f"执行 #{run['id']} 开始")
        t0 = time.time()
        while time.time() - t0 < 600:
            r = call('GET', f"/api/runs/{run['id']}")
            legs = ' '.join(f"{l['seq']}:{l['status']}" for l in r['legs'])
            print(f"  [{time.time() - t0:5.1f}s] {r['status']:<10} {legs}")
            if r['status'] in ('completed', 'failed', 'aborted'):
                print('结果:', r['status'], r.get('error'), r['summary'])
                for i in r['inspections']:
                    print(f"  检查 {i['waypoint_name']}: {i['answer']} passed={i['passed']} tts={i['tts_text']!r} vlm={i['vlm_provider']} {i['latency_ms']}ms")
                return 0 if r['status'] == 'completed' else 1
            time.sleep(2)
        print('等待超时'); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
