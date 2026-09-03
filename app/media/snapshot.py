# -*- coding: utf-8 -*-
"""抓一帧全景的几种来源。真机用 RTSP + ffmpeg（必须 TCP，C12）；开发用合成图；也可从文件/目录读。"""
from __future__ import annotations

import io
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from app.media import pano


class SnapshotError(RuntimeError):
    pass


class SnapshotSource:
    name = 'base'

    def grab(self, context: dict | None = None) -> bytes:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class RtspFfmpegSource(SnapshotSource):
    """从 RTSP 抓一帧。与 SDK snapshot() 同一条 ffmpeg 命令，只是输出到管道而不是文件。"""
    name = 'rtsp'

    def __init__(self, url_provider: Callable[[], str], *, timeout: float = 40.0) -> None:
        self.url_provider = url_provider
        self.timeout = timeout

    def grab(self, context: dict | None = None) -> bytes:
        if not shutil.which('ffmpeg'):
            raise SnapshotError('本机没有 ffmpeg，无法抓帧')
        url = self.url_provider()
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp',
               '-i', url, '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-']
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise SnapshotError(f'抓帧超时（{self.timeout}s）：{url}') from e
        if r.returncode != 0 or not r.stdout:
            raise SnapshotError(f"抓帧失败: {r.stderr.decode('utf-8', 'replace')[:300]}")
        return r.stdout

    def describe(self) -> str:
        try:
            return f'rtsp {self.url_provider()}'
        except Exception:      # noqa: BLE001
            return 'rtsp (地址未知)'


class HlsFfmpegSource(SnapshotSource):
    """从 HLS 播放列表抓一帧（8554 被防火墙挡时的备选，延迟比 RTSP 高 2–3 s）。"""
    name = 'hls'

    def __init__(self, url_provider: Callable[[], str], *, timeout: float = 40.0) -> None:
        self.url_provider = url_provider
        self.timeout = timeout

    def grab(self, context: dict | None = None) -> bytes:
        if not shutil.which('ffmpeg'):
            raise SnapshotError('本机没有 ffmpeg，无法抓帧')
        url = self.url_provider()
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', url, '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-']
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise SnapshotError(f'HLS 抓帧超时（{self.timeout}s）：{url}') from e
        if r.returncode != 0 or not r.stdout:
            raise SnapshotError(f"HLS 抓帧失败: {r.stderr.decode('utf-8', 'replace')[:300]}")
        return r.stdout

    def describe(self) -> str:
        try:
            return f'hls {self.url_provider()}'
        except Exception:      # noqa: BLE001
            return 'hls (地址未知)'


class LavfiSource(SnapshotSource):
    """ffmpeg 的合成信号源（testsrc 等），用来验证 ffmpeg 抓帧管道本身。"""
    name = 'lavfi'

    def __init__(self, filt: str = 'testsrc=size=1280x640:rate=1') -> None:
        self.filt = filt

    def grab(self, context: dict | None = None) -> bytes:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', self.filt,
                            '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-'], capture_output=True, timeout=30)
        if r.returncode != 0:
            raise SnapshotError(r.stderr.decode('utf-8', 'replace')[:300])
        return r.stdout


class SyntheticPanoSource(SnapshotSource):
    """合成等距全景：带角度刻度与若干「物体」，位姿从状态快照来。用于 mock 与裁切数学验证。"""
    name = 'synthetic'

    def __init__(self, pose_provider: Callable[[], dict | None] | None = None,
                 scene_provider: Callable[[], dict] | None = None) -> None:
        self.pose_provider = pose_provider
        self.scene_provider = scene_provider

    def grab(self, context: dict | None = None) -> bytes:
        pose = None
        try:
            pose = self.pose_provider() if self.pose_provider else None
        except Exception:      # noqa: BLE001
            pose = None
        scene = {}
        try:
            scene = self.scene_provider() if self.scene_provider else {}
        except Exception:      # noqa: BLE001
            scene = {}
        img = pano.synth_pano(pose=pose, door_open=bool(scene.get('door_open', False)),
                              label=scene.get('label', 'MOCK 全景'))
        return pano.to_jpeg(img)


class FileSource(SnapshotSource):
    """从文件读；给目录则轮换其中的 jpg/png。"""
    name = 'file'

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._i = 0

    def grab(self, context: dict | None = None) -> bytes:
        if self.path.is_dir():
            files = sorted(p for p in self.path.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
            if not files:
                raise SnapshotError(f'目录里没有图片: {self.path}')
            p = files[self._i % len(files)]
            self._i += 1
        else:
            p = self.path
        if not p.exists():
            raise SnapshotError(f'文件不存在: {p}')
        data = p.read_bytes()
        if p.suffix.lower() == '.png':
            data = pano.to_jpeg(Image.open(io.BytesIO(data)))
        return data

    def describe(self) -> str:
        return f'file {self.path}'


def build_source(spec: str, *, rtsp_url_provider: Callable[[], str] | None = None,
                 hls_url_provider: Callable[[], str] | None = None,
                 pose_provider: Callable[[], dict | None] | None = None,
                 scene_provider: Callable[[], dict] | None = None) -> SnapshotSource:
    spec = (spec or 'synthetic').strip()
    if spec == 'rtsp':
        if rtsp_url_provider is None:
            raise SnapshotError('rtsp 源需要 rtsp_url_provider')
        return RtspFfmpegSource(rtsp_url_provider)
    if spec == 'hls':
        if hls_url_provider is None:
            raise SnapshotError('hls 源需要 hls_url_provider')
        return HlsFfmpegSource(hls_url_provider)
    if spec.startswith('http://') or spec.startswith('https://'):
        return HlsFfmpegSource(lambda: spec)
    if spec.startswith('rtsp://'):
        return RtspFfmpegSource(lambda: spec)
    if spec.startswith('file:'):
        return FileSource(spec[5:])
    if spec.startswith('lavfi:'):
        return LavfiSource(spec[6:] or 'testsrc=size=1280x640:rate=1')
    return SyntheticPanoSource(pose_provider, scene_provider)


def save_jpeg(data: bytes, directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f'{stem}.jpg'
    p.write_bytes(data)
    return p


def timestamp_stem(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{int((time.time() % 1) * 1000):03d}"
