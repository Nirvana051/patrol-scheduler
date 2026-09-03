# -*- coding: utf-8 -*-
"""统计：按任务航点的检查通过率、最近执行概况。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ctx_of

router = APIRouter(prefix='/api/stats', tags=['stats'])


@router.get('')
def stats(request: Request, days: int = 30):
    c = ctx_of(request)
    since = f"-{max(1, min(days, 3650))} days"
    per_wp = c.db.query(
        "SELECT task_waypoint_id, waypoint_name, COUNT(*) AS total, "
        "SUM(CASE WHEN passed=1 THEN 1 ELSE 0 END) AS passed, SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN passed IS NULL THEN 1 ELSE 0 END) AS unknown, MAX(created_at) AS last_at, AVG(latency_ms) AS avg_latency_ms "
        "FROM inspections WHERE created_at >= datetime('now', 'localtime', ?) GROUP BY task_waypoint_id, waypoint_name ORDER BY failed DESC, total DESC", (since,))
    for r in per_wp:
        r['pass_rate'] = round(r['passed'] / r['total'], 3) if r['total'] else None
        r['avg_latency_ms'] = int(r['avg_latency_ms'] or 0)
    runs = c.db.query("SELECT status, COUNT(*) AS n FROM runs WHERE started_at >= datetime('now', 'localtime', ?) GROUP BY status", (since,))
    recent_fail = c.db.query("SELECT id, run_id, waypoint_name, prompt, answer, created_at FROM inspections WHERE passed=0 ORDER BY id DESC LIMIT 10")
    return {'days': days, 'per_waypoint': per_wp, 'runs': {r['status']: r['n'] for r in runs},
            'recent_failures': recent_fail,
            'totals': {'inspections': sum(r['total'] for r in per_wp), 'passed': sum(r['passed'] for r in per_wp),
                       'failed': sum(r['failed'] for r in per_wp), 'unknown': sum(r['unknown'] for r in per_wp)}}
