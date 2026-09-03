# -*- coding: utf-8 -*-
"""进程内发布/订阅：后台线程 → 浏览器 SSE。每个订阅者一个有界队列，慢消费者丢最旧消息而不是拖死发布者。"""
from __future__ import annotations

import queue
import threading
from typing import Any


class Bus:
    def __init__(self, maxsize: int = 500) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, kind: str, payload: Any) -> None:
        msg = {'kind': kind, 'payload': payload}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except queue.Empty:
                    pass
