#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出本平台的桌面安装包（desktop/dist/）。三平台、CI、本机都走这一条路。

    python3 scripts/build_desktop.py                 # 核对 runtime → 同步版本号 → npm ci → 自检(--smoke) → electron-builder
    python3 scripts/build_desktop.py --skip-smoke    # 不跑自检（没有显示环境的机器）
    python3 scripts/build_desktop.py --publish       # 打 tag 时在 CI 上用：把产物与更新元数据传到 GitHub Releases（需 GH_TOKEN）
    python3 scripts/build_desktop.py --only-smoke    # 只跑自检，不打包

步骤：
  1. runtime/ 必须已由 scripts/build_runtime.py 组装好且 --check 通过（锁没变、解释器与 ffmpeg 能跑）
  2. desktop/package.json 的 version 改成 app/__init__.py 的 __version__（版本号只有一个来源）
  3. npm ci（有 package-lock.json 时）或 npm install
  4. electron . --smoke：拉起后端（仿真）→ 健康检查 → 抓图 → 优雅退出
  5. electron-builder 出本平台的目标（Windows nsis+zip / macOS dmg+zip / Linux AppImage+deb）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / 'desktop'
IS_WIN = sys.platform == 'win32'
for _s in (sys.stdout, sys.stderr):           # Windows CI 的 stdout 默认 cp1252，中文会 UnicodeEncodeError
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')


def say(msg: str) -> None:
    print(msg, flush=True)


def npm() -> str:
    exe = shutil.which('npm.cmd' if IS_WIN else 'npm') or shutil.which('npm')
    if not exe:
        raise SystemExit('没有 npm：装 Node.js 20+（https://nodejs.org），CI 用 actions/setup-node')
    return exe


def npx() -> str:
    exe = shutil.which('npx.cmd' if IS_WIN else 'npx') or shutil.which('npx')
    if not exe:
        raise SystemExit('没有 npx')
    return exe


def run(cmd: list[str], **kw) -> None:
    env = {**os.environ, **kw.pop('env', {})}
    # 这台机器若是 VS Code / Claude Code 之类的 Electron 宿主，会带着 ELECTRON_RUN_AS_NODE=1，
    # 那样 `electron .` 会当成普通 node 跑、壳根本起不来 —— 清掉
    env.pop('ELECTRON_RUN_AS_NODE', None)
    say('  $ ' + ' '.join(cmd))
    r = subprocess.run(cmd, cwd=kw.pop('cwd', DESKTOP), env=env, **kw)
    if r.returncode != 0:
        raise SystemExit(f'命令失败（退出码 {r.returncode}）：{" ".join(cmd)}')


def code_version() -> str:
    m = re.search(r"__version__ = '([^']+)'", (ROOT / 'app' / '__init__.py').read_text(encoding='utf-8'))
    if not m:
        raise SystemExit('app/__init__.py 里找不到 __version__')
    return m.group(1)


def sync_version() -> str:
    ver = code_version()
    pkg = DESKTOP / 'package.json'
    data = json.loads(pkg.read_text(encoding='utf-8'))
    if data.get('version') != ver:
        data['version'] = ver
        pkg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        say(f'  desktop/package.json version → {ver}')
    else:
        say(f'  版本 {ver}')
    return ver


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--skip-smoke', action='store_true')
    ap.add_argument('--only-smoke', action='store_true')
    ap.add_argument('--publish', action='store_true', help='electron-builder --publish always（CI 打 tag 时）')
    ap.add_argument('--target', default='', help='只出这些目标（空格分隔），如 "AppImage" / "nsis zip" / "dmg zip"（默认按 package.json）')
    a = ap.parse_args()

    say('① 核对 runtime')
    run([sys.executable, str(ROOT / 'scripts' / 'build_runtime.py'), '--check'], cwd=ROOT)
    say('② 版本号')
    ver = sync_version()
    say('③ npm 依赖')
    if (DESKTOP / 'package-lock.json').exists():
        run([npm(), 'ci', '--no-audit', '--no-fund'])
    else:
        run([npm(), 'install', '--no-audit', '--no-fund'])
    if not (DESKTOP / 'node_modules' / 'electron' / 'dist').exists():
        run(['node', 'node_modules/electron/install.js'])         # npm 偶尔跳过二进制下载
    if not a.skip_smoke:
        say('④ 自检（拉起后端 → 健康检查 → 抓图 → 优雅退出）')
        run([npx(), 'electron', '.', '--smoke'])
    if a.only_smoke:
        return 0
    say('⑤ 打包')
    cmd = [npx(), 'electron-builder']
    if a.target:
        flag = {'win32': '--win', 'darwin': '--mac'}.get(sys.platform, '--linux')
        cmd += [flag, *a.target.split()]
    if sys.platform == 'darwin':
        # macOS 的自动更新要求 latest-mac.yml 里有 zip；而 arm64 与 x64 两个作业各自发布会互相覆盖同一个 latest-mac.yml ——
        # 按架构分通道（latest-arm64 / latest-x64），壳里 autoUpdater.channel 同样按 process.arch 选
        import platform as _pf
        arch = 'arm64' if _pf.machine().lower() == 'arm64' else 'x64'
        cmd += [f'-c.publish.channel=latest-{arch}']
    cmd += ['--publish', 'always' if a.publish else 'never']
    run(cmd)
    dist = DESKTOP / 'dist'
    arts = sorted(p for p in dist.iterdir() if p.is_file() and p.suffix in ('.exe', '.zip', '.dmg', '.AppImage', '.deb', '.yml', '.blockmap'))
    say(f'完成 v{ver}：')
    for p in arts:
        say(f'  {p.name:60} {p.stat().st_size / 1048576:7.1f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
