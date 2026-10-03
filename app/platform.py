# -*- coding: utf-8 -*-
"""跨平台的小入口：runtime 目录、ffmpeg 路径、字体候选、命令模板切分、子进程参数。

桌面版把 Python、依赖、ffmpeg、字体一起装进 `runtime/`（`scripts/build_runtime.py`，见 docs/DESKTOP.md），
三个平台装的是同一份，所以「用哪个 ffmpeg、哪个字体」在这里统一回答：
先 runtime，再环境变量，最后才是 PATH / 系统目录。没有 runtime 时（Ubuntu 开发环境）行为与以前一样。

注意模块名与标准库 `platform` 同名：包内一律 `from app import platform` 显式引用，别 `import platform` 再混用。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'

# Windows 上从无控制台的进程（桌面壳拉起的 python）再起 ffmpeg 会闪一个黑框；CREATE_NO_WINDOW 压掉它。
SUBPROCESS_KW: dict = {'creationflags': subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def runtime_dir() -> Path | None:
    """`PS_RUNTIME_DIR` 优先；否则仓库旁的 `runtime/`（开发机上 build_runtime.py 的产物），
    再否则上一级的 `runtime/`（打包后的布局：resources/backend/ 与 resources/runtime/ 并列）。都没有返回 None。"""
    env = os.environ.get('PS_RUNTIME_DIR')
    for c in ([Path(env)] if env else []) + [ROOT / 'runtime', ROOT.parent / 'runtime']:
        if (c / 'manifest.json').is_file():
            return c
    return None


def _exe(name: str) -> str:
    return f'{name}.exe' if IS_WINDOWS else name


def ffmpeg_exe(name: str = 'ffmpeg') -> str | None:
    """ffmpeg / ffplay 的可执行文件：`PS_FFMPEG_DIR` → runtime/ffmpeg/ → PATH。找不到返回 None。"""
    cands: list[Path] = []
    env = os.environ.get('PS_FFMPEG_DIR')
    if env:
        cands.append(Path(env) / _exe(name))
    rt = runtime_dir()
    if rt:
        cands.append(rt / 'ffmpeg' / _exe(name))
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which(name)


def font_candidates() -> list[Path]:
    """中文字体候选，按优先级：`PS_FONT` → runtime/fonts/ → 各系统自带的 CJK 字体。"""
    out: list[Path] = []
    env = os.environ.get('PS_FONT')
    if env:
        out.append(Path(env))
    rt = runtime_dir()
    if rt and (rt / 'fonts').is_dir():
        out += sorted(p for p in (rt / 'fonts').iterdir() if p.suffix.lower() in ('.otf', '.ttf', '.ttc'))
    if IS_WINDOWS:
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        out += [fonts / n for n in ('msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'simsun.ttc', 'arial.ttf')]
    elif IS_MAC:
        out += [Path('/System/Library/Fonts/PingFang.ttc'), Path('/System/Library/Fonts/STHeiti Light.ttc'),
                Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'), Path('/Library/Fonts/Arial Unicode.ttf')]
    else:
        out += [Path(p) for p in ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
                                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc',
                                   '/usr/share/fonts/truetype/arphic/uming.ttc',
                                   '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    return out


def split_command(template: str, *, windows: bool | None = None) -> list[str]:
    """把 `TTS_COMMAND` 这类命令模板切成参数列表。

    POSIX 上就是 shlex.split；Windows 上关掉反斜杠转义（否则 `C:\\Users\\x` 会被吃成 `C:Usersx`），
    引号照常起作用，所以带空格的路径用双引号括起来即可。
    """
    if windows is None:
        windows = IS_WINDOWS
    if not windows:
        return shlex.split(template)
    lex = shlex.shlex(template, posix=True)
    lex.whitespace_split = True
    lex.escape = ''
    lex.commenters = ''
    return list(lex)


def describe() -> dict:
    """给 /api/health 与桌面壳看的平台信息：三平台对账时一眼看出用的是不是同一套运行时。"""
    rt = runtime_dir()
    font = next((str(p) for p in font_candidates() if p.is_file()), None)
    return {'os': sys.platform, 'python': sys.version.split()[0], 'executable': sys.executable,
            'runtime_dir': str(rt) if rt else None, 'ffmpeg': ffmpeg_exe(), 'font': font}
