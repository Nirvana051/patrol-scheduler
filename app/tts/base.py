# -*- coding: utf-8 -*-
"""TTS：引擎（文本→音频文件）与汇出（把播报送到某个「扬声器」）解耦。

汇出是插件：
* robot    —— **机器人自带扬声器**（云端 API 2026-09-14 起提供 POST …/tts 与 …/tts/audio，见
              Sample_web_api/docs/tts.md）。文字让机器人本地 piper 合成；有音频文件就原样推过去
* browser  —— 经 SSE 推给网页，由浏览器播放音频（无音频文件时用浏览器自带 speechSynthesis 念文本）
* local    —— 本机扬声器（ffplay）
* http     —— 本项目自带的 audio_server（audio_server/，纯标准库；给固件太旧没有 /tts 的机器人兜底）
* webhook  —— POST JSON 到任意地址
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app import platform
from app.robot.client import RobotError, clean_error_text
from app.vendor.certaintyx import RobotClient


class TtsEngine:
    name = 'base'
    ext = 'mp3'

    def synthesize(self, text: str, out_path: Path) -> Path | None:
        raise NotImplementedError


class NullEngine(TtsEngine):
    """不生成音频，只把文本交给汇出（浏览器会用 speechSynthesis 念）。"""
    name = 'none'

    def synthesize(self, text, out_path):
        return None


class EdgeTtsEngine(TtsEngine):
    name = 'edge'

    def __init__(self, voice: str = 'zh-CN-XiaoxiaoNeural', timeout: float = 20.0) -> None:
        self.voice = voice
        self.timeout = timeout

    def synthesize(self, text, out_path):
        import edge_tts

        async def go():
            await edge_tts.Communicate(text, self.voice).save(str(out_path))
        # 微软的接口偶尔会卡住不返回：不设超时会把整条执行线程挂死（通宵观察里真的发生了）
        asyncio.run(asyncio.wait_for(go(), timeout=self.timeout))
        return out_path if out_path.exists() and out_path.stat().st_size > 0 else None


class CommandEngine(TtsEngine):
    """任意命令行 TTS：模板里用 {text} 与 {out}，例如 `espeak-ng -v cmn -w {out} {text}` 或 piper。"""
    name = 'command'
    ext = 'wav'

    def __init__(self, template: str) -> None:
        self.template = template

    def synthesize(self, text, out_path):
        if not self.template:
            raise RuntimeError('TTS_COMMAND 为空')
        cmd = [part.replace('{text}', text).replace('{out}', str(out_path)) for part in platform.split_command(self.template)]
        r = subprocess.run(cmd, capture_output=True, timeout=60, **platform.SUBPROCESS_KW)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode('utf-8', 'replace')[:300])
        return out_path if out_path.exists() else None


class AudioSink:
    name = 'base'

    def play(self, text: str, audio_path: Path | None, audio_url: str | None, meta: dict) -> str:
        raise NotImplementedError


class BrowserSink(AudioSink):
    name = 'browser'

    def __init__(self, bus) -> None:
        self.bus = bus

    def play(self, text, audio_path, audio_url, meta):
        self.bus.publish('tts', {'text': text, 'audio_url': audio_url, 'meta': meta, 'ts': time.time()})
        return 'ok' if self.bus.subscriber_count else 'ok（当前没有打开的页面）'


class LocalSpeakerSink(AudioSink):
    name = 'local'

    def play(self, text, audio_path, audio_url, meta):
        if audio_path is None:
            return 'skip（无音频文件）'
        player = platform.ffmpeg_exe('ffplay')
        if player:
            cmd = [player, '-nodisp', '-autoexit', '-loglevel', 'error', str(audio_path)]
        elif shutil.which('paplay') and audio_path.suffix == '.wav':
            cmd = ['paplay', str(audio_path)]
        else:
            return 'error: 找不到 ffplay/paplay'
        threading.Thread(target=lambda: subprocess.run(cmd, capture_output=True, timeout=120, **platform.SUBPROCESS_KW), daemon=True).start()
        return 'ok'


class RobotSpeakerSink(AudioSink):
    """机器人自带扬声器：云端 POST …/tts（文字，机器人离线 piper 合成）/ POST …/tts/audio（我们合成好的音频）。

    * mode=auto：有音频文件就推音频（保留 edge 的音色）；推不成（ffmpeg 缺失 / 格式不认 / 超 5 MB）退回文字
      让机器人自己念 —— 「永远有声音」；没有音频文件（TTS_ENGINE=none）就直接发文字
    * mode=text：只发文字（机器人离线合成，不依赖外网，最省）；mode=audio：只推音频，没有就 skip
    * 旧固件（< 2026-09-14）整组端点回一页 HTML 的 404：识别出来给明确提示，不做无谓的退回
    * 与巡检下发共用 5 rps 限流预算（经 gateway.limiter）和控制权：409 = 现场有人在网页上操作
    * wait=True 时机器人最多等 50 s 再返回，比 SDK 默认 30 s 超时长，所以这里用自己的 RobotClient（超时 60 s）
    """
    name = 'robot'
    AUDIO_FORMATS = {'mp3', 'wav', 'ogg', 'opus', 'flac', 'm4a', 'aac'}
    FALLBACK_STATUSES = {400, 413, 503}     # 这些是「音频这条路走不通」，退回文字有意义；409/429/5xx 则不是

    def __init__(self, gateway_provider, *, mode: str = 'auto', volume: int | None = None, wait: bool = False,
                 interrupt: bool = False, http_timeout: float = 60.0) -> None:
        self.gateway_provider = gateway_provider
        self.mode = mode if mode in ('auto', 'text', 'audio') else 'auto'
        self.volume, self.wait, self.interrupt = volume, bool(wait), bool(interrupt)
        self.http_timeout = float(http_timeout)
        self._client: RobotClient | None = None
        self._client_key: tuple | None = None
        self.last: dict | None = None

    def describe(self) -> dict:
        return {'mode': self.mode, 'volume': self.volume, 'wait': self.wait, 'interrupt': self.interrupt}

    def _client_for(self, gw) -> RobotClient:
        key = (gw.raw.base, gw.robot, gw.raw.api_key)
        if self._client is None or self._client_key != key:
            self._client = RobotClient(gw.raw.base, gw.robot, gw.raw.api_key, timeout=self.http_timeout,
                                       verify_tls=gw.raw._ctx is None)
            self._client_key = key
        return self._client

    @staticmethod
    def is_old_firmware(e: RobotError) -> bool:
        return e.status == 404 and isinstance(e.body, str) and '<' in e.body

    @staticmethod
    def _fmt_result(kind: str, r: dict) -> str:
        eng = r.get('engine') or kind
        if r.get('pending'):
            return f'ok（{eng}，>50 s 仍在播）'
        if r.get('seconds') is not None:
            return f"ok（{eng}，{float(r['seconds']):.1f}s）"
        q = r.get('queued')
        return f'queued（{eng}' + (f'，前面还有 {q} 条' if q else '') + '）'

    def _call(self, gw, kind: str, text: str, audio_path: Path | None) -> dict:
        common = {'volume': self.volume, 'wait': self.wait, 'interrupt': self.interrupt}
        gw.limiter.acquire()
        c = self._client_for(gw)
        if kind == 'audio':
            ext = Path(audio_path).suffix.lstrip('.').lower()
            return c.tts_audio(Path(audio_path).read_bytes(), format=ext if ext in self.AUDIO_FORMATS else None, **common)
        return c.tts(text, **common)

    def play(self, text, audio_path, audio_url, meta):
        gw = self.gateway_provider()
        if gw is None:
            return 'error: 云端网关未就绪'
        use_audio = audio_path is not None and self.mode in ('auto', 'audio')
        if not use_audio and self.mode == 'audio':
            return 'skip（无音频文件）'
        kind = 'audio' if use_audio else 'text'
        try:
            r = self._call(gw, kind, text, audio_path)
        except RobotError as e:
            if self.is_old_firmware(e):
                self.last = {'kind': kind, 'error': 'firmware_old'}
                return 'error: 机器人固件太旧（< 2026-09-14），没有播报接口 —— 让管理员升级固件，或改用 http 汇出（audio_server）'
            if kind == 'audio' and self.mode == 'auto' and e.status in self.FALLBACK_STATUSES:
                try:
                    r = self._call(gw, 'text', text, None)
                except RobotError as e2:
                    self.last = {'kind': 'text', 'error': str(e2)}
                    return f'error: 音频 HTTP {e.status} {clean_error_text(str(e), 80)}；退回文字也失败 HTTP {e2.status} {clean_error_text(str(e2), 80)}'
                self.last = {'kind': 'text', 'result': r, 'fallback_from': e.status}
                return f'{self._fmt_result("text", r)} · 音频被拒 HTTP {e.status}，已退回文字'
            self.last = {'kind': kind, 'error': str(e)}
            hint = '（现场有人在网页上操作，等对方放手）' if e.status == 409 else ('（机器人播报队列满 20 条）' if e.status == 429 else '')
            return f'error: HTTP {e.status} {clean_error_text(str(e), 120)}{hint}'
        self.last = {'kind': kind, 'result': r}
        return self._fmt_result(kind, r)


class AudioServerSink(AudioSink):
    """推给本项目自带的 audio_server（机器狗 / 现场 PC 上的喇叭）。

    有音频文件就直接把字节 POST 到 /play-audio（对方不需要联网也不需要 TTS 引擎）；
    只有文本（TTS_ENGINE=none）时 POST /play 让对方自己合成（对方要配引擎）。
    """
    name = 'http'

    def __init__(self, url: str, token: str = '', timeout: float = 8.0) -> None:
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        h = dict(extra or {})
        if self.token:
            h['X-Audio-Token'] = self.token
        return h

    def play(self, text, audio_path, audio_url, meta):
        import urllib.parse
        import requests
        try:
            if audio_path is not None:
                ctype = 'audio/wav' if str(audio_path).endswith('.wav') else 'audio/mpeg'
                r = requests.post(f'{self.url}/play-audio?{urllib.parse.urlencode({"text": text})}', data=Path(audio_path).read_bytes(),
                                  headers=self._headers({'Content-Type': ctype}), timeout=self.timeout)
            else:
                r = requests.post(f'{self.url}/play', json={'text': text, 'wait': False}, headers=self._headers(), timeout=self.timeout)
            if r.ok:
                return 'ok'
            try:
                return f"error: HTTP {r.status_code} {r.json().get('error', '')}"
            except ValueError:
                return f'error: HTTP {r.status_code}'
        except requests.RequestException as e:
            return f'error: {e}'


class WebhookSink(AudioSink):
    name = 'webhook'

    def __init__(self, url: str) -> None:
        self.url = url

    def play(self, text, audio_path, audio_url, meta):
        import requests
        try:
            r = requests.post(self.url, json={'text': text, 'audio_url': audio_url, 'meta': meta}, timeout=5)
            return 'ok' if r.ok else f'error: HTTP {r.status_code}'
        except requests.RequestException as e:
            return f'error: {e}'


class TtsService:
    """合成 + 汇出。合成结果按 (引擎, 声音, 文本) 缓存在 media/tts_cache/ —— 巡检的播报句子是答案模版里
    早就写死的三句话，任务开始时 prewarm() 全部先合成好，到点时零合成（edge 每句 0.5–1.3 s 就省下来了）；
    推给机器人的字节也因此每次相同，机器人端按内容哈希命中缓存、不再转码。"""

    def __init__(self, engine: TtsEngine, sinks: list[AudioSink], media_dir: Path, url_prefix: str = '/media',
                 timeout: float = 20.0) -> None:
        self.engine = engine
        self.sinks = sinks
        self.media_dir = Path(media_dir)
        self.url_prefix = url_prefix
        self.timeout = float(timeout)
        self.lock = threading.Lock()
        self.cache_dir = self.media_dir / 'tts_cache'
        self.cache_hits = 0
        self.synth_count = 0
        self._prewarm_thread: threading.Thread | None = None

    def _synthesize(self, text: str, out_path: Path) -> Path | None:
        """合成放到**守护**线程里、带上限等待：即使引擎本身不理会取消，也不能拖住执行器。

        刻意不用 ThreadPoolExecutor —— 它的工作线程是非守护的，解释器退出时 atexit 会 join 它们：
        一个卡住的合成（edge-tts 真的会卡，见 T21）会让进程 30 s 都退不掉，
        于是 `start.sh --stop` 落到 SIGKILL，应用就来不及中止执行、给云端发 DELETE /task 停机器人。
        守护线程被超时丢下后不影响退出。
        """
        box: dict = {}

        def work() -> None:
            try:
                box['path'] = self.engine.synthesize(text, out_path)
            except BaseException as e:      # noqa: BLE001 —— 原样交回调用方
                box['error'] = e
        th = threading.Thread(target=work, name='tts-synth', daemon=True)
        th.start()
        th.join(self.timeout + 1.0)
        if th.is_alive():
            raise TimeoutError(f'合成超时（>{self.timeout:.0f}s）')
        if 'error' in box:
            raise box['error']
        return box.get('path')

    # ── 缓存 ────────────────────────────────────────────────────────────────
    def _voice_tag(self) -> str:
        return str(getattr(self.engine, 'voice', None) or getattr(self.engine, 'template', '') or '')

    def cache_path(self, text: str) -> Path:
        h = hashlib.sha1(f'{self.engine.name}|{self._voice_tag()}|{text}'.encode('utf-8')).hexdigest()[:20]
        return self.cache_dir / f'{h}.{self.engine.ext}'

    def _ensure_audio(self, text: str) -> tuple[Path | None, bool]:
        """返回 (音频路径或 None, 是否命中缓存)。合成写临时文件再原子改名，prewarm 与 speak 并发也不会互相读到半个文件。"""
        path = self.cache_path(text)
        if path.exists() and path.stat().st_size > 0:
            try:
                os.utime(path, None)                       # 记一下「最近用过」，给 cleanup_media 按时间清理用
            except OSError:
                pass
            self.cache_hits += 1
            return path, True
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp{path.suffix}')
        try:
            out = self._synthesize(text, tmp)
            self.synth_count += 1
            if out is None or not tmp.exists() or tmp.stat().st_size == 0:
                return None, False
            os.replace(tmp, path)
            return path, False
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def prewarm(self, texts, *, block: bool = False) -> int:
        """把一批句子先合成进缓存（后台守护线程，逐句、带超时）。返回还没在缓存里的句数。"""
        if isinstance(self.engine, NullEngine):
            return 0
        todo = []
        seen = set()
        for t in texts:
            t = (t or '').strip()
            if t and t not in seen and not self.cache_path(t).exists():
                seen.add(t)
                todo.append(t)
        if not todo:
            return 0

        def work() -> None:
            for t in todo:
                try:
                    self._ensure_audio(t)
                except Exception:      # noqa: BLE001 —— 预合成失败不影响什么，到点时会再试一次
                    pass
        th = threading.Thread(target=work, name='tts-prewarm', daemon=True)
        th.start()
        self._prewarm_thread = th
        if block:
            th.join(self.timeout * len(todo) + 2.0)
        return len(todo)

    def describe(self) -> dict:
        out = {'engine': self.engine.name, 'sinks': [s.name for s in self.sinks]}
        for s in self.sinks:
            if hasattr(s, 'describe'):
                out[s.name] = s.describe()
        return out

    def speak(self, text: str, meta: dict | None = None) -> dict:
        meta = meta or {}
        text = (text or '').strip()
        out = {'text': text, 'engine': self.engine.name, 'audio_path': None, 'audio_url': None, 'sinks': {}, 'error': None}
        if not text:
            out['error'] = '空文本'
            return out
        audio_path = None
        if not isinstance(self.engine, NullEngine):
            t_syn = time.time()
            try:
                audio_path, hit = self._ensure_audio(text)
                out['cache_hit'] = hit
            except TimeoutError as e:
                out['error'] = str(e)
            except Exception as e:      # noqa: BLE001 —— 合成失败不阻断播报，浏览器仍可念文本
                out['error'] = f'合成失败: {e}'
            out['synth_ms'] = int((time.time() - t_syn) * 1000)
        if audio_path:
            out['audio_path'] = audio_path.relative_to(self.media_dir).as_posix()      # URL 永远用正斜杠（Windows 的 Path 是反斜杠）
            out['audio_url'] = f"{self.url_prefix}/{out['audio_path']}"
        for s in self.sinks:
            try:
                out['sinks'][s.name] = s.play(text, audio_path, out['audio_url'], meta)
            except Exception as e:      # noqa: BLE001
                out['sinks'][s.name] = f'error: {e}'
        return out


def build_tts(cfg, bus, media_dir: Path, gateway_provider=None) -> TtsService:
    eng = (cfg.get('TTS_ENGINE') or 'edge').lower()
    timeout = cfg.get_float('TTS_TIMEOUT')
    if eng == 'edge':
        engine: TtsEngine = EdgeTtsEngine(cfg.get('TTS_VOICE'), timeout=timeout)
    elif eng == 'command':
        engine = CommandEngine(cfg.get('TTS_COMMAND'))
    else:
        engine = NullEngine()
    sinks: list[AudioSink] = []
    for name in [s.strip() for s in (cfg.get('TTS_SINKS') or 'browser').split(',') if s.strip()]:
        if name == 'browser':
            sinks.append(BrowserSink(bus))
        elif name == 'robot' and gateway_provider is not None:
            vol_raw = (cfg.get('TTS_ROBOT_VOLUME') or '').strip()
            volume = max(0, min(100, int(float(vol_raw)))) if vol_raw else None
            sinks.append(RobotSpeakerSink(gateway_provider, mode=(cfg.get('TTS_ROBOT_MODE') or 'auto').lower(),
                                          volume=volume, wait=cfg.get_bool('TTS_ROBOT_WAIT'),
                                          interrupt=cfg.get_bool('TTS_ROBOT_INTERRUPT')))
        elif name == 'local':
            sinks.append(LocalSpeakerSink())
        elif name == 'http' and cfg.get('TTS_AUDIO_SERVER_URL'):
            sinks.append(AudioServerSink(cfg.get('TTS_AUDIO_SERVER_URL'), cfg.get('TTS_AUDIO_SERVER_TOKEN')))
        elif name == 'webhook' and cfg.get('TTS_WEBHOOK_URL'):
            sinks.append(WebhookSink(cfg.get('TTS_WEBHOOK_URL')))
    if not sinks:
        sinks.append(BrowserSink(bus))
    return TtsService(engine, sinks, media_dir, timeout=timeout)
