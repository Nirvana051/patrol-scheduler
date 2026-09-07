# -*- coding: utf-8 -*-
"""TTS：引擎（文本→音频文件）与汇出（把播报送到某个「扬声器」）解耦。

云端 API 没有扬声器端点（docs/TODO T2），所以汇出做成插件：
* browser  —— 经 SSE 推给网页，由浏览器播放音频（无音频文件时用浏览器自带 speechSynthesis 念文本）
* local    —— 本机扬声器（ffplay）
* http     —— 本项目自带的 audio_server（audio_server/，纯标准库，部署在机器狗或现场 PC 上）
* webhook  —— POST JSON 到任意地址
"""
from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path


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
        cmd = [part.replace('{text}', text).replace('{out}', str(out_path)) for part in shlex.split(self.template)]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
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
        player = shutil.which('ffplay')
        if player:
            cmd = [player, '-nodisp', '-autoexit', '-loglevel', 'error', str(audio_path)]
        elif shutil.which('paplay') and audio_path.suffix == '.wav':
            cmd = ['paplay', str(audio_path)]
        else:
            return 'error: 找不到 ffplay/paplay'
        threading.Thread(target=lambda: subprocess.run(cmd, capture_output=True, timeout=120), daemon=True).start()
        return 'ok'


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
    def __init__(self, engine: TtsEngine, sinks: list[AudioSink], media_dir: Path, url_prefix: str = '/media',
                 timeout: float = 20.0) -> None:
        self.engine = engine
        self.sinks = sinks
        self.media_dir = Path(media_dir)
        self.url_prefix = url_prefix
        self.timeout = float(timeout)
        self.lock = threading.Lock()

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

    def describe(self) -> dict:
        return {'engine': self.engine.name, 'sinks': [s.name for s in self.sinks]}

    def speak(self, text: str, meta: dict | None = None) -> dict:
        meta = meta or {}
        text = (text or '').strip()
        out = {'text': text, 'engine': self.engine.name, 'audio_path': None, 'audio_url': None, 'sinks': {}, 'error': None}
        if not text:
            out['error'] = '空文本'
            return out
        audio_path = None
        if not isinstance(self.engine, NullEngine):
            d = self.media_dir / 'tts'
            d.mkdir(parents=True, exist_ok=True)
            stem = f"tts_{time.strftime('%Y%m%d_%H%M%S')}_{int((time.time() % 1) * 1000):03d}"
            try:
                audio_path = self._synthesize(text, d / f'{stem}.{self.engine.ext}')
            except TimeoutError as e:
                out['error'] = str(e)
            except Exception as e:      # noqa: BLE001 —— 合成失败不阻断播报，浏览器仍可念文本
                out['error'] = f'合成失败: {e}'
        if audio_path:
            out['audio_path'] = str(audio_path.relative_to(self.media_dir))
            out['audio_url'] = f"{self.url_prefix}/{out['audio_path']}"
        for s in self.sinks:
            try:
                out['sinks'][s.name] = s.play(text, audio_path, out['audio_url'], meta)
            except Exception as e:      # noqa: BLE001
                out['sinks'][s.name] = f'error: {e}'
        return out


def build_tts(cfg, bus, media_dir: Path) -> TtsService:
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
        elif name == 'local':
            sinks.append(LocalSpeakerSink())
        elif name == 'http' and cfg.get('TTS_AUDIO_SERVER_URL'):
            sinks.append(AudioServerSink(cfg.get('TTS_AUDIO_SERVER_URL'), cfg.get('TTS_AUDIO_SERVER_TOKEN')))
        elif name == 'webhook' and cfg.get('TTS_WEBHOOK_URL'):
            sinks.append(WebhookSink(cfg.get('TTS_WEBHOOK_URL')))
    if not sinks:
        sinks.append(BrowserSink(bus))
    return TtsService(engine, sinks, media_dir, timeout=timeout)
