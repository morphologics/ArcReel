"""Make cancellations swallowed by dependency libraries take effect again in the task's own code.

When a task is inside an anyio task group or cancel scope (httpx enters the ``anyio.connect_tcp`` task group via
httpcore while connecting), if anyio cancels its own scope in the same scheduling round, an external ``Task.cancel()``
merges with it into one anyio cancellation that is swallowed entirely at the scope exit: the task keeps running and
``cancelling()`` stays non-zero.

Don't re-send ``cancel()`` repeatedly from outside: a re-issued cancellation would interrupt the wind-down the task
is doing inside ``asyncio.shield`` (closing the SQLAlchemy session and returning the connection), and the waiter
would return before the wind-down finishes. Instead the task calls :func:`raise_if_cancel_absorbed` at a checkpoint
after dependency library calls, and the waiter cancels only once and waits until the task ends.

让被依赖库吞掉的取消在任务自己的代码里重新生效。

任务在 anyio 的任务组或取消域里时（httpx 建立连接时经 httpcore 进入 ``anyio.connect_tcp`` 的任务组），
anyio 若恰在同一轮调度里取消自己的域，外部的 ``Task.cancel()`` 会与它合并成一个 anyio 取消，在域出口被整个吞掉：
任务继续运行，``cancelling()`` 停在非零。

不从外部反复 ``cancel()`` 补发：重投的取消会打断任务正在 ``asyncio.shield`` 里做的收尾（SQLAlchemy 会话的关闭与
连接归还），等待方返回时收尾还没做完。由任务在依赖库调用之后的检查点调用 :func:`raise_if_cancel_absorbed`，
等待方只取消一次、一直等到任务结束。
"""

from __future__ import annotations

import asyncio


def raise_if_cancel_absorbed() -> None:
    """Raise ``CancelledError`` when the current task has a cancellation request that hasn't taken effect.

    Only called at the task's own checkpoints, where no cancellation is propagating: a non-zero ``cancelling()`` means the
    cancellation request was swallowed by a lower layer.

    当前任务有未生效的取消请求时抛出 ``CancelledError``。

    只在任务自己的检查点调用，此处没有正在传播的取消：``cancelling()`` 非零说明取消请求被下层吞掉了。
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


__all__ = ["raise_if_cancel_absorbed"]
