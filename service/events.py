"""有界事件日志：每个订阅者独立游标，保留旧 drain 接口供 CLI 使用。"""

import asyncio
import copy
import queue
import threading
from collections import deque


class EventBuffer:
    def __init__(self, capacity=1024):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("事件容量必须为正整数")
        self._items = deque(maxlen=capacity)
        self._condition = threading.Condition()
        self._sequence = 0
        self._legacy_cursor = 0
        self._waiters = set()
        self._finished = False

    @staticmethod
    def _resolve_waiter(future):
        if not future.done():
            future.set_result(True)

    def _notify(self):
        # 调用方持有 condition；Future 只在所属事件循环中完成。
        self._condition.notify_all()
        waiters, self._waiters = self._waiters, set()
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(self._resolve_waiter, future)
            except RuntimeError:
                # 已关闭的订阅循环不影响后台工单继续发布。
                pass

    def finish(self):
        """生产线程不再写事件；确认阶段仍可追加事件并供下次连接重放。"""
        with self._condition:
            self._finished = True
            self._notify()

    @property
    def finished(self):
        with self._condition:
            return self._finished

    async def wait_after(self, cursor, timeout):
        """等待新事件或生产结束，不占用线程池，不轮询；断线自动移除等待者。"""
        loop = asyncio.get_running_loop()
        with self._condition:
            if self._sequence != cursor or self._finished:
                return True
            future = loop.create_future()
            waiter = (loop, future)
            # 检查游标和注册等待者共用锁，避免事件恰好到达时漏唤醒。
            self._waiters.add(waiter)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            return False
        finally:
            with self._condition:
                self._waiters.discard(waiter)

    def put(self, event):
        with self._condition:
            self._sequence += 1
            event = {**event, "seq": self._sequence}
            self._items.append(copy.deepcopy(event))
            self._notify()
            return event

    def read_after(self, cursor):
        with self._condition:
            oldest = self._items[0]["seq"] if self._items else self._sequence + 1
            gap = cursor < oldest - 1 or cursor > self._sequence
            if gap:
                cursor = oldest - 1
            return copy.deepcopy([e for e in self._items if e["seq"] > cursor]), gap

    def get(self, timeout=0):
        # 兼容旧的单消费者接口；SSE 使用 read_after，互不抢事件。
        with self._condition:
            if not self._condition.wait_for(
                lambda: self._sequence > self._legacy_cursor, timeout=timeout
            ):
                raise queue.Empty
            event = next(e for e in self._items if e["seq"] > self._legacy_cursor)
            self._legacy_cursor = event["seq"]
            return copy.deepcopy(event)

    def empty(self):
        with self._condition:
            return self._legacy_cursor >= self._sequence
