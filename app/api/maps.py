# -*- coding: utf-8 -*-
"""地图与导航航点：从云端同步进缓存表、查询、路径预览、点云上传与下采样。"""
from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile, File

from app.api.deps import bad_request, ctx_of, not_found
from app.db import dumps, loads, now_iso
from app.media.pointcloud import PointCloudError
from app.planning.graph import quat_to_yaw, sort_ids
from app.robot.client import RobotError

router = APIRouter(prefix='/api/maps', tags=['maps'])


@router.get('')
def list_maps(request: Request):
    c = ctx_of(request)
    local = {r['name']: r for r in c.db.query('SELECT * FROM maps')}
    cloud, err = [], None
    try:
        cloud = c.gateway.maps()
    except RobotError as e:
        err = str(e)
    names = list(dict.fromkeys([*cloud, *local]))
    out = []
    for n in names:
        r = local.get(n) or {}
        out.append({'name': n, 'on_cloud': n in cloud, 'synced_at': r.get('synced_at'),
                    'waypoint_count': r.get('waypoint_count', 0),
                    'has_pointcloud': c.pointclouds.path_for(n) is not None})
    return {'maps': out, 'cloud_error': err}


@router.post('/{name}/sync')
def sync_map(name: str, request: Request):
    c = ctx_of(request)
    wps = c.gateway.waypoints(name)
    if not wps:
        raise not_found(f'地图 {name} 没有航点或不存在')
    rows = []
    for nid, w in wps.items():
        p = w.get('pose', {}).get('position', {})
        q = w.get('pose', {}).get('orientation', {}) or {}
        qx, qy, qz, qw = (float(q.get(k, 0 if k != 'w' else 1)) for k in ('x', 'y', 'z', 'w'))
        rows.append((name, str(nid), float(p.get('x', 0)), float(p.get('y', 0)), float(p.get('z', 0)),
                     qx, qy, qz, qw, quat_to_yaw(qx, qy, qz, qw), dumps([str(n) for n in (w.get('neighbors') or [])])))
    with c.db.transaction() as conn:
        conn.execute('DELETE FROM nav_waypoints WHERE map_name=?', (name,))
        conn.executemany('INSERT INTO nav_waypoints(map_name,node_id,x,y,z,qx,qy,qz,qw,yaw,neighbors) VALUES(?,?,?,?,?,?,?,?,?,?,?)', rows)
        conn.execute('INSERT INTO maps(name,synced_at,waypoint_count) VALUES(?,?,?) '
                     'ON CONFLICT(name) DO UPDATE SET synced_at=excluded.synced_at, waypoint_count=excluded.waypoint_count',
                     (name, now_iso(), len(rows)))
    c.invalidate_graph(name)
    c.log_event('map_synced', f'已从云端同步地图 {name} 的 {len(rows)} 个导航航点', data={'map_name': name, 'count': len(rows)})
    return {'name': name, 'count': len(rows)}


@router.get('/{name}/waypoints')
def waypoints(name: str, request: Request):
    c = ctx_of(request)
    rows = c.db.query('SELECT * FROM nav_waypoints WHERE map_name=?', (name,))
    if not rows:
        raise not_found(f'地图 {name} 尚未同步')
    by_id = {r['node_id']: r for r in rows}
    edges, seen = [], set()
    for r in rows:
        r['neighbors'] = loads(r['neighbors'], [])
        for n in r['neighbors']:
            key = tuple(sorted((r['node_id'], n)))
            if key not in seen and n in by_id:
                seen.add(key)
                edges.append([key[0], key[1]])
    ordered = [by_id[i] for i in sort_ids(by_id)]
    xs, ys = [r['x'] for r in rows], [r['y'] for r in rows]
    g = c.graph(name)
    return {'name': name, 'waypoints': ordered, 'edges': edges,
            'bounds': {'min_x': min(xs), 'max_x': max(xs), 'min_y': min(ys), 'max_y': max(ys)},
            'components': g.components() if g else 0}


@router.get('/{name}/route')
def route(name: str, request: Request, from_node: str, to_node: str):
    c = ctx_of(request)
    g = c.graph(name)
    if g is None:
        raise not_found(f'地图 {name} 尚未同步')
    path = g.shortest_path(from_node, to_node)
    if path is None:
        return {'path': None, 'length': None, 'reachable': False}
    return {'path': path, 'length': round(g.path_length(path), 2), 'reachable': True}


@router.get('/{name}/pointcloud')
def pointcloud(name: str, request: Request, voxel: float = 0.2, max_points: int = 60000,
               z_min: float | None = None, z_max: float | None = None):
    c = ctx_of(request)
    try:
        zr = (z_min, z_max) if z_min is not None and z_max is not None else None
        d = c.pointclouds.downsampled(name, voxel=voxel, max_points=max_points, z_range=zr)
    except PointCloudError as e:
        raise bad_request(str(e))
    if d is None:
        raise not_found('该地图没有点云，请先上传')
    return d


@router.post('/{name}/pointcloud')
async def upload_pointcloud(name: str, request: Request, file: UploadFile = File(...)):
    c = ctx_of(request)
    data = await file.read()
    ext = '.' + (file.filename or 'x.pcd').rsplit('.', 1)[-1].lower()
    try:
        p = c.pointclouds.save_upload(name, data, ext)
    except PointCloudError as e:
        raise bad_request(str(e))
    c.db.execute('INSERT INTO maps(name,pointcloud_path) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET pointcloud_path=excluded.pointcloud_path',
                 (name, str(p)))
    pts = c.pointclouds.load(name)
    c.log_event('pointcloud_uploaded', f'地图 {name} 上传点云 {file.filename}（{len(pts)} 点）')
    return {'name': name, 'points': int(len(pts)), 'path': str(p)}
