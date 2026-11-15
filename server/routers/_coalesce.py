"""Request coalescing for aggregate reads: at most 1 computing and 1 waiting per key at a time.

聚合读取的请求合流：同一键同时最多 1 份在算、1 份等待。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any


class RequestCoalescer:
    """Merge concurrent requests on the same key into at most two computations.

    When no computation is running, start immediately; when one is running, all new requests attach to the single
    waiting slot and share the next computation.
    The waiting slot starts only after the current computation ends and reads its input only when it starts, so no
    request gets a result older than its arrival time. When a computation fails, the sharers receive the same
    exception, the key is freed, and the next request recomputes.

    In-process; does not cache results across requests. The key must include every request dimension that affects
    the response content (at least the language).
    The computation runs in a separate task and is awaited via ``asyncio.shield``: cancellation of one request
    (client disconnect) only affects itself, and the shared computation and other waiters get their results as
    usual.

    把同一键上的并发请求合进至多两份计算。

    没有计算在跑时立即开算；有计算在跑时，新请求全部挂到唯一的等待位上，共享下一次计算。
    等待位要等当前计算结束才开始，并在开始时才读取输入，所以任何请求拿到的结果都不早于它
    到达的时刻。计算出错时共享者收到同一个异常，键随之空出，下一次请求重新计算。

    进程内、不缓存跨请求的结果。键必须包含所有会影响响应内容的请求维度（至少是语言）。
    计算放在独立任务里、经 ``asyncio.shield`` 等待：某个请求被取消（客户端断开）只影响
    它自己，共享的计算和其他等待者照常拿到结果。
    """

    def __init__(self) -> None:
        self._running: dict[Hashable, asyncio.Task[Any]] = {}
        self._waiting: dict[Hashable, asyncio.Task[Any]] = {}

    async def run[T](self, key: Hashable, compute: Callable[[], Awaitable[T]]) -> T:
        task = self._waiting.get(key)
        if task is None:
            previous = self._running.get(key)
            task = asyncio.create_task(self._compute(key, previous, compute))
            # All requests may have disconnected with nobody taking the exception; retrieve it once so task cleanup
            # does not log "never retrieved".
            # 请求可能已全部断开而无人取走异常；取走一次以免任务回收时记 "never retrieved"。
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            (self._running if previous is None else self._waiting)[key] = task
        return await asyncio.shield(task)

    async def _compute[T](
        self, key: Hashable, previous: asyncio.Task[Any] | None, compute: Callable[[], Awaitable[T]]
    ) -> T:
        me = asyncio.current_task()
        try:
            if previous is not None:
                await asyncio.wait([previous])
                # The waiting slot becomes the active one: requests arriving after this open a new waiting slot and
                # read the newer input.
                # 等待位转正：此后到达的请求另开新的等待位，读取更新的输入。
                self._running[key] = self._waiting.pop(key)
            return await compute()
        finally:
            for slots in (self._running, self._waiting):
                if slots.get(key) is me:
                    del slots[key]
