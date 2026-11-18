"""本地推理准入：有界队列、前台优先、公平让行和排队取消。

仅管理本进程的原生模型调用；不持锁执行网络请求，也不抢占已进入 Ollama 的推理。
"""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from .config import settings

_context = ContextVar("anvil_model_scheduling", default=(None, False))


class ModelCancelled(RuntimeError):
    pass


class ModelQueueBusy(RuntimeError):
    pass


@contextmanager
def model_scope(*, cancel=None, background=False):
    token = _context.set((cancel, background))
    try:
        yield
    finally:
        _context.reset(token)


class ModelScheduler:
    def __init__(self, max_parallel=1, max_waiting=32, foreground_burst=8):
        if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0
               for n in (max_parallel, max_waiting, foreground_burst)):
            raise ValueError("模型调度容量必须为正整数")
        self.max_parallel = max_parallel
        self.max_waiting = max_waiting
        self.foreground_burst = foreground_burst
        self._condition = threading.Condition()
        self._waiting = []
        self._active = 0
        self._foreground_runs = 0

    def snapshot(self):
        with self._condition:
            return {"active": self._active, "waiting": len(self._waiting),
                    "background_waiting": sum(item[1] for item in self._waiting),
                    "max_parallel": self.max_parallel, "max_waiting": self.max_waiting}

    def _next(self):
        foreground = next((item for item in self._waiting if not item[1]), None)
        background = next((item for item in self._waiting if item[1]), None)
        if background is not None and (foreground is None or self._foreground_runs >= self.foreground_burst):
            return background
        return foreground

    @contextmanager
    def slot(self, timeout):
        cancel, background = _context.get()
        started = time.monotonic()
        deadline = started + timeout
        ticket = (object(), background)
        with self._condition:
            if cancel is not None and cancel.is_set():
                raise ModelCancelled("工单已取消，未进入模型推理")
            if len(self._waiting) >= self.max_waiting:
                raise ModelQueueBusy("本地模型等待队列已满，请稍后重试")
            self._waiting.append(ticket)
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ModelCancelled("工单已取消，已退出模型等待队列")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ModelQueueBusy("本地模型排队超时")
                    if self._active < self.max_parallel and self._next() is ticket:
                        self._active += 1
                        self._foreground_runs = 0 if background else self._foreground_runs + 1
                        break
                    self._condition.wait(min(remaining, 0.1) if cancel is not None else remaining)
            finally:
                self._waiting.remove(ticket)
                self._condition.notify_all()
        try:
            yield {"queue_sec": time.monotonic() - started,
                   "timeout": max(0.001, deadline - time.monotonic()), "background": background}
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


@lru_cache(maxsize=1)
def scheduler():
    limits = settings()["inference"]
    return ModelScheduler(limits["max_parallel"], limits["max_waiting"], limits["foreground_burst"])
