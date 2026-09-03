#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线备份 SQLite（用 sqlite3 的 backup API，不用 cp——WAL 模式下直接拷文件可能不一致）。

    .venv/bin/python scripts/backup_db.py [--out data/backups] [--keep 14]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402


def backup(db_path: Path, out_dir: Path, keep: int = 14) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"scheduler-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(dst))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    olds = sorted(out_dir.glob('scheduler-*.db'))
    for p in olds[:-keep] if keep > 0 else []:
        p.unlink()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    ap.add_argument('--keep', type=int, default=14)
    a = ap.parse_args()
    cfg = Config()
    out = Path(a.out) if a.out else cfg.data_dir / 'backups'
    dst = backup(cfg.db_path, out, a.keep)
    print(f'{dst}  {dst.stat().st_size / 1024:.0f} KB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
