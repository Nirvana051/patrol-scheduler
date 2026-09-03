# -*- coding: utf-8 -*-
"""导航航点图：四元数→yaw、最近航点、按 neighbors 拓扑的最短路（Dijkstra，权重为欧氏距离）。"""
from __future__ import annotations

import heapq
import math
from typing import Iterable


def quat_to_yaw(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def sort_ids(ids: Iterable[str]) -> list[str]:
    """航点 ID 是字符串，字典序是 1,10,11,…；按编号排要显式转 int。"""
    return sorted(ids, key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k)))


class NavGraph:
    def __init__(self, nodes: dict[str, dict], adj: dict[str, set[str]]) -> None:
        self.nodes = nodes            # id -> {x,y,z,yaw}
        self.adj = adj                # id -> set(id)，已对称化

    @classmethod
    def from_api(cls, waypoints: dict) -> 'NavGraph':
        """输入 GET /maps/{name}/waypoints 的 data。"""
        nodes, adj = {}, {}
        for nid, w in waypoints.items():
            p = w.get('pose', {}).get('position', {})
            q = w.get('pose', {}).get('orientation', {}) or {}
            nodes[str(nid)] = {
                'x': float(p.get('x', 0)), 'y': float(p.get('y', 0)), 'z': float(p.get('z', 0)),
                'yaw': quat_to_yaw(float(q.get('x', 0)), float(q.get('y', 0)), float(q.get('z', 0)), float(q.get('w', 1))),
                'q': {k: float(q.get(k, 0 if k != 'w' else 1)) for k in ('x', 'y', 'z', 'w')},
            }
            adj[str(nid)] = set(str(n) for n in (w.get('neighbors') or []))
        for a, ns in list(adj.items()):
            for b in ns:
                adj.setdefault(b, set()).add(a)
        return cls(nodes, adj)

    @classmethod
    def from_rows(cls, rows: list[dict]) -> 'NavGraph':
        """输入 nav_waypoints 表的行（neighbors 为 JSON 文本或列表）。"""
        import json
        nodes, adj = {}, {}
        for r in rows:
            nid = str(r['node_id'])
            nodes[nid] = {'x': float(r['x']), 'y': float(r['y']), 'z': float(r.get('z') or 0), 'yaw': float(r.get('yaw') or 0),
                          'q': {'x': r.get('qx', 0), 'y': r.get('qy', 0), 'z': r.get('qz', 0), 'w': r.get('qw', 1)}}
            nb = r.get('neighbors') or []
            if isinstance(nb, str):
                nb = json.loads(nb or '[]')
            adj[nid] = set(str(n) for n in nb)
        for a, ns in list(adj.items()):
            for b in ns:
                adj.setdefault(b, set()).add(a)
        return cls(nodes, adj)

    # ── 查询 ────────────────────────────────────────────────────────────────
    def __contains__(self, nid: str) -> bool:
        return str(nid) in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def ids(self) -> list[str]:
        return sort_ids(self.nodes)

    def dist(self, a: str, b: str) -> float:
        pa, pb = self.nodes[a], self.nodes[b]
        return math.hypot(pa['x'] - pb['x'], pa['y'] - pb['y'])

    def nearest(self, x: float, y: float) -> tuple[str | None, float]:
        best, bd = None, float('inf')
        for nid, p in self.nodes.items():
            d = math.hypot(p['x'] - x, p['y'] - y)
            if d < bd:
                best, bd = nid, d
        return best, bd

    def shortest_path(self, a: str, b: str) -> list[str] | None:
        a, b = str(a), str(b)
        if a not in self.nodes or b not in self.nodes:
            return None
        if a == b:
            return [a]
        dist = {a: 0.0}
        prev: dict[str, str] = {}
        pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, float('inf')):
                continue
            for v in self.adj.get(u, ()):
                if v not in self.nodes:
                    continue
                nd = d + self.dist(u, v)
                if nd < dist.get(v, float('inf')):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None
        path = [b]
        while path[-1] != a:
            path.append(prev[path[-1]])
        return path[::-1]

    def path_length(self, path: list[str]) -> float:
        return sum(self.dist(path[i], path[i + 1]) for i in range(len(path) - 1))

    def components(self) -> int:
        seen, n = set(), 0
        for s in self.nodes:
            if s in seen:
                continue
            n += 1
            stack = [s]
            while stack:
                u = stack.pop()
                if u in seen:
                    continue
                seen.add(u)
                stack.extend(v for v in self.adj.get(u, ()) if v not in seen)
        return n
