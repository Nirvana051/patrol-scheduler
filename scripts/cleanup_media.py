#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理旧的执行媒体（全景/裁切/TTS 音频）与旧事件，防止 data/ 无限增长。

    .venv/bin/python scripts/cleanup_media.py --keep-days 30 [--events] [--dry-run]

只删 runs/<id>/ 下已结束且早于 keep-days 的执行目录，以及 tts/、snapshots/、tests/ 下同样过期的文件；
inspections 表里的路径保留（界面会显示「无图」）。--events 同时删除早于 keep-days 的事件行。
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep-days', type=float, default=30)
    ap.add_argument('--events', action='store_true', help='同时删除过期事件行')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    cfg = Config()
    db = Database(cfg.db_path)
    cfg.db = db
    cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=a.keep_days)
    cutoff_ts = cutoff.timestamp()
    freed, removed = 0, 0

    runs_dir = cfg.media_dir / 'runs'
    if runs_dir.exists():
        for d in runs_dir.iterdir():
            if not d.is_dir() or not d.name.isdigit():
                continue
            run = db.query_one('SELECT status, ended_at FROM runs WHERE id=?', (int(d.name),))
            if run and run['status'] in ('pending', 'preflight', 'running', 'paused'):
                continue
            ended = run['ended_at'] if run and run['ended_at'] else None
            old = (dt.datetime.fromisoformat(ended) < cutoff) if ended else (d.stat().st_mtime < cutoff_ts)
            if old:
                size = sum(p.stat().st_size for p in d.rglob('*') if p.is_file())
                print(f"{'[dry] ' if a.dry_run else ''}删除执行媒体 run {d.name}（{size / 1e6:.1f} MB）")
                if not a.dry_run:
                    shutil.rmtree(d, ignore_errors=True)
                freed += size
                removed += 1
    for sub in ('tts', 'snapshots', 'tests'):
        p = cfg.media_dir / sub
        if not p.exists():
            continue
        for f in p.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff_ts:
                freed += f.stat().st_size
                removed += 1
                if not a.dry_run:
                    f.unlink()
    if a.events:
        n = db.query_one('SELECT COUNT(*) AS n FROM events WHERE ts < ?', (cutoff.isoformat(),))['n']
        print(f"{'[dry] ' if a.dry_run else ''}删除 {n} 条早于 {cutoff:%Y-%m-%d} 的事件")
        if not a.dry_run and n:
            db.execute('DELETE FROM events WHERE ts < ?', (cutoff.isoformat(),))
            db.conn().execute('VACUUM')
    print(f'完成：{removed} 项，释放 {freed / 1e6:.1f} MB（{time.strftime("%F %T")}）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
