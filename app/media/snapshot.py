# -*- coding: utf-8 -*-
"""抓一帧全景的几种来源。真机用 RTSP + ffmpeg（必须 TCP，C12）；开发用合成图；也可从文件/目录读。"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app import platform
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


def _ffmpeg() -> str:
    """ffmpeg 可执行文件：runtime 自带的优先，其次 PATH（见 app/platform.py）。没有就抛 SnapshotError。"""
    exe = platform.ffmpeg_exe()
    if not exe:
        raise SnapshotError('本机没有 ffmpeg，无法抓帧')
    return exe


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
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, **platform.SUBPROCESS_KW)
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

    def warm(self) -> None:
        """「马上要抓帧了」的预告（执行器在下发下一段时调）。默认什么都不做；常驻读流器用它提前连流。"""

    def stop(self) -> None:
        """释放后台资源（换抓图源 / 进程退出时调）。默认什么都不做。"""


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
        ffmpeg = _ffmpeg()
        url = self.url_provider()
        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp',
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
        ffmpeg = _ffmpeg()
        url = self.url_provider()
        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', url, '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-']
        return _ffmpeg_grab_checked(cmd, timeout=self.timeout, label='HLS ', url=url,
                                    attempts=self.attempts, min_detail=self.min_detail, context=context)

    def describe(self) -> str:
        try:
            return f'hls {self.url_provider()}'
        except Exception:      # noqa: BLE001
            return 'hls (地址未知)'


class LiveStreamSource(SnapshotSource):
    """常驻读流器：给 rtsp / hls / http 源套一层，让「到点抓帧」从 1–3.4 s 变成 ≤ 0.5 s 且不抖。

    一次性起 ffmpeg 抓一帧必须等到**下一个关键帧**（真机直播流 GOP ≈ 3 s，2026-09-15 实测 1.0–3.4 s 抖动），
    `-fflags nobuffer` 之类的低延迟参数救不了。这里改成 ffmpeg 常驻解码、每 1/fps 秒吐一帧 JPEG，
    内存里只留最新一帧；执行器在**下发下一段时**调 warm()，机器人走到点时画面早就在手上了。

    * grab()：有 ≤ max_age 秒的新鲜帧就直接用；否则等下一帧（最多 wait 秒）；
      读流器没起来 / 等不到 → **退回一次性抓帧**（原逻辑，含体检），所以永远不会比以前更慢
    * 体检照旧：纵向细节度不够 → 换下一帧（最多 attempts 次）；ffmpeg 刚在 stderr 报过解码错误的
      1 s 内的帧视为可疑 → 直接等干净帧（不计入 attempts）
    * 空闲 idle_seconds 没人 grab / warm 就自动停掉 ffmpeg，别一直占带宽；连续 stall_seconds 没新帧就重连
    * 进程意外退出且仍需要时自动重连（退避 1 → 5 s）
    """
    name = 'live'
    SUSPECT_AFTER_ERROR = 1.0

    def __init__(self, inner: SnapshotSource, input_args_provider: Callable[[], list[str]], *, fps: float = 2.0,
                 max_age: float = 1.5, wait: float = 4.0, idle_seconds: float = 120.0, stall_seconds: float = 15.0,
                 attempts: int = DEFAULT_MAX_ATTEMPTS, min_detail: float = DEFAULT_MIN_DETAIL, qc: bool = True) -> None:
        self.inner, self.input_args_provider = inner, input_args_provider
        self.fps, self.max_age, self.wait = float(fps), float(max_age), float(wait)
        self.idle_seconds, self.stall_seconds = float(idle_seconds), float(stall_seconds)
        self.attempts, self.min_detail, self.qc = max(1, int(attempts)), float(min_detail), bool(qc)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._wanted = False
        self._frame: bytes | None = None
        self._frame_ts = 0.0
        self._frame_seq = 0
        self._last_err_ts = 0.0
        self._last_err = ''
        self._last_use = 0.0
        self._connected_at = 0.0
        self.frames_total = 0
        self.restarts = 0
        self.grabs_live = 0
        self.grabs_fallback = 0

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def warm(self) -> None:
        with self._lock:
            self._last_use = time.time()
            self._wanted = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name='live-stream', daemon=True)
                self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._wanted = False
            proc = self._proc
        self._kill(proc)

    def running(self) -> bool:
        with self._lock:
            return self._wanted and self._proc is not None and self._proc.poll() is None

    def stats(self) -> dict:
        with self._lock:
            return {'running': self._wanted and self._proc is not None and self._proc.poll() is None,
                    'frame_age_s': round(time.time() - self._frame_ts, 2) if self._frame_ts else None,
                    'frames': self.frames_total, 'restarts': self.restarts,
                    'grabs_live': self.grabs_live, 'grabs_fallback': self.grabs_fallback, 'last_error': self._last_err[-120:]}

    @staticmethod
    def _kill(proc) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:      # noqa: BLE001
            pass

    def _loop(self) -> None:
        backoff = 1.0
        while True:
            with self._lock:
                if not self._wanted:
                    return
            try:
                cmd = [_ffmpeg(), '-hide_banner', '-loglevel', 'error', *self.input_args_provider(),
                       '-vf', f'fps={self.fps:g}', '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '2', 'pipe:1']
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **platform.SUBPROCESS_KW)
            except Exception as e:      # noqa: BLE001 —— 没有 ffmpeg / 地址拿不到：等一会再试，grab 会走退回路径
                with self._lock:
                    self._last_err = f'启动 ffmpeg 失败: {e}'
                time.sleep(backoff)
                backoff = min(5.0, backoff * 2)
                continue
            with self._lock:
                self._proc = proc
                self._connected_at = time.time()
            threading.Thread(target=self._pump_stderr, args=(proc,), name='live-stream-err', daemon=True).start()
            threading.Thread(target=self._watchdog, args=(proc,), name='live-stream-dog', daemon=True).start()
            got_any = self._pump_frames(proc)
            self._kill(proc)
            with self._lock:
                self._proc = None
                wanted = self._wanted
            if not wanted:
                return
            self.restarts += 1
            time.sleep(backoff if not got_any else 1.0)
            backoff = 1.0 if got_any else min(5.0, backoff * 2)

    def _pump_frames(self, proc) -> bool:
        """从 stdout 里切出一张张 JPEG（SOI … EOI），只留最新一帧。返回是否收到过帧。"""
        buf = b''
        got_any = False
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b'\xff\xd8')
                e = buf.find(b'\xff\xd9', s + 2) if s >= 0 else -1
                if s < 0 or e < 0:
                    if s > 0:
                        buf = buf[s:]
                    break
                frame, buf = buf[s:e + 2], buf[e + 2:]
                got_any = True
                with self._cond:
                    self._frame, self._frame_ts = frame, time.time()
                    self._frame_seq += 1
                    self.frames_total += 1
                    self._cond.notify_all()
        return got_any

    def _pump_stderr(self, proc) -> None:
        for raw in iter(proc.stderr.readline, b''):
            line = raw.decode('utf-8', 'replace').strip()
            if not line:
                continue
            with self._lock:
                self._last_err = line
                if DECODE_ERROR_PATTERNS.search(line):
                    self._last_err_ts = time.time()

    def _watchdog(self, proc) -> None:
        """空闲太久 → 停；太久没新帧（流卡死）→ 杀掉让主循环重连。"""
        period = max(0.5, min(5.0, self.idle_seconds / 2))
        while proc.poll() is None:
            time.sleep(period)
            with self._lock:
                idle = time.time() - self._last_use > self.idle_seconds
                stalled = (time.time() - max(self._frame_ts, self._connected_at)) > self.stall_seconds
                if idle:
                    self._wanted = False
            if idle or stalled:
                self._kill(proc)
                return

    # ── 抓帧 ────────────────────────────────────────────────────────────────
    def _next_frame(self, *, min_seq: int, min_ts: float, deadline: float):
        with self._cond:
            while True:
                if self._frame is not None and self._frame_seq >= min_seq and self._frame_ts >= min_ts:
                    return self._frame, self._frame_ts, self._frame_seq
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.5))

    def grab(self, context: dict | None = None) -> bytes:
        t0 = time.time()
        with self._lock:
            self._last_use = t0
            alive = self._wanted and self._proc is not None and self._proc.poll() is None
        if not alive:
            self.warm()                                    # 没预热：这次退回一次性抓帧，顺手把读流器起起来
            return self._fallback(context, 'not_running', t0)
        deadline = t0 + self.wait
        info: dict = {'attempts': 0, 'ok': False, 'detail': None, 'reason': None, 'min_detail': self.min_detail}
        min_seq, min_ts = 0, t0 - self.max_age
        last = None
        waited_clean = False
        for i in range(self.attempts):
            with self._lock:
                err_ts = self._last_err_ts
            clean_after = err_ts + self.SUSPECT_AFTER_ERROR
            if self.qc and clean_after > min_ts:
                min_ts, waited_clean = clean_after, True        # 刚报过解码错误：直接等干净帧，不计入 attempts
            got = self._next_frame(min_seq=min_seq, min_ts=min_ts, deadline=deadline)
            if got is None:
                break
            frame, ts, seq = got
            last, min_seq = (frame, ts), seq + 1
            info['attempts'] = i + 1
            if not self.qc:
                info.update(ok=True, reason=None)
                break
            detail = frame_detail(frame)
            info['detail'] = detail
            if self.min_detail > 0 and 0 <= detail < self.min_detail:
                info['reason'] = f'画面没收敛（纵向细节 {detail:.2f} < {self.min_detail:.2f}）'
                continue
            info.update(ok=True, reason=None)
            break
        if last is None:
            return self._fallback(context, 'no_fresh_frame', t0)
        frame, ts = last
        self.grabs_live += 1
        if context is not None:
            context['quality'] = info if self.qc else {}
            context['grab'] = {'source': 'live', 'age_s': round(time.time() - ts, 2), 'ms': int((time.time() - t0) * 1000),
                               'waited_for_clean': waited_clean}
        return frame

    def _fallback(self, context: dict | None, reason: str, t0: float) -> bytes:
        self.grabs_fallback += 1
        data = self.inner.grab(context)
        if context is not None:
            context['grab'] = {'source': 'oneshot', 'reason': reason, 'ms': int((time.time() - t0) * 1000)}
        return data

    def describe(self) -> str:
        return f'{self.inner.describe()} +live({"读流中" if self.running() else "空闲"})'


class LavfiSource(SnapshotSource):
    """ffmpeg 的合成信号源（testsrc 等），用来验证 ffmpeg 抓帧管道本身。"""
    name = 'lavfi'

    def __init__(self, filt: str = 'testsrc=size=1280x640:rate=1') -> None:
        self.filt = filt

    def grab(self, context: dict | None = None) -> bytes:
        r = subprocess.run([_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', self.filt,
                            '-frames:v', '1', '-f', 'image2', '-q:v', '2', '-'], capture_output=True, timeout=30, **platform.SUBPROCESS_KW)
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
                 min_detail: float = DEFAULT_MIN_DETAIL,
                 keepalive: bool = False, keepalive_idle: float = 120.0) -> SnapshotSource:
    spec = (spec or 'synthetic').strip()
    qc = {'attempts': attempts, 'min_detail': min_detail}      # 只有 ffmpeg 抓真实流的源才体检

    def live(inner: SnapshotSource, input_args: Callable[[], list[str]], *, do_qc: bool = True) -> SnapshotSource:
        # 常驻读流只对 ffmpeg 读真实流的源有意义；synthetic / file 本来就是瞬时的
        if not keepalive:
            return inner
        return LiveStreamSource(inner, input_args, idle_seconds=keepalive_idle, qc=do_qc, **qc)

    if spec == 'rtsp':
        if rtsp_url_provider is None:
            raise SnapshotError('rtsp 源需要 rtsp_url_provider')
        return live(RtspFfmpegSource(rtsp_url_provider, **qc), lambda: ['-rtsp_transport', 'tcp', '-i', rtsp_url_provider()])
    if spec == 'hls':
        if hls_url_provider is None:
            raise SnapshotError('hls 源需要 hls_url_provider')
        return live(HlsFfmpegSource(hls_url_provider, **qc), lambda: ['-fflags', 'nobuffer', '-i', hls_url_provider()])
    if spec.startswith('http://') or spec.startswith('https://'):
        return live(HlsFfmpegSource(lambda: spec, **qc), lambda: ['-fflags', 'nobuffer', '-i', spec])
    if spec.startswith('rtsp://'):
        return live(RtspFfmpegSource(lambda: spec, **qc), lambda: ['-rtsp_transport', 'tcp', '-i', spec])
    if spec.startswith('file:'):
        return FileSource(spec[5:])
    if spec.startswith('lavfi:'):
        filt = spec[6:] or 'testsrc=size=1280x640:rate=1'
        return live(LavfiSource(filt), lambda: ['-f', 'lavfi', '-i', filt], do_qc=False)
    return SyntheticPanoSource(pose_provider, scene_provider)


def save_jpeg(data: bytes, directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f'{stem}.jpg'
    p.write_bytes(data)
    return p


def timestamp_stem(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{int((time.time() % 1) * 1000):03d}"
