# -*- coding: utf-8 -*-
"""audio_server —— 巡检播报的「扬声器端」。纯标准库，部署只需 python3 + ffplay（或 aplay/paplay/mpg123）。

    python -m audio_server --host 0.0.0.0 --port 5566 [--token 口令] [--player auto|ffplay|aplay|paplay|mpg123|dry]
                           [--engine none|edge|command] [--command 'espeak-ng -v cmn -w {out} {text}'] [--voice zh-CN-XiaoxiaoNeural]

接口（JSON 响应 {"ok": bool, ...}）：
    GET  /health                       状态：播放器、引擎、队列长度、正在播放
    POST /play-audio?text=...&wait=0|1  请求体 = 音频字节（Content-Type: audio/mpeg 或 audio/wav）→ 入队播放
    POST /play  {"text": "...", "wait": false}   只有文本：有引擎就本地合成再播；没有引擎返回 501
    POST /stop                          停掉正在播放的并清空队列
    鉴权（可选）：启动时给 --token，请求头带 X-Audio-Token

设计：单一播放队列 + 一个播放线程，保证播报不重叠；`wait=1` 阻塞到这条播完（默认不等）。
典型用法是调度系统在自己那边用 edge-tts 合成好，把 mp3 推过来 —— 机器人端不需要联网、不需要装任何 TTS。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def pick_player(preferred: str = 'auto') -> list[str] | None:
    """返回播放命令模板（最后追加文件路径）。dry = 不出声只记录（测试用）。"""
    if preferred == 'dry':
        return ['dry']
    cands = {
        'ffplay': ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'error'],
        'mpg123': ['mpg123', '-q'],
        'paplay': ['paplay'],
        'aplay': ['aplay', '-q'],
    }
    order = [preferred] if preferred != 'auto' else ['ffplay', 'mpg123', 'paplay', 'aplay']
    for name in order:
        if name in cands and shutil.which(name):
            return cands[name]
    return None


class Engine:
    """可选的本地合成引擎（只在收到纯文本时用）。"""

    def __init__(self, kind: str = 'none', command: str = '', voice: str = 'zh-CN-XiaoxiaoNeural', timeout: float = 20.0) -> None:
        self.kind, self.command, self.voice, self.timeout = kind, command, voice, timeout

    def available(self) -> bool:
        if self.kind == 'edge':
            try:
                import edge_tts  # noqa: F401
                return True
            except ImportError:
                return False
        return self.kind == 'command' and bool(self.command)

    def synthesize(self, text: str, out_dir: Path) -> Path:
        if self.kind == 'edge':
            import asyncio
            import edge_tts
            out = out_dir / f'tts_{int(time.time() * 1000)}.mp3'

            async def go():
                await edge_tts.Communicate(text, self.voice).save(str(out))
            asyncio.run(asyncio.wait_for(go(), timeout=self.timeout))
            return out
        if self.kind == 'command':
            import shlex
            out = out_dir / f'tts_{int(time.time() * 1000)}.wav'
            cmd = [p.replace('{text}', text).replace('{out}', str(out)) for p in shlex.split(self.command)]
            subprocess.run(cmd, check=True, timeout=self.timeout, capture_output=True)
            return out
        raise RuntimeError('no engine')


class Player(threading.Thread):
    def __init__(self, cmd: list[str] | None, work_dir: Path) -> None:
        super().__init__(name='audio-player', daemon=True)
        self.cmd = cmd
        self.work_dir = work_dir
        self.q: queue.Queue = queue.Queue()
        self.current: subprocess.Popen | None = None
        self.now_playing: str | None = None
        self.played: list[dict] = []          # dry 模式 / 排障用：最近 50 条
        self.lock = threading.Lock()
        self.start()

    def enqueue(self, path: Path, text: str) -> threading.Event:
        done = threading.Event()
        self.q.put((path, text, done))
        return done

    def stop_all(self) -> int:
        n = 0
        while True:
            try:
                _, _, done = self.q.get_nowait()
                done.set()
                n += 1
            except queue.Empty:
                break
        with self.lock:
            if self.current and self.current.poll() is None:
                self.current.terminate()
                n += 1
        return n

    def run(self) -> None:
        while True:
            path, text, done = self.q.get()
            try:
                self.now_playing = text
                if self.cmd == ['dry'] or not self.cmd:
                    time.sleep(0.05)
                else:
                    with self.lock:
                        self.current = subprocess.Popen(self.cmd + [str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.current.wait(timeout=120)
            except Exception as e:      # noqa: BLE001
                print(f'[audio] 播放失败: {e}', file=sys.stderr)
            finally:
                with self.lock:
                    self.current = None
                self.now_playing = None
                self.played.append({'text': text, 'file': path.name, 'ts': time.time()})
                del self.played[:-50]
                try:
                    if path.parent == self.work_dir:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                done.set()


def make_handler(player: Player, engine: Engine, token: str | None, work_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'patrol-audio/1.0'

        def log_message(self, fmt, *args):      # 安静一点
            if os.environ.get('AUDIO_SERVER_VERBOSE'):
                super().log_message(fmt, *args)

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self) -> bool:
            if token and self.headers.get('X-Audio-Token') != token:
                self._send(401, {'ok': False, 'error': '口令不对'})
                return False
            return True

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == '/health':
                return self._send(200, {'ok': True, 'player': player.cmd[0] if player.cmd else None, 'engine': engine.kind if engine.available() else None,
                                        'queue': player.q.qsize(), 'now_playing': player.now_playing, 'recent': player.played[-5:]})
            self._send(404, {'ok': False, 'error': 'not found'})

        def do_POST(self):
            if not self._auth():
                return
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            wait = (qs.get('wait', ['0'])[0] in ('1', 'true'))
            if url.path == '/play-audio':
                if not body:
                    return self._send(400, {'ok': False, 'error': '空音频'})
                ctype = (self.headers.get('Content-Type') or '').lower()
                ext = '.wav' if 'wav' in ctype else '.mp3'
                text = qs.get('text', [''])[0]
                fd, tmp = tempfile.mkstemp(prefix='play_', suffix=ext, dir=str(work_dir))
                with os.fdopen(fd, 'wb') as f:
                    f.write(body)
                done = player.enqueue(Path(tmp), text)
                if wait:
                    done.wait(timeout=120)
                return self._send(200, {'ok': True, 'queued': player.q.qsize(), 'finished': done.is_set()})
            if url.path == '/play':
                try:
                    payload = json.loads(body or b'{}')
                except ValueError:
                    return self._send(400, {'ok': False, 'error': 'JSON 无效'})
                text = str(payload.get('text') or '').strip()
                if not text:
                    return self._send(400, {'ok': False, 'error': '缺 text'})
                if not engine.available():
                    return self._send(501, {'ok': False, 'error': '本机没有 TTS 引擎，请改用 /play-audio 直接推音频'})
                try:
                    path = engine.synthesize(text, work_dir)
                except Exception as e:      # noqa: BLE001
                    return self._send(500, {'ok': False, 'error': f'合成失败: {e}'})
                done = player.enqueue(path, text)
                if wait or payload.get('wait'):
                    done.wait(timeout=120)
                return self._send(200, {'ok': True, 'queued': player.q.qsize(), 'finished': done.is_set()})
            if url.path == '/stop':
                return self._send(200, {'ok': True, 'stopped': player.stop_all()})
            self._send(404, {'ok': False, 'error': 'not found'})
    return Handler


def serve(host: str = '0.0.0.0', port: int = 5566, *, token: str | None = None, player: str = 'auto',
          engine: Engine | None = None, work_dir: Path | None = None) -> ThreadingHTTPServer:
    work_dir = work_dir or Path(tempfile.mkdtemp(prefix='patrol-audio-'))
    cmd = pick_player(player)
    if cmd is None:
        print('[audio] 没找到播放器（ffplay/mpg123/paplay/aplay），进入 dry 模式只记录不出声', file=sys.stderr)
        cmd = ['dry']
    pl = Player(cmd, work_dir)
    srv = ThreadingHTTPServer((host, port), make_handler(pl, engine or Engine(), token, work_dir))
    srv.player = pl              # type: ignore[attr-defined]
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description='巡检播报服务（扬声器端）')
    ap.add_argument('--host', default=os.environ.get('AUDIO_HOST', '0.0.0.0'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('AUDIO_PORT', '5566')))
    ap.add_argument('--token', default=os.environ.get('AUDIO_TOKEN') or None)
    ap.add_argument('--player', default=os.environ.get('AUDIO_PLAYER', 'auto'), help='auto|ffplay|mpg123|paplay|aplay|dry')
    ap.add_argument('--engine', default=os.environ.get('AUDIO_ENGINE', 'none'), help='none|edge|command（只在收到纯文本时用）')
    ap.add_argument('--command', default=os.environ.get('AUDIO_COMMAND', ''), help="command 引擎模板，如 'espeak-ng -v cmn -w {out} {text}'")
    ap.add_argument('--voice', default=os.environ.get('AUDIO_VOICE', 'zh-CN-XiaoxiaoNeural'))
    a = ap.parse_args()
    srv = serve(a.host, a.port, token=a.token, player=a.player, engine=Engine(a.engine, a.command, a.voice))
    print(f'播报服务 http://{a.host}:{a.port}  播放器={srv.player.cmd[0]}  引擎={a.engine}  鉴权={"是" if a.token else "否"}')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
