#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组装 runtime/：固定版本的 Python 3.12 + 按 requirements.lock 装好的依赖 + ffmpeg + Noto Sans CJK 字体。

三个平台（Windows x64 / macOS arm64 与 x64 / Linux x64）跑同一个脚本、得到同一份运行时：
桌面壳（desktop/）拉起后端用它，CI 跑全量用例用它，打安装包也把它整个带上。
每个组件的来源与 sha256 钉在 `runtime.lock.json`，这就是「三平台复现 Ubuntu 表现」的基础。

    python3 scripts/build_runtime.py              # 组装到仓库旁的 runtime/（幂等：已就绪的组件跳过）
    python3 scripts/build_runtime.py --dev        # 另装 pytest / ruff（CI 用 runtime 跑全量用例）
    python3 scripts/build_runtime.py --check      # 不联网，只核对：解释器能 import、ffmpeg 能跑、字体能加载、锁没变
    python3 scripts/build_runtime.py --pin        # 重新生成 runtime.lock.json（下载全部平台的 ffmpeg 与字体算哈希）
    python3 scripts/build_runtime.py --dest DIR   # 装到别处（默认 <仓库>/runtime）
    python3 scripts/build_runtime.py --prune      # 删掉解释器里用不到的大头（标准库 test/、idlelib、tkinter），省 ~40 MB

需要：能跑这个脚本的任意 Python 3.8+ 与联网。uv 有就用，没有会用 pip 装一份到 runtime.cache/tools/。
下载缓存在 <dest>.cache/（与 runtime/ 并列，不进安装包）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / 'runtime.lock.json'
# Windows 上 stdout 是管道时默认 cp1252，print 中文 / ✅ 会直接 UnicodeEncodeError（CI 第一行就死）
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')
REQ_LOCK = ROOT / 'requirements.lock'
IS_WIN = sys.platform == 'win32'

# 第一次 --pin 用的默认来源；之后以 runtime.lock.json 为准
DEFAULT_SPEC = {
    'python': {'version': '3.12.13'},
    'ffmpeg': {
        # 来源：PyPI 上 imageio-ffmpeg 的平台轮子（里面就是一个静态 ffmpeg，只有 ffmpeg 没有 ffplay ——
        # 桌面版不需要 ffplay：窗口本身就是本机扬声器）。选它的原因：一个版本号对应四个平台、PyPI 文件不可变、
        # 哈希由 PyPI 元数据给出。各平台的 ffmpeg 小版本由上游静态构建决定（--pin 时从轮子里读出并记录），
        # 目前是 Linux 7.0.2、Windows / macOS 7.1 —— 同一大版本，抓帧用到的参数集完全一致。
        'package': 'imageio-ffmpeg',
        'version': '0.6.0',
        'wheels': {
            'linux-x64': 'manylinux2014_x86_64',
            'win32-x64': 'win_amd64',
            'darwin-x64': 'macosx_10_9_intel.macosx_10_9_x86_64',
            'darwin-arm64': 'macosx_11_0_arm64',
        },
        'assets': {},        # --pin 填：每个平台 {file, url, sha256, member, ffmpeg_version}
    },
    'fonts': [
        # Noto Sans CJK SC Regular（OFL）：Ubuntu 开发机上用的就是 Noto CJK，三平台带同一份，全景标注逐像素一致
        {'file': 'NotoSansCJKsc-Regular.otf',
         'url': 'https://raw.githubusercontent.com/notofonts/noto-cjk/165c01b46ea533872e002e0785ff17e44f6d97d8/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf',
         'sha256': ''},
        {'file': 'LICENSE-NotoSansCJK.txt',
         'url': 'https://raw.githubusercontent.com/notofonts/noto-cjk/165c01b46ea533872e002e0785ff17e44f6d97d8/LICENSE',
         'sha256': ''},
    ],
}


# ── 小工具 ──────────────────────────────────────────────────────────────────
def say(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f'  ✅ {msg}')


def fail(msg: str) -> None:
    say(f'  ❌ {msg}')
    raise SystemExit(1)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def platform_key() -> str:
    m = _platform.machine().lower()
    if IS_WIN:
        if m in ('amd64', 'x86_64'):
            return 'win32-x64'
    elif sys.platform == 'darwin':
        return 'darwin-arm64' if m == 'arm64' else 'darwin-x64'
    elif sys.platform.startswith('linux'):
        if m in ('x86_64', 'amd64'):
            return 'linux-x64'
    fail(f'不支持的平台：{sys.platform} / {m}（支持 Windows x64、macOS arm64/x64、Linux x64）')
    return ''


def download(url: str, dest: Path, *, expect_sha: str = '', retries: int = 3) -> Path:
    """下载到 dest（已存在且哈希对得上就跳过）。先写 .part 再改名，中途断了不会留下半个文件。"""
    if dest.exists() and (not expect_sha or sha256_of(dest) == expect_sha):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + '.part')
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'patrol-scheduler-build-runtime'})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, 'wb') as f:
                total = int(r.headers.get('Content-Length') or 0)
                done = 0
                t0 = time.time()
                for chunk in iter(lambda: r.read(1 << 18), b''):
                    f.write(chunk)
                    done += len(chunk)
                    if total and time.time() - t0 > 2:
                        say(f'     … {dest.name} {done / 1048576:.0f}/{total / 1048576:.0f} MB')
                        t0 = time.time()
            if expect_sha:
                got = sha256_of(part)
                if got != expect_sha:
                    part.unlink(missing_ok=True)
                    fail(f'{dest.name} 哈希不符：期望 {expect_sha[:12]}…，实际 {got[:12]}…（来源被改动？重新 --pin 或检查网络）')
            part.replace(dest)
            return dest
        except (OSError, urllib.error.URLError) as e:          # noqa: PERF203
            last = e
            say(f'     下载失败（第 {attempt} 次）：{e}')
            time.sleep(2 * attempt)
    fail(f'下载 {url} 失败：{last}')
    return dest


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, 'PYTHONNOUSERSITE': '1'}
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    return subprocess.run(cmd, env=env, **kw)


def load_spec() -> dict:
    if LOCK.exists():
        return json.loads(LOCK.read_text(encoding='utf-8'))
    return json.loads(json.dumps(DEFAULT_SPEC))


# ── uv：只用它下载固定版本的 python-build-standalone ────────────────────────
def ensure_uv(cache: Path) -> str:
    uvname = 'uv.exe' if IS_WIN else 'uv'
    tools = cache / 'tools'
    # pip --target 在 Windows 上也把可执行文件放进 bin/（不是 Scripts/），两处都找
    in_tools = [tools / 'bin' / uvname, tools / 'Scripts' / uvname]
    for c in (shutil.which('uv'), Path.home() / '.local' / 'bin' / uvname, Path.home() / '.cargo' / 'bin' / uvname, *in_tools):
        if c and Path(c).is_file():
            return str(c)
    say('  没有 uv，用 pip 装一份到缓存目录（只用来下载 Python）…')
    r = run([sys.executable, '-m', 'pip', 'install', '-q', '--disable-pip-version-check', '--target', str(tools), 'uv'])
    if r.returncode != 0:
        fail('pip 装 uv 失败；手动装：curl -LsSf https://astral.sh/uv/install.sh | sh（Windows：winget install astral-sh.uv）')
    exe = next((p for p in in_tools if p.is_file()), None)
    if exe is None:
        fail(f'装了 uv 但找不到 {" 或 ".join(map(str, in_tools))}')
    return str(exe)


def find_interpreter(pydir: Path, version: str) -> Path | None:
    for d in sorted(pydir.glob(f'cpython-{version}-*')):
        if d.is_symlink() or not d.is_dir():
            continue
        for cand in (d / 'python.exe', d / 'bin' / 'python3', d / 'bin' / 'python'):
            if cand.is_file():
                return cand
    return None


def install_python(dest: Path, cache: Path, version: str) -> Path:
    pydir = dest / 'python'
    interp = find_interpreter(pydir, version)
    if interp is None:
        uv = ensure_uv(cache)
        say(f'  下载 Python {version}（python-build-standalone，经 uv）…')
        # --no-bin：别往 ~/.local/bin 放 python3.12 的快捷方式（那是用户的目录，这份解释器只给 runtime 用）
        flags = ['--no-bin'] + (['--no-registry'] if IS_WIN else [])        # Windows 再加 --no-registry：别把这份私有解释器登记进注册表（PEP 514）
        r = run([uv, 'python', 'install', version, '--install-dir', str(pydir), '--no-progress', *flags])
        if r.returncode != 0:
            r = run([uv, 'python', 'install', version, '--install-dir', str(pydir), '--no-progress'])     # 老版本 uv 没有 --no-bin
        if r.returncode != 0:
            fail('uv python install 失败')
        interp = find_interpreter(pydir, version)
        if interp is None:
            fail(f'装完找不到解释器：{pydir}')
    # uv 会放一个「小版本别名」符号链接（cpython-3.12-… → cpython-3.12.13-…）；打包工具复制时会把它展开成第二份，删掉
    isjunction = getattr(os.path, 'isjunction', lambda _p: False)      # Windows 上 uv 建的是 junction，is_symlink() 看不出来
    for d in pydir.iterdir():
        if d.is_symlink() or isjunction(str(d)):
            try:
                d.unlink()
            except OSError:
                os.rmdir(d)                                           # 目录 junction / 目录符号链接用 rmdir 摘掉，不碰目标
    # uv 给它的解释器加了 EXTERNALLY-MANAGED（PEP 668），意思是「别往系统 Python 里装东西」。
    # 这份解释器是我们私有的运行时、随应用一起分发，装依赖正是它存在的目的，所以把标记去掉。
    for marker in list(interp.parent.parent.glob('lib/python3.*/EXTERNALLY-MANAGED')) + [interp.parent / 'Lib' / 'EXTERNALLY-MANAGED']:
        if marker.exists():
            marker.unlink()
    out = run([str(interp), '-c', 'import sys; print(sys.version.split()[0])'], capture_output=True, text=True)
    if out.returncode != 0 or out.stdout.strip() != version:
        fail(f'解释器版本不对：{out.stdout.strip() or out.stderr.strip()}（期望 {version}）')
    ok(f'Python {version}  {interp.relative_to(dest)}')
    return interp


def install_deps(interp: Path, dev: bool) -> int:
    say('  按 requirements.lock 装依赖…')
    r = run([str(interp), '-m', 'pip', 'install', '-q', '--disable-pip-version-check', '--no-warn-script-location',
             '-r', str(REQ_LOCK)])
    if r.returncode != 0:
        fail('pip install -r requirements.lock 失败')
    if dev:
        r = run([str(interp), '-m', 'pip', 'install', '-q', '--disable-pip-version-check', '--no-warn-script-location',
                 'pytest>=8', 'ruff>=0.6'])
        if r.returncode != 0:
            fail('装 pytest / ruff 失败')
    r = run([str(interp), '-m', 'pip', 'list', '--format=json', '--disable-pip-version-check'], capture_output=True, text=True)
    n = len(json.loads(r.stdout)) if r.returncode == 0 else -1
    ok(f'依赖就绪（{n} 个包{"，含 pytest/ruff" if dev else ""}）')
    return n


def ffmpeg_version(exe: Path) -> str:
    try:
        r = subprocess.run([str(exe), '-version'], capture_output=True, text=True, timeout=20)
        first = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else ''
        return first.split()[2] if first.startswith('ffmpeg version') else ''
    except Exception:          # noqa: BLE001
        return ''


def _wheel_member(wheel: Path) -> tuple[str, str]:
    """轮子里 ffmpeg 二进制的成员名与版本（文件名形如 imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2）。"""
    with zipfile.ZipFile(wheel) as z:
        for n in z.namelist():
            base = n.rsplit('/', 1)[-1]
            if n.startswith('imageio_ffmpeg/binaries/ffmpeg-') and not base.endswith('.md'):
                ver = base.split('-v', 1)[1] if '-v' in base else ''
                return n, ver.removesuffix('.exe')
    fail(f'{wheel.name} 里没有 ffmpeg 二进制')
    return '', ''


def install_ffmpeg(dest: Path, cache: Path, spec: dict, key: str) -> Path:
    exe = dest / 'ffmpeg' / ('ffmpeg.exe' if IS_WIN else 'ffmpeg')
    asset = spec['ffmpeg'].get('assets', {}).get(key)
    if not asset or not asset.get('sha256'):
        fail('runtime.lock.json 里没有本平台 ffmpeg 的哈希，先跑 --pin')
    want = asset['ffmpeg_version']
    if exe.is_file() and ffmpeg_version(exe).startswith(want):
        ok(f'ffmpeg {want} 已就绪')
        return exe
    say(f'  下载 ffmpeg {want}（{asset["file"]}）…')
    wheel = download(asset['url'], cache / asset['file'], expect_sha=asset['sha256'])
    member, _ver = _wheel_member(wheel)
    exe.parent.mkdir(parents=True, exist_ok=True)
    tmp = exe.with_suffix(exe.suffix + '.part')
    with zipfile.ZipFile(wheel) as z, z.open(member) as src, open(tmp, 'wb') as out:
        shutil.copyfileobj(src, out, 1 << 20)
        lic = [n for n in z.namelist() if n.endswith('.dist-info/LICENSE')]
        if lic:
            (exe.parent / 'LICENSE-imageio-ffmpeg.txt').write_bytes(z.read(lic[0]))
    tmp.replace(exe)
    if not IS_WIN:
        exe.chmod(0o755)
    got = ffmpeg_version(exe)
    if not got.startswith(want):
        fail(f'ffmpeg 跑不起来或版本不对：{got!r}（期望 {want}）')
    ok(f'ffmpeg {got}  {exe.relative_to(dest)}')
    return exe


def install_fonts(dest: Path, cache: Path, spec: dict) -> list[str]:
    out = []
    for f in spec['fonts']:
        target = dest / 'fonts' / f['file']
        if not f.get('sha256'):
            fail('runtime.lock.json 里字体还没有哈希，先跑 --pin')
        if not (target.exists() and sha256_of(target) == f['sha256']):
            say(f'  下载字体 {f["file"]}…')
            src = download(f['url'], cache / f['file'], expect_sha=f['sha256'])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        out.append(f['file'])
    ok('字体 ' + ', '.join(n for n in out if not n.lower().startswith('license')))
    return out


def verify(dest: Path, interp: Path, exe: Path) -> dict:
    """解释器能 import 全部关键包、ffmpeg 能跑、字体能被 PIL 加载 —— 三平台都要过的同一组检查。"""
    code = ('import json, sys\n'
            'mods = ["fastapi","uvicorn","starlette","pydantic","requests","PIL","numpy","edge_tts","multipart","anthropic"]\n'
            'vers = {}\n'
            'for m in mods:\n'
            '    mod = __import__(m); vers[m] = getattr(mod, "__version__", "?")\n'
            'from PIL import ImageFont\n'
            f'font = ImageFont.truetype(r"{dest / "fonts" / "NotoSansCJKsc-Regular.otf"}", 20)\n'
            'bbox = font.getbbox("巡检调度")\n'
            'print(json.dumps({"versions": vers, "font_ok": bbox[2] > 40, "python": sys.version.split()[0]}))\n')
    r = run([str(interp), '-c', code], capture_output=True, text=True)
    if r.returncode != 0:
        fail('自检失败：\n' + r.stderr[-2000:])
    info = json.loads(r.stdout.strip().splitlines()[-1])
    if not info['font_ok']:
        fail('字体加载了但渲染不出中文')
    v = ffmpeg_version(exe)
    if not v:
        fail('ffmpeg -version 跑不起来')
    ok('自检通过：' + '  '.join(f'{k} {v}' for k, v in info['versions'].items()))
    return info


def prune(interp: Path) -> None:
    base = interp.parent.parent if not IS_WIN else interp.parent
    lib = next(iter(base.glob('lib/python3.*')), None) if not IS_WIN else base / 'Lib'
    if not lib or not lib.is_dir():
        return
    freed = 0
    for name in ('test', 'idlelib', 'turtledemo', 'tkinter', 'lib2to3'):
        d = lib / name
        if d.is_dir():
            freed += sum(p.stat().st_size for p in d.rglob('*') if p.is_file())
            shutil.rmtree(d, ignore_errors=True)
    ok(f'裁掉标准库里用不到的部分，省 {freed / 1048576:.0f} MB')


def write_manifest(dest: Path, spec: dict, key: str, interp: Path, exe: Path, fonts: list[str], info: dict, packages: int) -> None:
    man = {
        'built_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'platform': key,
        'python': {'version': spec['python']['version'], 'exe': interp.relative_to(dest).as_posix()},
        'ffmpeg': {'version': spec['ffmpeg']['assets'][key]['ffmpeg_version'], 'package': f"{spec['ffmpeg']['package']}=={spec['ffmpeg']['version']}",
                   'exe': exe.relative_to(dest).as_posix()},
        'fonts': fonts,
        'packages': packages,
        'package_versions': info['versions'],
        'requirements_lock_sha256': sha256_of(REQ_LOCK),
        'runtime_lock_sha256': sha256_of(LOCK) if LOCK.exists() else '',
    }
    (dest / 'manifest.json').write_text(json.dumps(man, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    ok(f'manifest.json 已写（{key}）')


# ── 子命令 ──────────────────────────────────────────────────────────────────
def do_pin(cache: Path) -> None:
    spec = load_spec()
    say('重新钉住来源：从 PyPI 取 imageio-ffmpeg 四个平台的轮子（哈希来自 PyPI 元数据），下载后读出 ffmpeg 版本；字体算 sha256…')
    pkg, ver = spec['ffmpeg']['package'], spec['ffmpeg']['version']
    with urllib.request.urlopen(f'https://pypi.org/pypi/{pkg}/{ver}/json', timeout=30) as r:
        files = json.load(r)['urls']
    assets = {}
    for key, tag in spec['ffmpeg']['wheels'].items():
        f = next((f for f in files if f['filename'].endswith(f'-{tag}.whl')), None)
        if f is None:
            fail(f'PyPI 上 {pkg}=={ver} 没有 {tag} 的轮子')
        p = download(f['url'], cache / f['filename'], expect_sha=f['digests']['sha256'])
        member, fver = _wheel_member(p)
        assets[key] = {'file': f['filename'], 'url': f['url'], 'sha256': f['digests']['sha256'], 'member': member, 'ffmpeg_version': fver}
        ok(f'{key:13} ffmpeg {fver:8} {f["filename"]:64} {p.stat().st_size / 1048576:.1f} MB')
    spec['ffmpeg']['assets'] = assets
    for f in spec['fonts']:
        p = download(f['url'], cache / f['file'])
        f['sha256'] = sha256_of(p)
        ok(f'{"font":13} {f["file"]:24} {f["sha256"][:16]}…  {p.stat().st_size / 1048576:.1f} MB')
    spec['pinned_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    LOCK.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    ok(f'写入 {LOCK.relative_to(ROOT)}')


def do_check(dest: Path) -> None:
    man_path = dest / 'manifest.json'
    if not man_path.exists():
        fail(f'{dest} 里没有 manifest.json，先跑 build_runtime.py')
    man = json.loads(man_path.read_text(encoding='utf-8'))
    spec = load_spec()
    interp = dest / man['python']['exe']
    exe = dest / man['ffmpeg']['exe']
    if man['platform'] != platform_key():
        fail(f'runtime 是 {man["platform"]} 的，本机是 {platform_key()}')
    if man['requirements_lock_sha256'] != sha256_of(REQ_LOCK):
        fail('requirements.lock 改过了，runtime 里的依赖已过时：重新跑 build_runtime.py')
    if LOCK.exists() and man.get('runtime_lock_sha256') != sha256_of(LOCK):
        fail('runtime.lock.json 改过了（Python/ffmpeg/字体版本），重新跑 build_runtime.py')
    if man['python']['version'] != spec['python']['version'] or man['ffmpeg']['version'] != spec['ffmpeg']['assets'][man['platform']]['ffmpeg_version']:
        fail('manifest 与 runtime.lock.json 的版本不一致')
    verify(dest, interp, exe)
    ok(f'runtime 就绪：{man["platform"]}  Python {man["python"]["version"]}  ffmpeg {man["ffmpeg"]["version"]}  '
       f'{man["packages"]} 个包  built {man["built_at"]}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dest', default=str(ROOT / 'runtime'))
    ap.add_argument('--dev', action='store_true', help='另装 pytest / ruff')
    ap.add_argument('--check', action='store_true', help='只核对，不联网')
    ap.add_argument('--pin', action='store_true', help='重新生成 runtime.lock.json')
    ap.add_argument('--prune', action='store_true', help='裁掉解释器里用不到的标准库大头')
    a = ap.parse_args()
    dest = Path(a.dest).resolve()
    cache = dest.parent / (dest.name + '.cache')
    cache.mkdir(parents=True, exist_ok=True)
    if a.pin:
        do_pin(cache)
        return 0
    if a.check:
        do_check(dest)
        return 0
    spec = load_spec()
    key = platform_key()
    say(f'组装 runtime → {dest}（{key}）')
    dest.mkdir(parents=True, exist_ok=True)
    interp = install_python(dest, cache, spec['python']['version'])
    packages = install_deps(interp, a.dev)
    exe = install_ffmpeg(dest, cache, spec, key)
    fonts = install_fonts(dest, cache, spec)
    if a.prune:
        prune(interp)
    info = verify(dest, interp, exe)
    write_manifest(dest, spec, key, interp, exe, fonts, info, packages)
    size = sum(p.stat().st_size for p in dest.rglob('*') if p.is_file()) / 1048576
    say(f'完成：{dest}  共 {size:.0f} MB。后端这样跑：{interp} -m app.main   （桌面壳会自动找到它）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
