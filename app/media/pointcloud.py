# -*- coding: utf-8 -*-
"""点云读取与体素下采样（接口预留）。

云端 API 目前不暴露地图点云下载（定位响应里只有机器人本地的 pcd_path），
所以点云先靠手工上传；等接口出来只需在 PointCloudProvider.fetch_from_robot 里补实现。
支持 PCD（ascii / binary，未压缩）与 PLY（ascii / binary_little_endian）的 x,y,z 字段。
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

_PCD_TYPES = {('F', 4): 'f4', ('F', 8): 'f8', ('I', 1): 'i1', ('I', 2): 'i2', ('I', 4): 'i4',
              ('U', 1): 'u1', ('U', 2): 'u2', ('U', 4): 'u4'}


class PointCloudError(RuntimeError):
    pass


def read_pcd(path: str | Path) -> np.ndarray:
    raw = Path(path).read_bytes()
    header, body_start = {}, 0
    lines = raw.split(b'\n')
    offset = 0
    for ln in lines:
        offset += len(ln) + 1
        s = ln.decode('ascii', 'replace').strip()
        if not s or s.startswith('#'):
            continue
        k, _, v = s.partition(' ')
        header[k.upper()] = v.split()
        if k.upper() == 'DATA':
            body_start = offset
            break
    if 'FIELDS' not in header or 'DATA' not in header:
        raise PointCloudError('不是合法的 PCD 文件（缺 FIELDS/DATA）')
    fields = header['FIELDS']
    sizes = [int(s) for s in header.get('SIZE', ['4'] * len(fields))]
    types = header.get('TYPE', ['F'] * len(fields))
    counts = [int(c) for c in header.get('COUNT', ['1'] * len(fields))]
    n = int(header.get('POINTS', header.get('WIDTH', ['0']))[0])
    mode = header['DATA'][0].lower()
    if mode == 'ascii':
        txt = raw[body_start:].decode('ascii', 'replace')
        arr = np.array([[float(v) for v in ln.split()[:len(fields)]] for ln in txt.splitlines() if ln.strip()], dtype='f8')
        idx = [fields.index(k) for k in ('x', 'y', 'z')]
        return arr[:, idx].astype('f4')
    if mode == 'binary':
        dt = []
        for f, s, t, c in zip(fields, sizes, types, counts):
            code = _PCD_TYPES.get((t, s), f'V{s}')
            dt.append((f, code, (c,)) if c > 1 else (f, code))
        a = np.frombuffer(raw[body_start:body_start + n * np.dtype(dt).itemsize], dtype=np.dtype(dt))
        return np.stack([a['x'], a['y'], a['z']], axis=1).astype('f4')
    raise PointCloudError(f'不支持的 PCD DATA 类型: {mode}（compressed 需先解压）')


def read_ply(path: str | Path) -> np.ndarray:
    raw = Path(path).read_bytes()
    end = raw.find(b'end_header')
    if end < 0:
        raise PointCloudError('不是合法的 PLY 文件')
    header = raw[:end].decode('ascii', 'replace').splitlines()
    body = raw[raw.find(b'\n', end) + 1:]
    fmt, n, props = 'ascii', 0, []
    in_vertex = False
    for ln in header:
        parts = ln.split()
        if not parts:
            continue
        if parts[0] == 'format':
            fmt = parts[1]
        elif parts[0] == 'element':
            in_vertex = parts[1] == 'vertex'
            if in_vertex:
                n = int(parts[2])
        elif parts[0] == 'property' and in_vertex:
            props.append((parts[-1], parts[1]))
    names = [p[0] for p in props]
    if fmt == 'ascii':
        rows = [ln.split() for ln in body.decode('ascii', 'replace').splitlines()[:n] if ln.strip()]
        arr = np.array([[float(r[names.index(k)]) for k in ('x', 'y', 'z')] for r in rows], dtype='f4')
        return arr
    if fmt == 'binary_little_endian':
        m = {'float': 'f4', 'float32': 'f4', 'double': 'f8', 'uchar': 'u1', 'uint8': 'u1', 'char': 'i1',
             'short': 'i2', 'ushort': 'u2', 'int': 'i4', 'uint': 'u4', 'int32': 'i4', 'uint32': 'u4'}
        dt = np.dtype([(nm, '<' + m.get(tp, 'f4')) for nm, tp in props])
        a = np.frombuffer(body[:n * dt.itemsize], dtype=dt)
        return np.stack([a['x'], a['y'], a['z']], axis=1).astype('f4')
    raise PointCloudError(f'不支持的 PLY 格式: {fmt}')


def read_pointcloud(path: str | Path) -> np.ndarray:
    p = Path(path)
    if p.suffix.lower() == '.pcd':
        return read_pcd(p)
    if p.suffix.lower() == '.ply':
        return read_ply(p)
    raise PointCloudError(f'不支持的点云格式: {p.suffix}')


def voxel_downsample(points: np.ndarray, voxel: float) -> np.ndarray:
    """体素栅格下采样：每个体素保留其中点的质心。"""
    if len(points) == 0 or voxel <= 0:
        return points
    keys = np.floor(points / voxel).astype(np.int64)
    _, inv, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    sums = np.zeros((len(counts), 3), dtype='f8')
    np.add.at(sums, inv, points.astype('f8'))
    return (sums / counts[:, None]).astype('f4')


class PointCloudProvider:
    def __init__(self, store_dir: Path) -> None:
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, np.ndarray] = {}

    def path_for(self, map_name: str) -> Path | None:
        for ext in ('.pcd', '.ply'):
            p = self.store_dir / f'{map_name}{ext}'
            if p.exists():
                return p
        return None

    def save_upload(self, map_name: str, data: bytes, ext: str) -> Path:
        ext = ext.lower() if ext.lower() in ('.pcd', '.ply') else '.pcd'
        for old in ('.pcd', '.ply'):
            op = self.store_dir / f'{map_name}{old}'
            if op.exists():
                op.unlink()
        p = self.store_dir / f'{map_name}{ext}'
        p.write_bytes(data)
        self._cache.pop(map_name, None)
        read_pointcloud(p)          # 立刻校验可读
        return p

    def load(self, map_name: str) -> np.ndarray | None:
        if map_name in self._cache:
            return self._cache[map_name]
        p = self.path_for(map_name)
        if p is None:
            return None
        pts = read_pointcloud(p)
        self._cache[map_name] = pts
        return pts

    def downsampled(self, map_name: str, voxel: float = 0.2, max_points: int = 60000,
                    z_range: tuple[float, float] | None = None) -> dict | None:
        pts = self.load(map_name)
        if pts is None:
            return None
        src = len(pts)
        if z_range:
            pts = pts[(pts[:, 2] >= z_range[0]) & (pts[:, 2] <= z_range[1])]
        ds = voxel_downsample(pts, voxel)
        while len(ds) > max_points:
            voxel *= 1.5
            ds = voxel_downsample(pts, voxel)
        return {'map_name': map_name, 'voxel': voxel, 'source_count': int(src), 'count': int(len(ds)),
                'points': np.round(ds, 3).tolist()}

    def fetch_from_robot(self, map_name: str) -> Path:
        raise PointCloudError('云端 API 尚未暴露地图点云下载，请先手工上传（docs/TODO.md T4）')
