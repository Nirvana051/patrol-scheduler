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
    assert h['threads'] == {'status_poller': True, 'events_listener': True}
    assert 'vlm' in h['adapters'] and h['media_bytes'] >= 0


def test_cleanup_script_dry_run(app_client, tmp_path):
    import subprocess, sys, os
    env = {**os.environ, 'PS_DATA_DIR': str(app_client.ctx.cfg.data_dir), 'PS_DB_PATH': str(app_client.ctx.db.path)}
    r = subprocess.run([sys.executable, 'scripts/cleanup_media.py', '--keep-days', '0', '--events', '--dry-run'],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert '完成' in r.stdout and '[dry]' in r.stdout
