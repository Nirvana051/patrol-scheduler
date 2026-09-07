#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档体检：把 docs/ 里写的东西跟代码实际情况对一遍，防止文档悄悄过时。

    .venv/bin/python scripts/check_docs.py        # 或 make docs-check

检查项：
  1. 文档间的相对链接与截图路径是否存在
  2. 文档里提到的 `make xxx` 目标是否真的在 Makefile 里
  3. 文档里提到的脚本/目录路径是否存在
  4. 用例数、schema 版本这类会漂移的数字是否与代码一致
  5. 文档里引用的任务选项名与设置项名是否真的存在

DEVLOG / CHANGELOG / task.md / TODO.md 里的数字是**历史记录**（「当时是 89 个用例」是对的），
所以数字类检查跳过它们，只检查它们的链接与路径。
"""
from __future__ import annotations

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
    tests = int(subprocess.run(['bash', '-c', "grep -c '^def test_\\|^    def test_' tests/*.py | awk -F: '{s+=$2} END {print s}'"],
                               cwd=ROOT, capture_output=True, text=True).stdout.strip() or 0)
    schema = re.search(r'SCHEMA_VERSION = (\d+)', (ROOT / 'app/db.py').read_text(encoding='utf-8')).group(1)

    problems: list[str] = []
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
        for path in sorted(set(re.findall(r'`(scripts/[a-z_]+\.py|deploy/[a-z-]+\.(?:service|md)|audio_server|config/env\.example|start\.sh)`', s))):
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

    print(f'代码实际：{tests} 用例 · schema v{schema} · {len(docs)} 篇文档')
    if problems:
        print('❌ 需要修：')
        for p in problems:
            print('  ', p)
        return 1
    print('✅ 链接、截图、make 目标、脚本路径、任务选项、设置项、用例数、schema 版本全部一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
