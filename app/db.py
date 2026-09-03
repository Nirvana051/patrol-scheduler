# -*- coding: utf-8 -*-
"""SQLite 访问层：每线程一个连接、WAL、schema.sql 迁移、几个小助手。刻意不用 ORM。"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = Path(__file__).resolve().parent / 'schema.sql'
SCHEMA_VERSION = 2


def now_iso() -> str:
    """本地时间、带时区偏移（如 2026-09-04T01:00:00.000+08:00），跨机器对账不歧义。"""
    return _dt.datetime.now().astimezone().isoformat(timespec='milliseconds')


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == '':
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.write_lock = threading.RLock()
        self.migrate()

    # ── 连接 ────────────────────────────────────────────────────────────────
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, 'conn', None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=NORMAL')
            c.execute('PRAGMA foreign_keys=ON')
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, 'conn', None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def transaction(self):
        with self.write_lock:
            c = self.conn()
            c.execute('BEGIN')
            try:
                yield c
                c.execute('COMMIT')
            except Exception:
                c.execute('ROLLBACK')
                raise

    # ── 查询 ────────────────────────────────────────────────────────────────
    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable = ()) -> dict | None:
        r = self.conn().execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    def execute(self, sql: str, params: Iterable = ()) -> int:
        with self.write_lock:
            cur = self.conn().execute(sql, tuple(params))
            return cur.lastrowid or cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Iterable]) -> None:
        with self.write_lock:
            self.conn().executemany(sql, [tuple(r) for r in rows])

    # ── 迁移 ────────────────────────────────────────────────────────────────
    def migrate(self) -> None:
        """空库：一次性建当前版本的全部表。旧库：按版本逐步 ALTER。"""
        c = self.conn()
        c.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)')
        row = c.execute('SELECT MAX(version) AS v FROM schema_version').fetchone()
        v = row['v'] or 0
        if v == 0:
            c.executescript(SCHEMA.read_text(encoding='utf-8'))
            c.execute('INSERT INTO schema_version(version) VALUES (?)', (SCHEMA_VERSION,))
            return
        if v < 2:
            c.execute('ALTER TABLE run_legs ADD COLUMN item_seq INTEGER')
            c.execute('INSERT INTO schema_version(version) VALUES (2)')

    def version(self) -> int:
        row = self.conn().execute('SELECT MAX(version) AS v FROM schema_version').fetchone()
        return int(row['v'] or 0)

    # ── settings ────────────────────────────────────────────────────────────
    def get_setting(self, key: str) -> str | None:
        r = self.query_one('SELECT value FROM settings WHERE key=?', (key,))
        return r['value'] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute('INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                     (key, value, now_iso()))

    def all_settings(self) -> dict[str, str]:
        return {r['key']: r['value'] for r in self.query('SELECT key, value FROM settings')}

    # ── 事件 ────────────────────────────────────────────────────────────────
    def add_event(self, source: str, etype: str, *, message: str = '', data: Any = None,
                  level: str = 'info', cloud_seq: int | None = None, run_id: int | None = None,
                  leg_id: int | None = None, ts: str | None = None) -> int | None:
        """写一条事件。云端事件按 cloud_seq 去重（重连补漏时会重复收到）。返回 id；重复返回 None。"""
        try:
            return self.execute(
                'INSERT INTO events(ts,source,type,cloud_seq,run_id,leg_id,level,message,data) VALUES(?,?,?,?,?,?,?,?,?)',
                (ts or now_iso(), source, etype, cloud_seq, run_id, leg_id, level, message, dumps(data or {})))
        except sqlite3.IntegrityError:
            return None
