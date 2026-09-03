# -*- coding: utf-8 -*-
"""Anthropic 官方 SDK 的视觉判读。默认 claude-opus-5，低 effort（二值分类不需要深思）。

要点（按 claude-api skill 的当前契约）：
* 图片走 base64 image block，放在文本前面
* 不传 thinking（Claude Opus 5 默认自适应），用 output_config.effort 控制开销
* 默认开启服务端 refusal fallbacks（beta），被安全分类器拒绝时自动换模型重跑
* stop_reason == "refusal" 时答 unknown 并写明原因
"""
from __future__ import annotations

import base64
import time

from .base import SYSTEM_PROMPT, VlmProvider, VlmResult, parse_yes_no


class AnthropicVlm(VlmProvider):
    name = 'anthropic'

    def __init__(self, api_key: str | None = None, model: str = 'claude-opus-5', *, fallbacks: bool = True,
                 effort: str = 'low', timeout: float = 120.0) -> None:
        import anthropic  # 延迟导入：没装 SDK 时其他 provider 仍可用
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout) if api_key else anthropic.Anthropic(timeout=timeout)
        self.model = model or 'claude-opus-5'
        self.fallbacks = fallbacks
        self.effort = effort

    def _content(self, images, prompt):
        content = []
        for data, mime in images:
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
                                                        'data': base64.standard_b64encode(data).decode('utf-8')}})
        content.append({'type': 'text', 'text': prompt})
        return content

    def ask_yes_no(self, images, prompt, *, system=None):
        a = self._anthropic
        t0 = time.time()
        kwargs = dict(model=self.model, max_tokens=256, system=system or SYSTEM_PROMPT,
                      output_config={'effort': self.effort},
                      messages=[{'role': 'user', 'content': self._content(images, prompt)}])
        try:
            if self.fallbacks:
                try:
                    resp = self.client.beta.messages.create(betas=['server-side-fallback-2026-07-01'],
                                                            fallbacks='default', **kwargs)
                except TypeError:
                    # 旧 SDK 不认识 fallbacks 参数时退回普通调用
                    resp = self.client.messages.create(**kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except a.RateLimitError as e:
            return VlmResult('error', provider=self.name, model=self.model, error=f'限流: {e}',
                             latency_ms=int((time.time() - t0) * 1000))
        except a.APIStatusError as e:
            return VlmResult('error', provider=self.name, model=self.model, error=f'HTTP {e.status_code}: {e.message}',
                             latency_ms=int((time.time() - t0) * 1000))
        except a.APIConnectionError as e:
            return VlmResult('error', provider=self.name, model=self.model, error=f'网络错误: {e}',
                             latency_ms=int((time.time() - t0) * 1000))
        latency = int((time.time() - t0) * 1000)
        if getattr(resp, 'stop_reason', None) == 'refusal':
            det = getattr(resp, 'stop_details', None)
            why = getattr(det, 'explanation', '') if det else ''
            return VlmResult('unknown', raw='', provider=self.name, model=getattr(resp, 'model', self.model),
                             error=f'模型拒绝回答: {why}', latency_ms=latency)
        text = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
        usage = getattr(resp, 'usage', None)
        return VlmResult(parse_yes_no(text), raw=text, provider=self.name, model=getattr(resp, 'model', self.model),
                         latency_ms=latency,
                         extra={'input_tokens': getattr(usage, 'input_tokens', None),
                                'output_tokens': getattr(usage, 'output_tokens', None)})
