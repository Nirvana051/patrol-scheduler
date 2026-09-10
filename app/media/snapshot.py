# -*- coding: utf-8 -*-
"""抓一帧全景的几种来源。真机用 RTSP + ffmpeg（必须 TCP，C12）；开发用合成图；也可从文件/目录读。"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from app.media import pano


class SnapshotError(RuntimeError):
    pass


# ffmpeg 解出**残缺画面**时会往 stderr 写这些，但**退出码仍是 0、图片照样输出**。
# 实测（构造一段从 GOP 中间接入的 H.264）：退出码 0、输出 24 KB、stderr 104 行全是这类。
# 原来的判断只看 `returncode != 0 or not stdout`，所以坏图会被当成正常结果送去判读。
DECODE_ERROR_PATTERNS = re.compile(
    r'concealing|error while decoding|no frame!|non-existing (?:PPS|SPS)|decode_slice_header'
    r'|Frame num gap|illegal (?:short|reordering)|corrupt|missing picture|mmco',
    re.I)

# 画面体检的默认阈值。指标是**纵向梯度**（相邻行的平均绝对差）：
# 直播流在刷新周期中间被抓下来时，画面是一条条上下恒定的竖带 → 纵向梯度趋近 0。
# 实测本机真机全景：正常 2.58–3.84；损坏的那张 0.17；合成「每列取均值」是 0.00。
# 注意**合成/仿真场景（Gazebo 空白灰墙）本来就只有 0.38 左右**，所以这项只对 ffmpeg 抓的
# 真实流生效（rtsp/hls），synthetic/file/lavfi 源不做体检。
DEFAULT_MIN_DETAIL = 1.0
DEFAULT_MAX_ATTEMPTS = 3


def frame_detail(data: bytes) -> float:
    """画面的纵向细节度：越接近 0 越可能是「抓到了没收敛的画面」。解不开就返回 -1（交给调用方判断）。"""
    try:
        a = np.asarray(Image.open(io.BytesIO(data)).convert('L'), dtype=np.float32)
    except Exception:      # noqa: BLE001
        return -1.0
    if a.ndim != 2 or a.shape[0] < 2:
        return -1.0
    return float(np.abs(np.diff(a, axis=0)).mean())


def _ffmpeg_grab_checked(cmd: list[str], *, timeout: float, label: str, url: str,
                         attempts: int, min_detail: float, context: dict | None) -> bytes:
    """跑 ffmpeg 抓一帧，并做两道体检；不合格就重抓。

    体检一：stderr 里有没有解码错误特征（ffmpeg 退出码骗不了人，stderr 才说实话）。
    体检二：画面纵向细节度是否足够（挡住 ffmpeg 不报错、但画面其实没收敛的情形）。

    重抓仍不合格时**不抛异常**：把最后一帧照常返回，但在 `context['quality']` 里写明
    `ok=False` 与原因 —— 由调用方决定怎么处理（执行器会跳过 VLM 判读，
    既不拿糊图去问模型、也不白花一次付费调用，同时把图留下来给人看）。
    """
    attempts = max(1, int(attempts))
    last: bytes | None = None
    info: dict = {'attempts': 0, 'ok': False, 'detail': None, 'reason': None, 'min_detail': min_detail}
    for i in range(attempts):
        info['attempts'] = i + 1
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise SnapshotError(f'{label}抓帧超时（{timeout}s）：{url}') from e
        err = r.stderr.decode('utf-8', 'replace')
        if r.returncode != 0 or not r.stdout:
            info['reason'] = f'ffmpeg 失败: {err[:200]}'
            if i + 1 < attempts:
                time.sleep(0.4)
                continue
            raise SnapshotError(f'{label}抓帧失败: {err[:300]}')
        last = r.stdout
        hit = DECODE_ERROR_PATTERNS.search(err)
        if hit:
            info['reason'] = f'解码报错（{hit.group(0)}）'
            info['detail'] = frame_detail(last)
            if i + 1 < attempts:
                time.sleep(0.4)
                continue
            break
        detail = frame_detail(last)
        info['detail'] = detail
        if min_detail > 0 and 0 <= detail < min_detail:
            info['reason'] = f'画面没收敛（纵向细节 {detail:.2f} < {min_detail:.2f}）'
            if i + 1 < attempts:
                time.sleep(0.4)
                continue
            break
        info['ok'] = True
        info['reason'] = None
        break
    if context is not None:
        context['quality'] = info
    if last is None:      # 理论上到不了这里（前面已经 raise 过）
        raise SnapshotError(f'{label}抓帧失败：没有拿到任何画面')
    return last


class SnapshotSource:
    name = 'base'

    def grab(self, context: dict | None = None) -> bytes:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class RtspFfmpegSource(SnapshotSource):
    """从 RTSP 抓一帧。与 SDK snapshot() 同一条 ffmpeg 命令，只是输出到管道而不是文件。"""
    name = 'rtsp'

    def __init__(self, url_provider: Callable[[], str], *, timeout: float = 40.0,
                 attempts: int = DEFAULT_MAX_ATTEMPTS, min_detail: float = DEFAULT_MIN_DETAIL) -> None:
        self.url_provider = url_provider
        self.timeout = timeout
        self.attempts = attempts
        self.min_detail = min_detail

    def grab(self, context: dict | None = None) -> bytes:
        if not shutil.which('ffmpeg'):
            raise SnapshotError('本机没有 ffmpeg，无法抓帧')
        url = self.url_provider()
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp',
               '-i', url, '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-']
        return _ffmpeg_grab_checked(cmd, timeout=self.timeout, label='', url=url,
                                    attempts=self.attempts, min_detail=self.min_detail, context=context)

    def describe(self) -> str:
        try:
            return f'rtsp {self.url_provider()}'
        except Exception:      # noqa: BLE001
            return 'rtsp (地址未知)'


class HlsFfmpegSource(SnapshotSource):
    """从 HLS 播放列表抓一帧（8554 被防火墙挡时的备选，延迟比 RTSP 高 2–3 s）。"""
    name = 'hls'

    def __init__(self, url_provider: Callable[[], str], *, timeout: float = 40.0,
                 attempts: int = DEFAULT_MAX_ATTEMPTS, min_detail: float = DEFAULT_MIN_DETAIL) -> None:
        self.url_provider = url_provider
        self.timeout = timeout
        self.attempts = attempts
        self.min_detail = min_detail

    def grab(self, context: dict | None = None) -> bytes:
        if not shutil.which('ffmpeg'):
            raise SnapshotError('本机没有 ffmpeg，无法抓帧')
        url = self.url_provider()
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', url, '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-']
        return _ffmpeg_grab_checked(cmd, timeout=self.timeout, label='HLS ', url=url,
                                    attempts=self.attempts, min_detail=self.min_detail, context=context)

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
                 scene_provider: Callable[[], dict] | None = None,
                 attempts: int = DEFAULT_MAX_ATTEMPTS,
                 min_detail: float = DEFAULT_MIN_DETAIL) -> SnapshotSource:
    spec = (spec or 'synthetic').strip()
    qc = {'attempts': attempts, 'min_detail': min_detail}      # 只有 ffmpeg 抓真实流的源才体检
    if spec == 'rtsp':
        if rtsp_url_provider is None:
            raise SnapshotError('rtsp 源需要 rtsp_url_provider')
        return RtspFfmpegSource(rtsp_url_provider, **qc)
    if spec == 'hls':
        if hls_url_provider is None:
            raise SnapshotError('hls 源需要 hls_url_provider')
        return HlsFfmpegSource(hls_url_provider, **qc)
    if spec.startswith('http://') or spec.startswith('https://'):
        return HlsFfmpegSource(lambda: spec, **qc)
    if spec.startswith('rtsp://'):
        return RtspFfmpegSource(lambda: spec, **qc)
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
