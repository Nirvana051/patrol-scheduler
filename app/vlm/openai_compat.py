# -*- coding: utf-8 -*-
"""OpenAI 兼容的 /chat/completions（Ollama、vLLM、LM Studio、DashScope 兼容模式、OpenAI 等）。用 requests，不引入 SDK。"""
from __future__ import annotations

import base64
import json
import time

import requests

from .base import SYSTEM_PROMPT, VlmProvider, VlmResult, parse_yes_no


class OpenAICompatVlm(VlmProvider):
    name = 'openai_compat'

    def __init__(self, base_url: str, model: str, api_key: str = '', *, timeout: float = 120.0) -> None:
        self.base_url = (base_url or '').rstrip('/')
        self.model = model
        self.api_key = api_key or ''
        self.timeout = timeout

    def ask_yes_no(self, images, prompt, *, system=None):
        t0 = time.time()
        content = [{'type': 'text', 'text': prompt}]
        for data, mime in images:
            b64 = base64.standard_b64encode(data).decode('ascii')
            content.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
        payload = {'model': self.model, 'temperature': 0, 'max_tokens': 200,
                   'messages': [{'role': 'system', 'content': system or SYSTEM_PROMPT},
                                {'role': 'user', 'content': content}]}
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        try:
            r = requests.post(f'{self.base_url}/chat/completions', json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            return VlmResult('error', provider=self.name, model=self.model, error=f'网络错误: {e}',
                             latency_ms=int((time.time() - t0) * 1000))
        if r.status_code != 200:
            return VlmResult('error', raw=r.text[:500], provider=self.name, model=self.model,
                             error=f'HTTP {r.status_code}', latency_ms=int((time.time() - t0) * 1000))
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
        return f'openai_compat {self.base_url} {self.model}'
