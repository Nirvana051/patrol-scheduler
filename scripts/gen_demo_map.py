#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 mock 网关的演示地图夹具 mock_gateway/fixtures/map_demo.json。

形状：30m×14m 的环形走廊（间距 ~2.2m）+ 北侧支路 4 点 + 东侧死胡同 3 点，
编号沿行走顺序递增，相邻编号相距 0.5–3m —— 贴近真实 87 点地图「编号相邻 ≠ 位置相邻，
但通常沿路径顺序编」的特征。另附一张 5 点小地图，用来测多地图切换。

输出形状与 GET /maps/{name}/waypoints 完全一致：
    {id: {"neighbors": [...], "pose": {"position": {x,y,z}, "orientation": {x,y,z,w}}}}
键是字符串，序列化时按字典序（"1","10","11"…），和真实网关一样。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / 'mock_gateway' / 'fixtures' / 'map_demo.json'


def quat(yaw: float) -> dict:
    return {'x': 0.0, 'y': 0.0, 'z': round(math.sin(yaw / 2), 6), 'w': round(math.cos(yaw / 2), 6)}


def ring(w: float, h: float, spacing: float) -> list[tuple[float, float]]:
    corners = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    pts = []
    for i in range(4):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        n = int(math.hypot(x1 - x0, y1 - y0) // spacing)
        for k in range(n):
            t = k / n
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return pts


def build_loop_map() -> dict:
    nodes: dict[str, dict] = {}
    loop = ring(30.0, 14.0, 2.2)
    n = len(loop)
    for i, (x, y) in enumerate(loop):
        nx, ny = loop[(i + 1) % n]
        nodes[str(i + 1)] = {'x': x, 'y': y, 'z': 0.0, 'yaw': math.atan2(ny - y, nx - x), 'nb': set()}
    ids = list(nodes)
    for i in range(n):
        a, b = ids[i], ids[(i + 1) % n]
        nodes[a]['nb'].add(b)
        nodes[b]['nb'].add(a)

    def nearest(px, py):
        return min(nodes, key=lambda k: math.hypot(nodes[k]['x'] - px, nodes[k]['y'] - py))

    def chain(anchor, pts):
        prev = anchor
        for x, y in pts:
            nid = str(len(nodes) + 1)
            yaw = math.atan2(y - nodes[prev]['y'], x - nodes[prev]['x'])
            nodes[nid] = {'x': x, 'y': y, 'z': 0.0, 'yaw': yaw, 'nb': {prev}}
            nodes[prev]['nb'].add(nid)
            prev = nid

    chain(nearest(15, 14), [(15.0, 16.0), (15.0, 18.0), (15.0, 20.0), (15.0, 22.0)])   # 北侧支路
    chain(nearest(30, 7), [(32.0, 7.0), (34.0, 7.0), (36.0, 7.0)])                     # 东侧死胡同
    return to_api(nodes)


def build_small_map() -> dict:
    nodes = {}
    for i in range(5):
        nid = str(i + 1)
        nodes[nid] = {'x': 2.0 * i, 'y': 0.0, 'z': 0.0, 'yaw': 0.0, 'nb': set()}
        if i:
            nodes[nid]['nb'].add(str(i))
            nodes[str(i)]['nb'].add(nid)
    return to_api(nodes)


def to_api(nodes: dict) -> dict:
    out = {}
    for nid, v in nodes.items():
        out[nid] = {
            'neighbors': sorted(v['nb'], key=int),
            'pose': {
                'position': {'x': round(v['x'], 6), 'y': round(v['y'], 6), 'z': round(v['z'], 6)},
                'orientation': quat(v['yaw']),
            },
        }
    return out


def main() -> None:
    data = {'maps': {
        'map_demo_20260903_220000': build_loop_map(),
        'map_small_20260901_000000': build_small_map(),
    }}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    for name, wps in data['maps'].items():
        print(f'{name}: {len(wps)} 个航点')


if __name__ == '__main__':
    main()
