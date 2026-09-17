"""到点流程提速：常驻读流器（到点抓帧不再等关键帧）、播报预合成缓存、分阶段计时。"""
import time

from app.bus import Bus
from app.executor.inspection import collect_tts_texts
from app.media.snapshot import LavfiSource, LiveStreamSource, build_source
from app.tts.base import BrowserSink, TtsEngine, TtsService


def _wait(pred, timeout=12.0, step=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def test_live_source_falls_back_when_cold_then_serves_fresh_frames_and_idles_out():
    src = build_source('lavfi:testsrc=size=640x320:rate=5', keepalive=True, keepalive_idle=2.0)
    assert isinstance(src, LiveStreamSource) and isinstance(src.inner, LavfiSource)
    assert build_source('lavfi:testsrc', keepalive=False).name == 'lavfi'          # 关掉就是原来的源
    assert build_source('synthetic', keepalive=True).name != 'live'                # 合成源不套读流器
    try:
        # 冷启动：没预热 → 退回一次性抓帧（和以前一样慢但一定有图），并顺手把读流器起起来
        ctx: dict = {}
        data = src.grab(ctx)
        assert data[:2] == b'\xff\xd8' and ctx['grab']['source'] == 'oneshot' and ctx['grab']['reason'] == 'not_running'
        assert _wait(lambda: src.stats()['frames'] >= 2), src.stats()
        # 热：直接拿最新帧，快且新鲜
        ctx = {}
        t0 = time.time()
        data = src.grab(ctx)
        assert time.time() - t0 < 1.5 and data[:2] == b'\xff\xd8' and data[-2:] == b'\xff\xd9'
        assert ctx['grab']['source'] == 'live' and ctx['grab']['age_s'] <= 1.5 and ctx['quality'] == {}   # lavfi 不体检
        assert '+live(读流中)' in src.describe() and 'lavfi' in src.describe()
        st = src.stats()
        assert st['running'] and st['grabs_live'] == 1 and st['grabs_fallback'] == 1
        # 空闲 2 s 没人抓帧 → 自动停掉 ffmpeg
        assert _wait(lambda: not src.running(), timeout=8), src.stats()
        assert '空闲' in src.describe()
        # 再 warm 又能起来
        src.warm()
        assert _wait(lambda: src.running() and src.stats()['frames'] > 0)
    finally:
        src.stop()
    assert _wait(lambda: not src.running(), timeout=5)


def test_live_source_waits_for_clean_frame_after_decode_error_and_checks_detail():
    src = LiveStreamSource(LavfiSource('testsrc=size=640x320:rate=5'), lambda: ['-f', 'lavfi', '-i', 'testsrc=size=640x320:rate=5'],
                           idle_seconds=30, attempts=3, min_detail=0.5, qc=True)
    try:
        src.warm()
        assert _wait(lambda: src.stats()['frames'] >= 2)
        # 刚报过解码错误：接下来 1 s 内的帧都可疑，要等干净帧（不计入 attempts）
        with src._lock:
            src._last_err_ts = time.time()
        ctx: dict = {}
        t0 = time.time()
        src.grab(ctx)
        assert ctx['grab']['source'] == 'live' and ctx['grab']['waited_for_clean'] is True
        assert time.time() - t0 >= 0.9 and ctx['quality']['ok'] is True and ctx['quality']['attempts'] == 1
        # testsrc 画面有纵向细节，体检能过；阈值抬到不可能的高度 → 三帧都不合格，返回最后一帧但 ok=False（与一次性抓帧同语义）
        src.min_detail = 10_000.0
        ctx = {}
        data = src.grab(ctx)
        assert data and ctx['quality']['ok'] is False and ctx['quality']['attempts'] == 3 and '没收敛' in ctx['quality']['reason']
    finally:
        src.stop()


class CountingEngine(TtsEngine):
    name, ext = 'count', 'wav'

    def __init__(self):
        self.calls = []

    def synthesize(self, text, out_path):
        self.calls.append(text)
        out_path.write_bytes(b'RIFF' + text.encode('utf-8'))
        return out_path


def test_tts_cache_hits_and_prewarm(tmp_path):
    eng = CountingEngine()
    svc = TtsService(eng, [BrowserSink(Bus())], tmp_path, timeout=5)
    r1 = svc.speak('消防栓门已关闭')
    assert r1['cache_hit'] is False and r1['audio_path'].startswith('tts_cache/') and r1['audio_url'] == '/media/' + r1['audio_path']
    r2 = svc.speak('消防栓门已关闭')
    assert r2['cache_hit'] is True and r2['audio_path'] == r1['audio_path'] and eng.calls == ['消防栓门已关闭']
    # 预合成：只合成缓存里没有的；到点时 speak 直接命中，一次合成都不做
    n = svc.prewarm(['消防栓门已关闭', '三脚架在位', '三脚架在位', '', '灭火器缺失'], block=True)
    assert n == 2 and sorted(eng.calls[1:]) == ['三脚架在位', '灭火器缺失']
    assert svc.speak('三脚架在位')['cache_hit'] is True and len(eng.calls) == 3
    assert svc.cache_hits == 2 and svc.synth_count == 3
    # 声音不同 → 不同的缓存键
    eng.voice = 'another'
    assert svc.speak('三脚架在位')['cache_hit'] is False


def test_prewarm_is_noop_for_null_engine(tmp_path):
    from app.tts.base import NullEngine
    assert TtsService(NullEngine(), [BrowserSink(Bus())], tmp_path).prewarm(['a', 'b']) == 0


def test_collect_tts_texts_covers_three_answers_and_dedupes():
    tws = [{'name': 'G1', 'answer_template': {'expected': 'yes', 'on_pass': 'G1被安全悬挂', 'on_fail': 'G1正在被使用请注意', 'on_unknown': ''}},
           {'name': '门', 'answer_template': '{"expected":"no","on_pass":"{name}畅通","on_fail":"{name}有障碍","on_unknown":"{name}无法判断"}'},
           {'name': '空', 'answer_template': {'on_pass': '', 'on_fail': '', 'on_unknown': ''}}, None]
    assert collect_tts_texts(tws) == ['G1被安全悬挂', 'G1正在被使用请注意', '门有障碍', '门畅通', '门无法判断']   # 期望「不是」时 answer=yes 是不通过那句
