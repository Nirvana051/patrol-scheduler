# -*- coding: utf-8 -*-
"""到点后的检查流水线：抓全景 → 按角度范围裁切 → VLM 判读 → 按答案模版选 TTS 文本 → 播报 → 落库。"""
from __future__ import annotations

import io
import time

from PIL import Image

from app.db import dumps, loads, now_iso
from app.media import pano
from app.media.snapshot import SnapshotError, save_jpeg
from app.vlm.base import SYSTEM_PROMPT, VlmResult, build_user_prompt

DEFAULT_TEMPLATE = {'expected': 'yes', 'on_pass': '{name}检查通过。', 'on_fail': '注意，{name}检查未通过，请处理。',
                    'on_unknown': '{name}无法判断，请人工复核。'}


def pick_tts(template: dict, answer: str, name: str) -> tuple[str | None, bool | None]:
    tpl = {**DEFAULT_TEMPLATE, **(template or {})}
    expected = str(tpl.get('expected') or 'yes').lower()
    passed: bool | None
    if answer in ('yes', 'no'):
        passed = (answer == expected)
        text = tpl.get('on_pass') if passed else tpl.get('on_fail')
    else:
        passed = None
        text = tpl.get('on_unknown')
    if text:
        text = str(text).replace('{name}', name or '').replace('{answer}', answer)
    return (text or None), passed


def collect_tts_texts(tws) -> list[str]:
    """一批任务航点在三种答案下会播报的全部句子（去重、去空）—— 任务开始时交给 TtsService.prewarm()。"""
    out: list[str] = []
    for tw in tws:
        if not tw:
            continue
        template = tw.get('answer_template')
        if isinstance(template, str):
            template = loads(template, {})
        name = tw.get('name') or f"航点{tw.get('nav_node_id') or tw.get('id')}"
        for answer in ('yes', 'no', 'unknown'):
            text, _ = pick_tts(template or {}, answer, name)
            if text and text not in out:
                out.append(text)
    return out


def _fmt_stages(stages: dict) -> str:
    parts = []
    if stages.get('pre_ms') is not None:
        parts.append(f"到点后等 {stages['pre_ms'] / 1000:.1f} s")
    if 'grab_ms' in stages:
        parts.append(f"抓帧 {stages['grab_ms'] / 1000:.1f} s" + ('(常驻流)' if stages.get('grab_source') == 'live' else ''))
    if 'vlm_ms' in stages:
        parts.append(f"判读 {stages['vlm_ms'] / 1000:.1f} s")
    if 'tts_ms' in stages:
        parts.append(f"播报 {stages['tts_ms'] / 1000:.1f} s" + ('(缓存)' if stages.get('tts_cache_hit') else ''))
    return ' · '.join(parts)


def run_inspection(ctx, run_id: int, leg: dict, tw: dict, *, pad_deg: float = 5.0) -> dict:
    """返回 inspections 表的新行（dict）。任何一步失败都会记录进行，不抛出——检查失败不应中断巡检。

    每一步的耗时记在 row['stages']（不落库，随 `tts` 事件与 SSE 推出去），一眼能看出慢在哪。"""
    t0 = time.time()
    stages: dict = {}
    if leg.get('arrived_at'):
        try:
            import datetime as _dt
            stages['pre_ms'] = max(0, int((t0 - _dt.datetime.fromisoformat(leg['arrived_at']).timestamp()) * 1000))
        except (TypeError, ValueError):
            pass
    name = tw.get('name') or f"航点{tw.get('nav_node_id') or tw.get('id')}"
    af, at = float(tw.get('angle_from', 0)), float(tw.get('angle_to', 360))
    forward = ctx.cfg.get_float('FORWARD_DEG')
    template = tw.get('answer_template')
    if isinstance(template, str):
        template = loads(template, {})
    row = {'run_id': run_id, 'leg_id': leg.get('id'), 'task_waypoint_id': tw.get('id'), 'waypoint_name': name,
           'prompt': tw.get('prompt') or '', 'angle_from': af, 'angle_to': at, 'image_path': None, 'crop_path': None,
           'vlm_provider': ctx.vlm.describe(), 'vlm_raw': '', 'answer': 'error', 'expected': (template or {}).get('expected', 'yes'),
           'passed': None, 'tts_text': None, 'tts_audio_path': None, 'tts_status': None, 'latency_ms': 0, 'capture_pose': None,
           'frame_score': None, 'frame_attempts': None, 'stages': stages}
    run_dir = ctx.media_dir / 'runs' / str(run_id)
    stem = f"leg{int(leg.get('seq', 0)):02d}_tw{tw.get('id')}_{time.strftime('%H%M%S')}"

    # 1. 抓图（顺手记下此刻位姿：机头 yaw 对校准角度范围有用）
    try:
        pose = ctx.status.get().get('position') if ctx.status else None
        row['capture_pose'] = dumps(pose) if pose else None
        ctx.log_event('snapshot', f'{name}：抓取全景', run_id=run_id, leg_id=leg.get('id'), data={'pose': pose})
        grab_ctx: dict = {'waypoint': tw, 'run_id': run_id}
        t_grab = time.time()
        data = ctx.snapshot.grab(grab_ctx)
        stages['grab_ms'] = int((time.time() - t_grab) * 1000)
        stages['grab_source'] = (grab_ctx.get('grab') or {}).get('source', 'oneshot')
        # 抓帧体检的结果（只有 ffmpeg 抓真实流的源会写；synthetic/file 不体检）
        qc = grab_ctx.get('quality') or {}
        row['frame_score'] = qc.get('detail')
        row['frame_attempts'] = qc.get('attempts')
        p_full = save_jpeg(data, run_dir, f'{stem}_pano')
        row['image_path'] = p_full.relative_to(ctx.media_dir).as_posix()
        img = Image.open(io.BytesIO(data)).convert('RGB')
        if qc and not qc.get('ok'):
            # 重抓过 attempts 次仍不合格：图留下来给人看，但**不拿它去问模型** ——
            # 糊图上的判读没有意义，而且每次都是一次真金白银的付费调用。
            row['answer'] = 'error'
            row['vlm_raw'] = (f"抓帧体检不合格，已跳过判读：{qc.get('reason')}"
                              f"（试了 {qc.get('attempts')} 次）")
            ctx.log_event('snapshot_degraded',
                          f"{name}：抓到的画面不可用，跳过判读 —— {qc.get('reason')}（试了 {qc.get('attempts')} 次）",
                          level='error', run_id=run_id, leg_id=leg.get('id'), data=qc)
            return _finish(ctx, row, template, name, t0)
        if qc.get('attempts', 1) > 1:
            ctx.log_event('snapshot_retried',
                          f"{name}：第 1 次抓到的画面没通过体检，重抓第 {qc['attempts']} 次才用上",
                          level='warn', run_id=run_id, leg_id=leg.get('id'), data=qc)
    except (SnapshotError, OSError) as e:
        row['vlm_raw'] = f'抓图失败: {e}'
        ctx.log_event('snapshot_failed', f'{name}：抓图失败 {e}', level='error', run_id=run_id, leg_id=leg.get('id'))
        return _finish(ctx, row, template, name, t0)

    # 2. 裁切 + 标注
    t_crop = time.time()
    try:
        crop = pano.crop_angle_range(img, af, at, pad_deg=pad_deg)
        crop_bytes = pano.to_jpeg(crop)
        p_crop = save_jpeg(crop_bytes, run_dir, f'{stem}_crop')
        row['crop_path'] = p_crop.relative_to(ctx.media_dir).as_posix()
        annotated = pano.annotate(img, af, at, forward_deg=forward, label=name)
        annotated_bytes = pano.to_jpeg(annotated, 80)
        save_jpeg(annotated_bytes, run_dir, f'{stem}_annot')
        stages['crop_ms'] = int((time.time() - t_crop) * 1000)
    except Exception as e:      # noqa: BLE001
        row['vlm_raw'] = f'裁切失败: {e}'
        ctx.log_event('crop_failed', f'{name}：裁切失败 {e}', level='error', run_id=run_id, leg_id=leg.get('id'))
        return _finish(ctx, row, template, name, t0)

    # 3. VLM
    prompt = row['prompt'].strip()
    if not prompt:
        row['answer'] = 'unknown'
        row['vlm_raw'] = '该任务航点没有 prompt，跳过判读'
        return _finish(ctx, row, template, name, t0)
    images = [(crop_bytes, 'image/jpeg')]
    if ctx.cfg.get_bool('VLM_SEND_FULL_PANO'):
        images.append((annotated_bytes, 'image/jpeg'))       # 整图带范围标注线，模型能对上「第几度到第几度」
    user = build_user_prompt(prompt, af, at, forward_deg=forward, waypoint_name=name)
    ctx.log_event('vlm_ask', f'{name}：向 VLM 提问（{ctx.vlm.describe()}）', run_id=run_id, leg_id=leg.get('id'),
                  data={'prompt': prompt, 'angle_from': af, 'angle_to': at})
    t_vlm = time.time()
    try:
        res: VlmResult = ctx.vlm.ask_yes_no(images, user, system=SYSTEM_PROMPT)
    except Exception as e:      # noqa: BLE001
        res = VlmResult('error', provider=ctx.vlm.name, model=getattr(ctx.vlm, 'model', ''), error=str(e))
    stages['vlm_ms'] = int((time.time() - t_vlm) * 1000)
    row['answer'] = res.answer
    row['vlm_raw'] = res.raw if not res.error else f"{res.raw}\n[error] {res.error}".strip()
    row['vlm_provider'] = f'{res.provider} {res.model}'.strip()
    ctx.log_event('vlm_answer', f"{name}：VLM 回答 {res.answer}" + (f'（{res.error}）' if res.error else ''),
                  level='error' if res.answer == 'error' else 'info', run_id=run_id, leg_id=leg.get('id'),
                  data=res.to_dict())
    return _finish(ctx, row, template, name, t0)


def _finish(ctx, row: dict, template: dict | None, name: str, t0: float) -> dict:
    # 4. TTS
    text, passed = pick_tts(template or {}, row['answer'], name)
    row['passed'] = None if passed is None else int(passed)
    stages = row.get('stages') if isinstance(row.get('stages'), dict) else {}
    if text:
        row['tts_text'] = text
        t_tts = time.time()
        try:
            r = ctx.tts.speak(text, {'run_id': row['run_id'], 'waypoint': name, 'answer': row['answer']})
            stages['tts_ms'] = int((time.time() - t_tts) * 1000)
            stages['tts_cache_hit'] = r.get('cache_hit')
            row['tts_audio_path'] = r.get('audio_path')
            row['tts_status'] = dumps(r.get('sinks')) if not r.get('error') else f"{r.get('error')} {dumps(r.get('sinks'))}"
            timing = _fmt_stages(stages)
            ctx.log_event('tts', f'{name}：播报「{text}」' + (f'（{timing}）' if timing else ''), run_id=row['run_id'], leg_id=row['leg_id'],
                          data={'sinks': r.get('sinks'), 'audio_url': r.get('audio_url'), 'stages': stages})
        except Exception as e:      # noqa: BLE001
            row['tts_status'] = f'error: {e}'
            ctx.log_event('tts_failed', f'{name}：TTS 失败 {e}', level='error', run_id=row['run_id'], leg_id=row['leg_id'])
    row['latency_ms'] = int((time.time() - t0) * 1000)
    row['created_at'] = now_iso()
    cols = ['run_id', 'leg_id', 'task_waypoint_id', 'waypoint_name', 'prompt', 'angle_from', 'angle_to', 'image_path',
            'crop_path', 'vlm_provider', 'vlm_raw', 'answer', 'expected', 'passed', 'tts_text', 'tts_audio_path',
            'tts_status', 'latency_ms', 'capture_pose', 'frame_score', 'frame_attempts', 'created_at']
    row['id'] = ctx.db.execute(f"INSERT INTO inspections({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                               [row.get(c) for c in cols])
    ctx.bus.publish('inspection', row)
    if row['passed'] == 0 or row['answer'] == 'error':
        ctx.notify('inspection_failed', {'run_id': row['run_id'], 'inspection_id': row['id'], 'waypoint': name, 'prompt': row['prompt'],
                                         'answer': row['answer'], 'tts_text': row['tts_text'], 'crop_path': row['crop_path']})
    return row
