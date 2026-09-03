import time

from app.robot.client import RateLimiter


def test_rate_limiter_paces_requests():
    rl = RateLimiter(rate=10.0, burst=2)
    t0 = time.monotonic()
    for _ in range(7):
        rl.acquire()
    elapsed = time.monotonic() - t0
    assert 0.4 <= elapsed <= 1.5, elapsed          # 2 个突发 + 5 个按 10/s 排队 ≈ 0.5s
    assert rl.total == 7


def test_health_endpoint_is_a_self_check(app_client):
    h = app_client.get('/api/health').json()
    assert h['ok'] and h['db_version'] >= 2 and len(h['instance_id']) == 6
    assert h['threads'] == {'status_poller': True, 'events_listener': True, 'scheduler': True}
    assert 'vlm' in h['adapters'] and h['media_bytes'] >= 0


def test_cleanup_script_dry_run(app_client, tmp_path):
    import subprocess, sys, os
    env = {**os.environ, 'PS_DATA_DIR': str(app_client.ctx.cfg.data_dir), 'PS_DB_PATH': str(app_client.ctx.db.path)}
    r = subprocess.run([sys.executable, 'scripts/cleanup_media.py', '--keep-days', '0', '--events', '--dry-run'],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert '完成' in r.stdout and '[dry]' in r.stdout


def test_db_backup_script(app_client, tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('backup_db', Path(__file__).resolve().parent.parent / 'scripts' / 'backup_db.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / 'bk'
    p1 = mod.backup(app_client.ctx.db.path, out, keep=1)
    import time as _t
    _t.sleep(1.1)
    p2 = mod.backup(app_client.ctx.db.path, out, keep=1)
    assert p2.exists() and not p1.exists()                    # 只保留最近 1 份
    import sqlite3
    c = sqlite3.connect(p2)
    assert c.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] >= 4
    assert c.execute("SELECT value FROM settings WHERE key='PS_INSTANCE_ID'").fetchone()[0] == app_client.ctx.instance_id
