# -*- coding: utf-8 -*-
"""VLM 适配层：统一成「给几张图 + 一个问题 → yes / no / unknown」。

回答必须收敛为二值：prompt 要求模型只回 JSON {"answer": "yes"|"no"|"unknown", "reason": "..."}，
解析时再做一层宽松兜底（是/否/不是/yes/no/true/false）。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field

SYSTEM_PROMPT = (
    '你是四足巡检机器人的视觉判读助手。机器人头顶是 360° 全景相机，你会收到全景图中按角度范围裁切出来的局部'
    '（有时还附整张全景作参考），以及一个需要用「是/不是」回答的检查问题。\n'
    '只输出一行 JSON，不要有其他文字：{"answer": "yes" 或 "no" 或 "unknown", "reason": "一句话依据"}。\n'
    '判断依据必须来自图像；画面中看不到相关目标、或被遮挡无法判断时 answer 用 "unknown"，不要猜。'
)


def build_user_prompt(prompt: str, angle_from: float, angle_to: float, *, forward_deg: float = 180.0,
                      waypoint_name: str = '') -> str:
    rel_from = (angle_from - forward_deg + 540) % 360 - 180
    rel_to = (angle_to - forward_deg + 540) % 360 - 180
    where = f'第一张图是全景图 {angle_from:.0f}°–{angle_to:.0f}° 的裁切（相对机头 {rel_from:+.0f}° 到 {rel_to:+.0f}°）。'
    head = f'巡检点「{waypoint_name}」。' if waypoint_name else ''
    return f'{head}{where}\n检查问题：{prompt.strip()}\n请只回答 JSON。'


_YES = ('yes', 'true', '是', '有', '正常', '关好', '已关', '关闭')
_NO = ('no', 'false', '否', '不是', '没有', '未', '没', '异常', '打开', '未关')


def parse_yes_no(text: str | None) -> str:
    if not text:
        return 'unknown'
    s = text.strip()
    m = re.search(r'\{.*?\}', s, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            a = str(obj.get('answer', '')).strip().lower()
            if a in ('yes', 'y', 'true', '是'):
                return 'yes'
            if a in ('no', 'n', 'false', '否', '不是'):
                return 'no'
            if a:
                return 'unknown'
        except ValueError:
            pass
    low = s.lower()
    first = re.split(r'[\s,，。.!！?？:：]+', low, maxsplit=1)[0]
    if first in ('yes', 'y', '是', '是的', 'true'):
        return 'yes'
    if first in ('no', 'n', '否', '不是', '不', 'false'):
        return 'no'
    if any(k in low for k in ('无法判断', 'unknown', 'cannot', '不确定', '看不到')):
        return 'unknown'
    # 兜底：先找否定词（「不是」包含「是」，所以否定优先）
    if any(k in low for k in _NO):
        return 'no'
    if any(k in low for k in _YES):
        return 'yes'
    return 'unknown'


@dataclass
class VlmResult:
    answer: str                  # yes | no | unknown | error
    raw: str = ''
    provider: str = ''
    model: str = ''
    latency_ms: int = 0
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'answer': self.answer, 'raw': self.raw, 'provider': self.provider, 'model': self.model,
                'latency_ms': self.latency_ms, 'error': self.error}


class VlmProvider:
    name = 'base'
    model = ''

    def ask_yes_no(self, images: list[tuple[bytes, str]], prompt: str, *, system: str | None = None) -> VlmResult:
        raise NotImplementedError

    def describe(self) -> str:
        return f'{self.name} {self.model}'.strip()


class MockVlm(VlmProvider):
    """mock：固定 / 交替 / 随机 / 按 prompt 关键词。测试和无模型环境用。"""
    name = 'mock'

    def __init__(self, mode: str = 'alternate') -> None:
        self.mode = (mode or 'alternate').lower()
        self._n = 0
        self.model = f'mock:{self.mode}'

    def ask_yes_no(self, images, prompt, *, system=None):
        t0 = time.time()
        self._n += 1
        if self.mode in ('yes', 'no', 'unknown'):
            a = self.mode
        elif self.mode == 'random':
            a = random.choice(['yes', 'no', 'unknown'])
        elif self.mode == 'keyword':
            a = 'no' if ('未' in prompt or '没' in prompt) else 'yes'
        else:
            a = 'yes' if self._n % 2 else 'no'
        raw = json.dumps({'answer': a, 'reason': f'mock 判读（{self.mode}，第 {self._n} 次，{len(images)} 张图）'}, ensure_ascii=False)
        time.sleep(0.05)
        return VlmResult(answer=a, raw=raw, provider=self.name, model=self.model,
                         latency_ms=int((time.time() - t0) * 1000))


def _extra_body(cfg) -> dict:
    """VLM_EXTRA_BODY：合并进请求体的额外字段（服务商私有参数）。写坏了只记一行日志，不影响判读。"""
    raw = (cfg.get('VLM_EXTRA_BODY') or '').strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except ValueError:
        logging.getLogger('scheduler').warning('VLM_EXTRA_BODY 不是合法 JSON 对象，已忽略：%s', raw[:120])
        return {}


def build_provider(cfg) -> VlmProvider:
    p = (cfg.get('VLM_PROVIDER') or 'mock').lower()
    if p == 'qwen':
        from .openai_compat import QwenVlm
        return QwenVlm(cfg.get('VLM_BASE_URL'), cfg.get('VLM_MODEL'), cfg.get('VLM_API_KEY'),
                       timeout=cfg.get_float('VLM_TIMEOUT'), extra_body=_extra_body(cfg))
    if p == 'openai_compat':
        from .openai_compat import OpenAICompatVlm
        return OpenAICompatVlm(cfg.get('VLM_BASE_URL'), cfg.get('VLM_MODEL'), cfg.get('VLM_API_KEY'),
                               timeout=cfg.get_float('VLM_TIMEOUT'), extra_body=_extra_body(cfg))
    if p == 'anthropic':
        from .anthropic_provider import AnthropicVlm
        return AnthropicVlm(api_key=cfg.get('ANTHROPIC_API_KEY') or None, model=cfg.get('ANTHROPIC_MODEL'),
                            fallbacks=cfg.get_bool('VLM_ANTHROPIC_FALLBACKS'), timeout=cfg.get_float('VLM_TIMEOUT'))
    return MockVlm(cfg.get('VLM_MOCK_ANSWER'))
