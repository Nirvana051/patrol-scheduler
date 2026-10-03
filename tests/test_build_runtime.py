# -*- coding: utf-8 -*-
"""runtime 组装脚本里不联网的那部分：平台键、轮子里找 ffmpeg、解释器布局、下载的哈希跳过逻辑。"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('build_runtime', ROOT / 'scripts' / 'build_runtime.py')
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


@pytest.mark.parametrize('plat, machine, key', [
    ('linux', 'x86_64', 'linux-x64'), ('win32', 'AMD64', 'win32-x64'),
    ('darwin', 'arm64', 'darwin-arm64'), ('darwin', 'x86_64', 'darwin-x64'),
])
def test_platform_key(monkeypatch, plat, machine, key):
    monkeypatch.setattr(br.sys, 'platform', plat)
    monkeypatch.setattr(br, 'IS_WIN', plat == 'win32')
    monkeypatch.setattr(br._platform, 'machine', lambda: machine)
    assert br.platform_key() == key


def test_platform_key_rejects_unsupported(monkeypatch):
    monkeypatch.setattr(br.sys, 'platform', 'linux')
    monkeypatch.setattr(br, 'IS_WIN', False)
    monkeypatch.setattr(br._platform, 'machine', lambda: 'aarch64')
    with pytest.raises(SystemExit):
        br.platform_key()


def test_wheel_member_finds_ffmpeg_binary_and_version(tmp_path):
    w = tmp_path / 'imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl'
    with zipfile.ZipFile(w, 'w') as z:
        z.writestr('imageio_ffmpeg/binaries/README.md', 'x')
        z.writestr('imageio_ffmpeg/binaries/__init__.py', '')
        z.writestr('imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe', b'MZ')
        z.writestr('imageio_ffmpeg-0.6.0.dist-info/LICENSE', 'BSD')
    member, ver = br._wheel_member(w)
    assert member == 'imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe' and ver == '7.1'
    empty = tmp_path / 'empty.whl'
    with zipfile.ZipFile(empty, 'w') as z:
        z.writestr('imageio_ffmpeg/binaries/README.md', 'x')
    with pytest.raises(SystemExit):
        br._wheel_member(empty)


def test_find_interpreter_handles_both_layouts(tmp_path):
    pydir = tmp_path / 'python'
    linux = pydir / 'cpython-3.12.13-linux-x86_64-gnu' / 'bin'
    linux.mkdir(parents=True)
    (linux / 'python3').write_bytes(b'')
    assert br.find_interpreter(pydir, '3.12.13') == linux / 'python3'
    assert br.find_interpreter(pydir, '3.13.0') is None
    win = tmp_path / 'py2' / 'cpython-3.12.13-windows-x86_64-none'
    win.mkdir(parents=True)
    (win / 'python.exe').write_bytes(b'')
    assert br.find_interpreter(tmp_path / 'py2', '3.12.13') == win / 'python.exe'
    # uv 留下的小版本别名符号链接不能被当成解释器目录
    if not sys.platform.startswith('win'):
        (pydir / 'cpython-3.12-linux-x86_64-gnu').symlink_to(pydir / 'cpython-3.12.13-linux-x86_64-gnu')
        assert br.find_interpreter(pydir, '3.12.13') == linux / 'python3'


def test_download_skips_when_hash_matches(tmp_path, monkeypatch):
    dest = tmp_path / 'a.bin'
    dest.write_bytes(b'hello')
    sha = hashlib.sha256(b'hello').hexdigest()
    calls = []
    monkeypatch.setattr(br.urllib.request, 'urlopen', lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError('no network')))
    assert br.download('http://x/a.bin', dest, expect_sha=sha) == dest and calls == []
    with pytest.raises(SystemExit):                       # 哈希不符 → 会去下载 → 这里没网 → 三次重试后退出
        monkeypatch.setattr(br.time, 'sleep', lambda _s: None)
        br.download('http://x/a.bin', dest, expect_sha='0' * 64)


def test_lock_file_is_complete():
    lock = br.load_spec()
    assert lock['python']['version'] == '3.12.13'
    assert set(lock['ffmpeg']['assets']) == {'linux-x64', 'win32-x64', 'darwin-x64', 'darwin-arm64'}
    for key, a in lock['ffmpeg']['assets'].items():
        assert len(a['sha256']) == 64 and a['member'].startswith('imageio_ffmpeg/binaries/ffmpeg-') and a['ffmpeg_version'], key
        assert a['file'].endswith('.whl') and a['url'].startswith('https://files.pythonhosted.org/')
    assert any(f['file'].endswith('.otf') and len(f['sha256']) == 64 for f in lock['fonts'])
