#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为演示地图生成一份「走廊墙面」点云（ASCII PCD），用来演示地图页的点云背景与下采样接口。

    .venv/bin/python scripts/gen_demo_pointcloud.py            # 写 mock_gateway/fixtures/map_demo_walls.pcd
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / 'mock_gateway' / 'fixtures'


def main() -> None:
    maps = json.loads((FIX / 'map_demo.json').read_text(encoding='utf-8'))['maps']
    wps = maps['map_demo_20260903_220000']
    rng = np.random.default_rng(7)
    pts = []
    # 走廊：航点两侧 ±1.2 m 各一面墙，沿相邻航点连线采样；墙高 0–2.4 m
    seen = set()
    for nid, w in wps.items():
        p0 = w['pose']['position']
        for nb in w['neighbors']:
            key = tuple(sorted((nid, nb)))
            if key in seen or nb not in wps:
                continue
            seen.add(key)
            p1 = wps[nb]['pose']['position']
            a, b = np.array([p0['x'], p0['y']]), np.array([p1['x'], p1['y']])
            d = b - a
            L = np.linalg.norm(d)
            if L < 1e-6:
                continue
            n = np.array([-d[1], d[0]]) / L
            for t in np.linspace(0, 1, max(2, int(L / 0.08))):
                c = a + d * t
                for side in (-1.2, 1.2):
                    wall = c + n * side
                    for z in np.arange(0.0, 2.4, 0.08):
                        pts.append([wall[0] + rng.normal(0, 0.015), wall[1] + rng.normal(0, 0.015), z + rng.normal(0, 0.01)])
    arr = np.array(pts, dtype='f4')
    hdr = (f'# .PCD v0.7 - demo corridor walls\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n'
           f'WIDTH {len(arr)}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {len(arr)}\nDATA ascii\n')
    out = FIX / 'map_demo_walls.pcd'
    with out.open('w', encoding='ascii') as f:
        f.write(hdr)
        np.savetxt(f, arr, fmt='%.3f')
    print(f'{out}: {len(arr)} 点，{out.stat().st_size / 1e6:.1f} MB')


if __name__ == '__main__':
    main()
