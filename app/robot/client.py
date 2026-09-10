# -*- coding: utf-8 -*-
"""SDK 包装：在 certaintyx.RobotClient 之外只加两件事 —— 全局限速令牌桶（C4）和模式判断。

不改 SDK 本身（app/vendor/certaintyx.py 是 Sample_web_api 仓库 6167083 的原样拷贝）。
所有对云端的调用都要经这里，这样 5 rps 的限流预算是全局统一的：
状态轮询、事件监听、执行器、页面上的手动操作共用一个桶。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from app.vendor.certaintyx import ACTIVE_STATUS, TERMINAL_STATUS, RobotClient, RobotError  # noqa: F401

__all__ = ['RobotGateway', 'RobotError', 'RateLimiter', 'ACTIVE_STATUS', 'TERMINAL_STATUS']


def clean_error_text(text: str, limit: int = 200) -> str:
    """真实网关偶尔在 nginx 层返回整页 HTML 的 502：去掉标签、压掉空白、截断，别把一页 HTML 写进事件。"""
    t = str(text or '')
    if '<' in t and '>' in t:
        t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:limit] + ('…' if len(t) > limit else '')


class RateLimiter:
    """令牌桶。rate 个/秒，burst 上限；acquire() 阻塞直到拿到令牌。"""

    def __init__(self, rate: float = 4.0, burst: int = 4) -> None:
        self.rate = max(0.5, float(rate))
        self.burst = max(1, int(burst))
        self.tokens = float(self.burst)
        self.ts = time.monotonic()
        self.lock = threading.Lock()
        self.total = 0

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self.total += 1
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))


class RobotGateway:
    """RobotClient 的限速代理。属性访问透传；可调用属性在调用前先拿令牌。"""

    def __init__(self, host: str, robot: str, api_key: str, *, rps: float = 4.0, timeout: float = 30.0) -> None:
        self.host, self.robot = host.rstrip('/'), robot
        self._client = RobotClient(self.host, robot, api_key, timeout=timeout)
        self.limiter = RateLimiter(rps, burst=max(2, int(rps)))
        self.mode = 'mock' if ('127.0.0.1' in self.host or 'localhost' in self.host) else 'real'

    @property
    def raw(self) -> RobotClient:
        return self._client

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr) or name.startswith('_'):
            return attr

        def wrapped(*a, **kw):
            # 限流与重试都在 SDK 里（上游 6167083 起 /v1 与透传通道都按 Retry-After 退避）：
            # 这里只做两件事 —— 先拿全局令牌（多线程共用 5 rps 预算），以及把网关偶发的整页 HTML 错误压成一行
            self.limiter.acquire()
            try:
                return attr(*a, **kw)
            except RobotError as e:
                if '<' in str(e) and '>' in str(e):
                    raise RobotError(clean_error_text(str(e)), e.status, e.body) from e
                raise
        wrapped.__name__ = name
        return wrapped

    # wait_device 内部会多次调状态接口，这里重写成每次都经限速器
    def wait_device(self, task_id: str, *, starting: bool = True, timeout: float = 180.0, interval: float = 2.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.limiter.acquire()
            st = self._client.device_start_status(task_id) if starting else self._client.device_stop_status(task_id)
            if st.get('completed'):
                if not st.get('result_success', True):
                    raise RobotError(f"设备脚本执行失败: {st.get('message')}", 0, st)
                return st
            time.sleep(max(1.0, interval))
        raise RobotError(f'等待设备脚本超时（{timeout}s）')
