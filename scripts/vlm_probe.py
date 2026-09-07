#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接一个新的 VLM 服务商时，用这个脚本先把「一张图 + 一句问题 → 是/不是」跑通。

    # 用当前 config/.env 的配置，拿最近一次真机检查的裁切图问一句
    .venv/bin/python scripts/vlm_probe.py

    # 指定图片与问题；--provider/--model/--base-url/--key 可临时覆盖配置（不写回 .env）
    .venv/bin/python scripts/vlm_probe.py --image data/media/runs/112/leg01_tw6_211757_crop.jpg \
        --prompt '画面中是否有一根红色的圆柱体？' --provider qwen --model qwen3.5-flash --key sk-xxx

打印：实际发出的请求要点、原始回复、解析出的 yes/no/unknown、耗时。失败时打印服务端的错误原文。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.vlm.base import SYSTEM_PROMPT, build_provider, build_user_prompt  # noqa: E402


def latest_crop(cfg: Config) -> Path | None:
    """最近一次检查的裁切图；没有就退回任意参考图。"""
    try:
        db = Database(cfg.db_path)
        r = db.query_one('SELECT crop_path FROM inspections WHERE crop_path IS NOT NULL ORDER BY id DESC LIMIT 1')
        if r:
            p = cfg.media_dir / r['crop_path']
            if p.exists():
                return p
    except Exception:      # noqa: BLE001 —— 没库也能用
        pass
    refs = sorted((cfg.media_dir / 'refs').glob('*.jpg')) if (cfg.media_dir / 'refs').exists() else []
    return refs[-1] if refs else None


def main() -> int:
    ap = argparse.ArgumentParser(description='VLM 连通性/判读探针')
    ap.add_argument('--image', default=None, help='图片路径；默认取最近一次检查的裁切图')
    ap.add_argument('--prompt', default='画面中是否有一根红色的圆柱体？', help='检查问题（必须能用「是/不是」回答）')
    ap.add_argument('--angle-from', type=float, default=150.0)
    ap.add_argument('--angle-to', type=float, default=210.0)
    ap.add_argument('--provider', default=None, help='临时覆盖 VLM_PROVIDER：mock|qwen|openai_compat|anthropic')
    ap.add_argument('--model', default=None)
    ap.add_argument('--base-url', default=None)
    ap.add_argument('--key', default=None)
    ap.add_argument('--extra-body', default=None, help='JSON 对象，合并进请求体')
    ap.add_argument('--full-pano', action='store_true', help='除裁切图外再附整张全景（同一张图重复发一次，仅用于测多图）')
    a = ap.parse_args()

    cfg = Config()
    for env_key, val in (('VLM_PROVIDER', a.provider), ('VLM_MODEL', a.model),
                         ('VLM_BASE_URL', a.base_url), ('VLM_API_KEY', a.key), ('VLM_EXTRA_BODY', a.extra_body)):
        if val is not None:
            os.environ[env_key] = val
    cfg = Config()                       # 重新读，让覆盖生效（不写 settings 表，不改 .env）

    img = Path(a.image) if a.image else latest_crop(cfg)
    if img is None or not img.exists():
        print('找不到可用图片：用 --image 指定，或先在网页上抓一张参考图', file=sys.stderr)
        return 2
    data = img.read_bytes()
    vlm = build_provider(cfg)
    images = [(data, 'image/jpeg')] + ([(data, 'image/jpeg')] if a.full_pano else [])
    user = build_user_prompt(a.prompt, a.angle_from, a.angle_to,
                             forward_deg=cfg.get_float('FORWARD_DEG'), waypoint_name='探针')

    print(f'提供方 : {vlm.describe()}')
    print(f'密钥   : {"已配置（" + (cfg.get("VLM_API_KEY")[:6] + "…" if cfg.get("VLM_API_KEY") else "") + "）" if cfg.get("VLM_API_KEY") else "未配置"}')
    print(f'额外字段: {getattr(vlm, "extra_body", {}) or "—"}')
    print(f'图片   : {img}（{len(data) / 1024:.0f} KB，{len(images)} 张）')
    print(f'问题   : {a.prompt}')
    print('-' * 60)
    res = vlm.ask_yes_no(images, user, system=SYSTEM_PROMPT)
    print(f'解析结果: {res.answer}   耗时 {res.latency_ms} ms')
    if res.error:
        print(f'错误    : {res.error}')
    print(f'原始回复: {res.raw[:1000] or "（空）"}')
    if res.extra:
        print(f'附加    : {res.extra}')
    print('-' * 60)
    if res.answer in ('yes', 'no'):
        print('✅ 通了。接下来在网页「任务航点」里逐点用「试问 VLM」调 prompt 与角度范围。')
        return 0
    if res.answer == 'unknown':
        print('⚠️  模型答了但没给出是/不是：改 prompt（更具体地指名目标）或收窄角度范围；也可能这张图里确实看不到目标。')
        return 0
    print('❌ 调用失败，按上面的错误原文排查：密钥、模型名、base_url、该模型是否支持图片输入。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
