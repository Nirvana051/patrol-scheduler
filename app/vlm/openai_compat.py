# -*- coding: utf-8 -*-
"""OpenAI 兼容的 /chat/completions（Ollama、vLLM、LM Studio、阿里 DashScope 兼容模式、OpenAI 等）。用 requests，不引入 SDK。"""
from __future__ import annotations

import base64
import json
import time

import requests

from .base import SYSTEM_PROMPT, VlmProvider, VlmResult, parse_yes_no


class OpenAICompatVlm(VlmProvider):
    name = 'openai_compat'

    def __init__(self, base_url: str, model: str, api_key: str = '', *, timeout: float = 120.0,
                 extra_body: dict | None = None) -> None:
        self.base_url = (base_url or '').rstrip('/')
        self.model = model
        self.api_key = api_key or ''
        self.timeout = timeout
        # 额外的请求体字段（不同服务商的私有参数）。DashScope 的 enable_thinking 就走这里。
        self.extra_body = dict(extra_body or {})

    def ask_yes_no(self, images, prompt, *, system=None):
        t0 = time.time()
        content = [{'type': 'text', 'text': prompt}]
        for data, mime in images:
            b64 = base64.standard_b64encode(data).decode('ascii')
            content.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
        payload = {'model': self.model, 'temperature': 0, 'max_tokens': 200,
                   'messages': [{'role': 'system', 'content': system or SYSTEM_PROMPT},
                                {'role': 'user', 'content': content}]}
        payload.update(self.extra_body)
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        try:
            r = requests.post(f'{self.base_url}/chat/completions', json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            return VlmResult('error', provider=self.name, model=self.model, error=f'网络错误: {e}',
                             latency_ms=int((time.time() - t0) * 1000))
        if r.status_code != 200:
            detail = r.text[:500]
            try:                                    # OpenAI/DashScope 都是 {"error": {"message": ...}}
                err = r.json().get('error')
                if isinstance(err, dict) and err.get('message'):
                    detail = f"{err.get('message')}（code={err.get('code') or err.get('type')}）"
            except ValueError:
                pass
            return VlmResult('error', raw=r.text[:500], provider=self.name, model=self.model,
                             error=f'HTTP {r.status_code}: {detail}', latency_ms=int((time.time() - t0) * 1000))
        try:
            body = r.json()
            text = body['choices'][0]['message']['content']
            if isinstance(text, list):
                text = ''.join(part.get('text', '') for part in text if isinstance(part, dict))
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return VlmResult('error', raw=r.text[:500], provider=self.name, model=self.model,
                             error=f'响应解析失败: {e}', latency_ms=int((time.time() - t0) * 1000))
        return VlmResult(parse_yes_no(text), raw=text, provider=self.name, model=self.model,
                         latency_ms=int((time.time() - t0) * 1000),
                         extra={'usage': body.get('usage')} if isinstance(body, dict) else {})

    def describe(self) -> str:
        return f'{self.name} {self.base_url} {self.model}'


class QwenVlm(OpenAICompatVlm):
    """阿里通义千问（DashScope 的 OpenAI 兼容端点）。

    与通用 openai_compat 的唯一区别是两个默认值：
    * base_url 默认 DashScope 兼容模式地址；
    * **必须显式带 `enable_thinking: false`** —— DashScope 对推理型 Qwen（qwen3.x 系列）
      的非流式调用要求这个参数存在且为 false，缺了直接 400
      「parameter.enable_thinking must be set to false for non-streaming calls」。
      我们的判读是一次性非流式请求，所以固定关掉思考（二值判断也不需要）。

    模型要能读图：`qwen3.5-flash` 官方标注支持文本/图像/视频；若某个模型拒收图片，
    换成明确的视觉模型（如 `qwen3-vl-plus`、`qwen-vl-max`）。
    """
    name = 'qwen'
    DEFAULT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'

    def __init__(self, base_url: str = '', model: str = 'qwen3.5-flash', api_key: str = '', *,
                 timeout: float = 120.0, extra_body: dict | None = None) -> None:
        body = {'enable_thinking': False}
        body.update(extra_body or {})
        super().__init__(base_url or self.DEFAULT_BASE_URL, model or 'qwen3.5-flash', api_key,
                         timeout=timeout, extra_body=body)
