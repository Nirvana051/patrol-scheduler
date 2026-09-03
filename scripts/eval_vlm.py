#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VLM 判读评测：用已人工复核的检查记录（human_passed 不为空）当标注，重新让当前配置的 VLM 判一遍，算准确率。

    .venv/bin/python scripts/eval_vlm.py [--days 30] [--limit 200] [--provider openai_compat] [--json out.json]

样本 = inspections 里 human_passed 非空的记录（裁切图 crop_path + prompt + 期望值）。
真值 = 人工复核结果（通过/不通过 → 结合 expected 反推「是/不是」）。
输出：总体准确率、按航点的准确率、误判清单。用它比较不同 provider / prompt / 是否附整图的效果（R3-7）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.db import Database, loads  # noqa: E402
from app.vlm.base import SYSTEM_PROMPT, build_provider, build_user_prompt  # noqa: E402


def truth_answer(expected: str, human_passed: int) -> str:
    """人工判「通过」= 模型应答 expected；判「不通过」= 应答相反。"""
    exp = 'yes' if (expected or 'yes') == 'yes' else 'no'
    if human_passed == 1:
        return exp
    return 'no' if exp == 'yes' else 'yes'


def evaluate(db: Database, cfg: Config, *, days: int = 30, limit: int = 200, provider=None) -> dict:
    rows = db.query("SELECT i.*, tw.angle_from AS tw_from, tw.angle_to AS tw_to FROM inspections i "
                    "LEFT JOIN task_waypoints tw ON tw.id=i.task_waypoint_id "
                    "WHERE i.human_passed IS NOT NULL AND i.crop_path IS NOT NULL AND i.created_at >= datetime('now','localtime', ?) "
                    "ORDER BY i.id DESC LIMIT ?", (f'-{days} days', limit))
    vlm = provider or build_provider(cfg)
    media = cfg.media_dir
    forward = cfg.get_float('FORWARD_DEG')
    results = []
    for r in rows:
        crop = media / r['crop_path']
        if not crop.exists():
            continue
        images = [(crop.read_bytes(), 'image/jpeg')]
        if cfg.get_bool('VLM_SEND_FULL_PANO') and r.get('image_path') and (media / r['image_path']).exists():
            images.append(((media / r['image_path']).read_bytes(), 'image/jpeg'))
        user = build_user_prompt(r['prompt'] or '', float(r['angle_from'] or 0), float(r['angle_to'] or 360), forward_deg=forward,
                                 waypoint_name=r['waypoint_name'] or '')
        res = vlm.ask_yes_no(images, user, system=SYSTEM_PROMPT)
        truth = truth_answer(r.get('expected'), int(r['human_passed']))
        results.append({'inspection_id': r['id'], 'waypoint': r['waypoint_name'], 'truth': truth, 'predicted': res.answer,
                        'correct': res.answer == truth, 'latency_ms': res.latency_ms, 'raw': (res.raw or '')[:200], 'error': res.error})
    n = len(results)
    correct = sum(1 for x in results if x['correct'])
    by_wp: dict[str, dict] = {}
    for x in results:
        b = by_wp.setdefault(x['waypoint'], {'n': 0, 'correct': 0})
        b['n'] += 1
        b['correct'] += int(x['correct'])
    return {'provider': vlm.describe(), 'samples': n, 'correct': correct, 'accuracy': (correct / n) if n else None,
            'by_waypoint': {k: {**v, 'accuracy': v['correct'] / v['n']} for k, v in by_wp.items()},
            'errors': [x for x in results if not x['correct']],
            'avg_latency_ms': int(sum(x['latency_ms'] for x in results) / n) if n else 0, 'ts': time.strftime('%F %T')}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--limit', type=int, default=200)
    ap.add_argument('--provider', default=None, help='临时覆盖 VLM_PROVIDER（mock/openai_compat/anthropic）')
    ap.add_argument('--json', default=None, help='把完整结果写到这个文件')
    a = ap.parse_args()
    cfg = Config()
    db = Database(cfg.db_path)
    cfg.db = db
    provider = None
    if a.provider:
        import os
        os.environ['VLM_PROVIDER'] = a.provider
        cfg.db = None                      # 让环境变量覆盖 settings 表
        provider = build_provider(cfg)
        cfg.db = db
    rep = evaluate(db, cfg, days=a.days, limit=a.limit, provider=provider)
    acc = '—' if rep['accuracy'] is None else f"{rep['accuracy']:.1%}"
    print(f"VLM: {rep['provider']}\n样本 {rep['samples']}（有人工复核的检查）  正确 {rep['correct']}  准确率 {acc}  平均耗时 {rep['avg_latency_ms']} ms")
    for wp, v in rep['by_waypoint'].items():
        print(f"  {wp:<16} {v['correct']}/{v['n']}  {v['accuracy']:.0%}")
    if rep['errors']:
        print('误判：')
        for e in rep['errors'][:20]:
            print(f"  #{e['inspection_id']} {e['waypoint']}: 真值 {e['truth']} / 模型 {e['predicted']}  {e['raw'][:80]}")
    if a.json:
        Path(a.json).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'已写 {a.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
