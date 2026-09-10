#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档体检：把 docs/ 里写的东西跟代码实际情况对一遍，防止文档悄悄过时。

    .venv/bin/python scripts/check_docs.py        # 或 make docs-check

检查项：
  1. 文档间的相对链接与截图路径是否存在
  2. 文档里提到的 `make xxx` 目标是否真的在 Makefile 里
  3. 文档里提到的脚本/目录路径是否存在
  4. 用例数、schema 版本、版本号这类会漂移的数字是否与代码一致
  5. 文档里引用的任务选项名与设置项名是否真的存在

DEVLOG / CHANGELOG / task.md / TODO.md 里的数字是**历史记录**（「当时是 89 个用例」是对的），
所以数字类检查跳过它们，只检查它们的链接与路径。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL = {'DEVLOG.md', 'CHANGELOG.md', 'task.md', 'TODO.md'}      # 数字是历史记录，不比对


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from app.config import RUNTIME_KEYS
    from app.executor.runner import DEFAULT_OPTIONS

    docs = sorted((ROOT / 'docs').glob('*.md')) + [ROOT / 'README.md', ROOT / 'CHANGELOG.md', ROOT / 'deploy/README.md']
    makefile = (ROOT / 'Makefile').read_text(encoding='utf-8')
    # 用例数用 **pytest 自己收集到的条数**，而不是数 `def test_` 的个数 ——
    # 有 @pytest.mark.parametrize 时两者不等（一个函数会跑成多条），
    # 那样文档里的数字就和 `make test` 打出来的对不上（实测 131 个函数 → 138 条）。
    _co = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q'],
                         cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, 'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'})
    # `-q` 的输出是每个文件一行「tests/test_x.py: 20」，加起来就是收集到的总条数
    tests = sum(int(n) for n in re.findall(r'^tests/\S+\.py: (\d+)$', _co.stdout, re.M))
    schema = re.search(r'SCHEMA_VERSION = (\d+)', (ROOT / 'app/db.py').read_text(encoding='utf-8')).group(1)

    problems: list[str] = []

    # 版本号只有一个来源（app/__init__.py 的 __version__），CHANGELOG 的最新条目必须与它一致。
    # 原来 app/main.py 里硬编码 version='0.1.0'，从 v0.1 一路漂到 v0.4.8 都没人发现 ——
    # /api/health 一直报 0.1.0，升级后想确认「跑的是哪一版」会被它骗。这条就是钉住它。
    from app import __version__ as code_version
    top = re.search(r'^## \[([0-9][^\]]*)\]', (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'), re.M)
    if not top:
        problems.append('CHANGELOG.md: 找不到形如 `## [x.y.z]` 的最新条目')
    elif top.group(1) != code_version:
        problems.append(f'版本号不一致：app/__init__.py 是 {code_version}，CHANGELOG 最新条目是 {top.group(1)}')
    for d in docs:
        s = d.read_text(encoding='utf-8')
        rel = d.relative_to(ROOT)
        for link in re.findall(r'\]\(([A-Za-z0-9_./-]+\.md)\)', s):
            if not (d.parent / link).resolve().exists():
                problems.append(f'{rel}: 死链 {link}')
        for img in re.findall(r'\]\((screenshots/[A-Za-z0-9_.-]+)\)', s):
            if not (ROOT / 'docs' / img).exists():
                problems.append(f'{rel}: 缺图 {img}')
        for target in sorted(set(re.findall(r'make ([a-z-]+)', s))):
            if f'\n{target}:' not in makefile:
                problems.append(f'{rel}: make {target} 不存在')
        for path in sorted(set(re.findall(r'`(scripts/[a-z_]+\.py|deploy/[a-z-]+\.(?:service|md)|audio_server|config/env\.example|start\.sh|run\.sh|bootstrap\.sh|requirements\.lock)`', s))):
            if not (ROOT / path).exists():
                problems.append(f'{rel}: 路径 {path} 不存在')
        for opt in sorted(set(re.findall(r'`(start_node|start_node_max_distance|settle_seconds|leg_timeout|not_started_timeout|'
                                         r'offline_timeout|stall_timeout|stop_wait_seconds|estop_if_stop_unconfirmed|max_retries|'
                                         r'return_to_start|require_localized|lost_localization_action|lease_retry_seconds|lease_retries)`', s))):
            if opt not in DEFAULT_OPTIONS:
                problems.append(f'{rel}: 任务选项 {opt} 不存在')
        for key in sorted(set(re.findall(r'`(CX_[A-Z_]+|VLM_[A-Z_]+|TTS_[A-Z_]+|SNAPSHOT_SOURCE|FORWARD_DEG|'
                                         r'NOTIFY_WEBHOOK_URL|SCHEDULE_TICK_SECONDS|ESTOP_ON_EXCEPTION|RATE_LIMIT_RPS)`', s))):
            if key not in RUNTIME_KEYS:
                problems.append(f'{rel}: 设置项 {key} 不存在（可改的只有 RUNTIME_KEYS 里那些）')
        if d.name not in HISTORICAL:
            for n in re.findall(r'(\d+) 用例', s):
                if int(n) != tests:
                    problems.append(f'{rel}: 写着 {n} 用例，实际 {tests}')
            for v in re.findall(r'schema \*?\*?v(\d+)', s):
                if v != schema:
                    problems.append(f'{rel}: 写着 schema v{v}，实际 v{schema}')

    print(f'代码实际：v{code_version} · {tests} 用例 · schema v{schema} · {len(docs)} 篇文档')
    if problems:
        print('❌ 需要修：')
        for p in problems:
            print('  ', p)
        return 1
    print('✅ 链接、截图、make 目标、脚本路径、任务选项、设置项、用例数、schema 版本全部一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
