# -*- coding: utf-8 -*-
"""跨平台入口（app/platform.py）、优雅退出接口与 .env 的容错 —— 桌面版三平台一致性的地基。"""
from __future__ import annotations

import types
from pathlib import Path

from app import platform


# ── runtime 目录与 ffmpeg / 字体的查找顺序 ─────────────────────────────────────
def test_runtime_dir_requires_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, 'ROOT', tmp_path / 'a' / 'b')       # 别让仓库里真实组装出来的 runtime/ 干扰
    rt = tmp_path / 'runtime'
    rt.mkdir()
    monkeypatch.setenv('PS_RUNTIME_DIR', str(rt))
    assert platform.runtime_dir() is None                        # 只有目录不算：要有 manifest.json 才是组装完成的 runtime
    (rt / 'manifest.json').write_text('{}', encoding='utf-8')
    assert platform.runtime_dir() == rt


def test_ffmpeg_prefers_runtime_then_env_then_path(tmp_path, monkeypatch):
    rt = tmp_path / 'runtime'
    (rt / 'ffmpeg').mkdir(parents=True)
    (rt / 'manifest.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('PS_RUNTIME_DIR', str(rt))
    monkeypatch.delenv('PS_FFMPEG_DIR', raising=False)
    monkeypatch.setattr(platform.shutil, 'which', lambda _n: '/usr/bin/ffmpeg')
    assert platform.ffmpeg_exe() == '/usr/bin/ffmpeg'            # runtime 里没有二进制 → 退到 PATH
    exe = rt / 'ffmpeg' / ('ffmpeg.exe' if platform.IS_WINDOWS else 'ffmpeg')
    exe.write_bytes(b'')
    assert platform.ffmpeg_exe() == str(exe)                     # runtime 自带的优先于 PATH
    d = tmp_path / 'custom'
    d.mkdir()
    custom = d / exe.name
    custom.write_bytes(b'')
    monkeypatch.setenv('PS_FFMPEG_DIR', str(d))
    assert platform.ffmpeg_exe() == str(custom)                  # 显式环境变量最优先
    monkeypatch.setattr(platform.shutil, 'which', lambda _n: None)
    monkeypatch.delenv('PS_FFMPEG_DIR')
    monkeypatch.delenv('PS_RUNTIME_DIR')
    monkeypatch.setattr(platform, 'ROOT', tmp_path / 'a' / 'b')      # 上一级也没有 runtime/
    assert platform.ffmpeg_exe() is None


def test_font_candidates_put_runtime_font_first(tmp_path, monkeypatch):
    rt = tmp_path / 'runtime'
    (rt / 'fonts').mkdir(parents=True)
    (rt / 'manifest.json').write_text('{}', encoding='utf-8')
    (rt / 'fonts' / 'NotoSansCJKsc-Regular.otf').write_bytes(b'')
    (rt / 'fonts' / 'LICENSE.txt').write_text('x', encoding='utf-8')
    monkeypatch.setenv('PS_RUNTIME_DIR', str(rt))
    monkeypatch.delenv('PS_FONT', raising=False)
    cands = platform.font_candidates()
    assert cands[0] == rt / 'fonts' / 'NotoSansCJKsc-Regular.otf'
    assert all(p.suffix != '.txt' for p in cands)
    monkeypatch.setenv('PS_FONT', str(tmp_path / 'my.ttf'))
    assert platform.font_candidates()[0] == tmp_path / 'my.ttf'


def test_pano_font_falls_back_without_crashing(tmp_path, monkeypatch):
    from app.media import pano
    monkeypatch.setenv('PS_FONT', str(tmp_path / 'missing.ttf'))
    assert pano.font(20) is not None                             # 候选都不存在也不炸（退到 PIL 默认字体）


# ── 命令模板切分：Windows 路径里的反斜杠不能被吃掉 ─────────────────────────────
def test_split_command_keeps_backslashes_on_windows():
    tpl = r'C:\tts\piper.exe --model "C:\voices\zh.onnx" -f {out} {text}'
    win = platform.split_command(tpl, windows=True)
    assert win[0] == r'C:\tts\piper.exe' and win[2] == r'C:\voices\zh.onnx' and win[-2:] == ['{out}', '{text}']
    posix = platform.split_command('espeak-ng -v cmn -w {out} "{text}"', windows=False)
    assert posix == ['espeak-ng', '-v', 'cmn', '-w', '{out}', '{text}']


def test_describe_reports_python_and_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv('PS_RUNTIME_DIR', raising=False)
    monkeypatch.setattr(platform, 'ROOT', tmp_path)
    d = platform.describe()
    assert d['python'].count('.') == 2 and d['os'] and d['runtime_dir'] is None
    assert set(d) == {'os', 'python', 'executable', 'runtime_dir', 'ffmpeg', 'font'}


# ── .env：带 BOM / CRLF 的记事本文件也要能读 ───────────────────────────────────
def test_env_file_with_bom_and_crlf(tmp_path, monkeypatch):
    from app.config import load_env_file
    f = tmp_path / '.env'
    f.write_bytes('\ufeffCX_ROBOT=dog-bom\r\nPS_PORT=9099 # 注释\r\n'.encode('utf-8'))
    monkeypatch.delenv('CX_ROBOT', raising=False)
    monkeypatch.delenv('PS_PORT', raising=False)
    load_env_file(f)
    import os
    assert os.environ['CX_ROBOT'] == 'dog-bom' and os.environ['PS_PORT'] == '9099'


def test_env_file_location_can_be_overridden(tmp_path, monkeypatch):
    import importlib
    import app.config as cfgmod
    monkeypatch.setenv('PS_ENV_FILE', str(tmp_path / 'elsewhere.env'))
    importlib.reload(cfgmod)
    try:
        assert cfgmod.ENV_FILE == tmp_path / 'elsewhere.env'
    finally:
        monkeypatch.delenv('PS_ENV_FILE')
        importlib.reload(cfgmod)
    assert cfgmod.ENV_FILE == Path(cfgmod.ROOT) / 'config' / '.env'


# ── 媒体 URL 永远是正斜杠 ───────────────────────────────────────────────────────
def test_snapshot_media_url_uses_forward_slashes(app_client):
    d = app_client.post('/api/robot/snapshot').json()
    assert '\\' not in d['url'] and d['url'] == f"/media/{d['path']}"
    assert app_client.get(d['url']).status_code == 200


# ── 优雅退出接口 ───────────────────────────────────────────────────────────────
def test_shutdown_endpoint_is_off_without_token(app_client, monkeypatch):
    monkeypatch.delenv('PS_SHUTDOWN_TOKEN', raising=False)
    assert app_client.post('/api/shutdown').status_code == 404


def test_shutdown_endpoint_checks_token_and_flags_server(app_client, monkeypatch):
    monkeypatch.setenv('PS_SHUTDOWN_TOKEN', 'secret-1')
    assert app_client.post('/api/shutdown').status_code == 403
    assert app_client.post('/api/shutdown', headers={'X-Shutdown-Token': 'nope'}).status_code == 403
    # 没有 Server 对象（TestClient 里）→ 501，说明它不会去乱发信号
    assert app_client.post('/api/shutdown', headers={'X-Shutdown-Token': 'secret-1'}).status_code == 501
    fake = types.SimpleNamespace(should_exit=False)
    app_client.app.state.server = fake
    r = app_client.post('/api/shutdown', headers={'X-Shutdown-Token': 'secret-1'})
    assert r.status_code == 200 and r.json()['ok'] and r.json()['active_run'] is None
    assert fake.should_exit is True
    ev = app_client.get('/api/events?limit=5').json()['items']
    assert any(e['type'] == 'shutdown_requested' for e in ev)


def test_health_and_platform_endpoint(app_client):
    h = app_client.get('/api/health').json()
    assert h['platform']['python'] == app_client.get('/api/platform').json()['python']
