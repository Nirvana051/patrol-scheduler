# -*- coding: utf-8 -*-
"""抓帧体检：ffmpeg 解出残缺画面时**退出码仍是 0**，所以必须看 stderr 与画面本身。

这一组用例钉住的是一个真实故障：接入直播流时抓到「还没收敛」的画面（上下恒定的竖带），
ffmpeg 退出码 0、图片非空、只在 stderr 里报解码错误 —— 原来的判断
（`returncode != 0 or not stdout`）一条都不成立，糊图被当成正常结果送去判读，
模型对着噪声给出「不是 / 无法判断」。线上真机 30 张里抓到 1 张（run 213）。
"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app.media import snapshot as sn


def _jpeg(img: Image.Image) -> bytes:
    b = io.BytesIO()
    img.save(b, format='JPEG', quality=90)
    return b.getvalue()


def _real_ish(w=320, h=160) -> Image.Image:
    """有纵向细节的「正常」画面（随机噪声 + 渐变），纵向梯度远高于阈值。"""
    rng = np.random.default_rng(7)
    a = rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    return Image.fromarray(a)


def _smeared(w=320, h=160) -> Image.Image:
    """竖带画面：每一列上下恒定 —— 正是抓到没收敛画面时的样子，纵向梯度为 0。"""
    row = np.random.default_rng(3).integers(0, 255, size=(1, w, 3), dtype=np.uint8)
    return Image.fromarray(np.repeat(row, h, axis=0))


# ── 指标本身 ─────────────────────────────────────────────────────────────
def test_frame_detail_separates_smeared_from_normal():
    normal = sn.frame_detail(_jpeg(_real_ish()))
    smeared = sn.frame_detail(_jpeg(_smeared()))
    assert smeared < sn.DEFAULT_MIN_DETAIL, f'竖带画面应当低于阈值，实际 {smeared}'
    assert normal > sn.DEFAULT_MIN_DETAIL * 2, f'正常画面应当远高于阈值，实际 {normal}'


def test_frame_detail_bad_bytes_returns_minus_one():
    assert sn.frame_detail(b'not an image') == -1.0


# ── stderr 特征 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('line', [
    '[h264 @ 0x55] non-existing PPS 0 referenced',
    '[h264 @ 0x55] decode_slice_header error',
    '[h264 @ 0x55] no frame!',
    '[h264 @ 0x55] concealing 120 DC, 120 AC, 120 MV errors in P frame',
    '[h264 @ 0x55] error while decoding MB 12 7',
    '[h264 @ 0x55] Frame num gap 3 1',
])
def test_decode_error_patterns_hit(line):
    assert sn.DECODE_ERROR_PATTERNS.search(line), f'应当认出解码错误：{line}'


@pytest.mark.parametrize('line', [
    '',
    '[swscaler @ 0x55] deprecated pixel format used, make sure you did set range correctly',
    'frame=    1 fps=0.0 q=2.0 Lsize=N/A time=00:00:00.04 bitrate=N/A speed=0.12x',
])
def test_decode_error_patterns_ignore_harmless(line):
    assert not sn.DECODE_ERROR_PATTERNS.search(line), f'不该把这行当成解码错误：{line}'


# ── 重抓逻辑（用假的 ffmpeg：只替换 subprocess.run）────────────────────────
class _FakeRun:
    """按脚本依次返回 (returncode, stdout, stderr)。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, cmd, capture_output=False, timeout=None):
        rc, out, err = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return type('R', (), {'returncode': rc, 'stdout': out, 'stderr': err})()


def _grab(monkeypatch, script, *, attempts=3, min_detail=None):
    fake = _FakeRun(script)
    monkeypatch.setattr(sn.subprocess, 'run', fake)
    monkeypatch.setattr(sn.time, 'sleep', lambda _s: None)      # 别真等
    monkeypatch.setattr(sn.shutil, 'which', lambda _n: '/usr/bin/ffmpeg')
    src = sn.RtspFfmpegSource(lambda: 'rtsp://x/y', attempts=attempts,
                              min_detail=sn.DEFAULT_MIN_DETAIL if min_detail is None else min_detail)
    ctx: dict = {}
    data = src.grab(ctx)
    return data, ctx.get('quality'), fake.calls


GOOD = _jpeg(_real_ish())
GOOD_A = _jpeg(_real_ish(w=321))     # 另一张「画面体检能过」的图，用来单独考 stderr 那条路
BAD = _jpeg(_smeared())


def test_clean_grab_does_not_retry(monkeypatch):
    data, qc, calls = _grab(monkeypatch, [(0, GOOD, b'')])
    assert data == GOOD and qc['ok'] is True and calls == 1
    assert qc['detail'] > sn.DEFAULT_MIN_DETAIL


def test_stderr_decode_error_triggers_retry(monkeypatch):
    """第一次 ffmpeg 退出码 0 且有输出，但 stderr 报解码错误 —— 必须重抓。

    这正是原来会静默通过的那条路：退出码 0 + 非空输出。

    第一帧特意用**画面体检能过**的图（GOOD_A）：这样只有 stderr 检查能救它。
    第一版这里用了竖带图，结果画面体检顺手就挡住了 —— 把 stderr 检查整段删掉用例照样全绿，
    等于没测到。
    """
    data, qc, calls = _grab(monkeypatch, [
        (0, GOOD_A, b'[h264 @ 0x55] non-existing PPS 0 referenced\n[h264 @ 0x55] no frame!\n'),
        (0, GOOD, b''),
    ])
    assert calls == 2, '应当重抓一次'
    assert data == GOOD and qc['ok'] is True, '第一帧画面「看起来正常」，只能靠 stderr 认出来'


def test_low_detail_triggers_retry_even_without_stderr(monkeypatch):
    """ffmpeg 一声不响（stderr 干净），但画面是竖带 —— 靠画面体检兜住。"""
    data, qc, calls = _grab(monkeypatch, [(0, BAD, b''), (0, GOOD, b'')])
    assert calls == 2 and data == GOOD and qc['ok'] is True


def test_all_attempts_bad_returns_frame_but_flags_not_ok(monkeypatch):
    """重抓到底仍不合格：**不抛异常**（图要留给人看），但 ok=False 且写明原因。

    执行器看到 ok=False 会跳过 VLM 判读 —— 不拿糊图问模型，也不白花付费调用。
    """
    data, qc, calls = _grab(monkeypatch, [(0, BAD, b'')], attempts=3)
    assert calls == 3, '应当用完全部重抓次数'
    assert data == BAD, '仍然要把最后一帧交出来'
    assert qc['ok'] is False and '没收敛' in qc['reason']
    assert qc['attempts'] == 3


def test_ffmpeg_hard_failure_still_raises(monkeypatch):
    """ffmpeg 真的失败（退出码非 0）时行为不变：抛 SnapshotError。"""
    monkeypatch.setattr(sn.shutil, 'which', lambda _n: '/usr/bin/ffmpeg')
    monkeypatch.setattr(sn.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(sn.subprocess, 'run', _FakeRun([(1, b'', b'Connection refused')]))
    with pytest.raises(sn.SnapshotError, match='抓帧失败'):
        sn.RtspFfmpegSource(lambda: 'rtsp://x/y', attempts=2).grab({})


def test_min_detail_zero_disables_the_check(monkeypatch):
    """SNAPSHOT_MIN_DETAIL=0 时关掉画面体检（留给「场景本来就很平」的现场，如 Gazebo 空白墙）。"""
    data, qc, calls = _grab(monkeypatch, [(0, BAD, b'')], min_detail=0)
    assert calls == 1 and data == BAD and qc['ok'] is True


# ── 端到端：体检不合格时执行器要跳过判读，而不是拿糊图去问模型 ──────────────
class _DegradedSource:
    """假抓帧源：永远返回一张竖带图，并把「体检不合格」写进 context —— 复刻重抓到底仍失败的情形。"""
    name = 'degraded'

    def __init__(self, ok: bool = False):
        self.ok = ok
        self.asked = 0

    def grab(self, context=None):
        self.asked += 1
        if context is not None:
            context['quality'] = {'ok': self.ok, 'attempts': 3, 'detail': 0.17,
                                  'reason': None if self.ok else '画面没收敛（纵向细节 0.17 < 1.00）',
                                  'min_detail': 1.0}
        return _jpeg(_smeared(1280, 640))

    def describe(self):
        return 'degraded(test)'


class _CountingVlm:
    name = 'counting'
    model = 'x'

    def __init__(self):
        self.calls = 0

    def describe(self):
        return 'counting x'

    def ask_yes_no(self, images, user, system=None):
        self.calls += 1
        from app.vlm.base import VlmResult
        return VlmResult('yes', provider='counting', model='x', raw='{"answer":"yes"}')


def _one_inspection(client):
    """建一个任务航点并直接跑一次检查（不下发导航，只测检查这一段）。"""
    from app.executor.inspection import run_inspection
    from tests.conftest import DEMO_MAP, sync_map
    sync_map(client)
    tw = client.post('/api/task-waypoints', json={
        'name': '体检点', 'map_name': DEMO_MAP, 'nav_node_id': '1', 'prompt': '门开了吗',
        'angle_from': 150.0, 'angle_to': 210.0,
        'answer_template': {'expected': 'yes', 'on_pass': 'a', 'on_fail': 'b', 'on_unknown': 'c'}}).json()
    ctx = client.ctx
    run_id = ctx.db.execute(
        "INSERT INTO runs(task_id,task_name,map_name,status,mode,started_at) "
        "VALUES(NULL,'qc',?,'running','mock',datetime('now'))", [DEMO_MAP])
    return run_inspection(ctx, run_id, {'id': None, 'seq': 1}, tw), ctx, run_id


def test_degraded_frame_skips_vlm_and_is_recorded(app_client):
    """重抓到底仍不合格：不问模型、答案记 error、指标落库、并有一条 error 级事件。"""
    ctx = app_client.ctx
    vlm = _CountingVlm()
    ctx.snapshot = _DegradedSource(ok=False)
    ctx.vlm = vlm
    row, ctx, run_id = _one_inspection(app_client)

    assert vlm.calls == 0, '画面不可用时**不该**去调 VLM（既无意义，又是一次真金白银的付费调用）'
    assert row['answer'] == 'error'
    assert '体检不合格' in row['vlm_raw'] and '没收敛' in row['vlm_raw']
    assert row['image_path'], '图仍然要留下来给人看'

    saved = ctx.db.query_one('SELECT frame_score, frame_attempts, answer FROM inspections WHERE id=?', [row['id']])
    assert abs(saved['frame_score'] - 0.17) < 1e-6 and saved['frame_attempts'] == 3
    ev = ctx.db.query("SELECT type, level FROM events WHERE run_id=? AND type='snapshot_degraded'", [run_id])
    assert ev and ev[0]['level'] == 'error', '必须留一条醒目的事件，不能静默'


def test_good_frame_still_asks_vlm_and_records_score(app_client):
    """体检通过时行为不变：照常判读，指标一样落库（用来事后审计）。"""
    ctx = app_client.ctx
    vlm = _CountingVlm()
    ctx.snapshot = _DegradedSource(ok=True)      # ok=True：体检算通过
    ctx.vlm = vlm
    row, ctx, _run_id = _one_inspection(app_client)
    assert vlm.calls == 1 and row['answer'] == 'yes'
    saved = ctx.db.query_one('SELECT frame_score, frame_attempts FROM inspections WHERE id=?', [row['id']])
    assert saved['frame_attempts'] == 3 and abs(saved['frame_score'] - 0.17) < 1e-6
