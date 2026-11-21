"""The client a provider call is made for: the caller label recorded on usage records (``api_calls.client``).

A client is a short self-declared label of the program that asked for the work (``webui`` for the bundled web UI,
``mcp`` / ``embedded`` for the agent hosts, or whatever an HTTP caller sends in the ``X-Client`` request header). It is
a label for filtering and attribution, not an authenticated identity.

Generation tasks carry it as ``tasks.source``. A provider call that serves a task takes the task's source; a call
without a task takes the client of the scope it runs in (:func:`call_client_scope`): the HTTP request that triggered
it, the generation worker running a task, or an agent tool call. Outside any scope it stays empty (``NULL``).

供应商调用所属的客户端：记在使用记录上的调用方标签（``api_calls.client``）。

客户端是发起这项工作的程序自报的短标签（自带 Web 界面为 ``webui``，两种 Agent 宿主为 ``mcp`` / ``embedded``，
HTTP 调用方则是它在 ``X-Client`` 请求头里给的值），用于筛选与归属，不是经过认证的身份。

生成任务以 ``tasks.source`` 记它。服务某个任务的供应商调用取该任务的 source；没有任务的调用取它所在作用域
（:func:`call_client_scope`）的客户端：触发它的 HTTP 请求、执行任务的生成 worker 或 Agent 工具调用。
不在任何作用域内时留空（``NULL``）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: Accepted client labels: 1-32 characters of lower-case ASCII letters, digits, ``.``, ``_`` and ``-``.
#: 可接受的客户端标签：1–32 个小写 ASCII 字母、数字、``.``、``_``、``-``。
CLIENT_NAME_PATTERN = re.compile(r"[a-z0-9._-]{1,32}")

_current_client: ContextVar[str | None] = ContextVar("arcreel_call_client", default=None)


def is_valid_client_name(value: str) -> bool:
    """Whether ``value`` is an acceptable client label (see :data:`CLIENT_NAME_PATTERN`).

    ``value`` 是否为可接受的客户端标签（见 :data:`CLIENT_NAME_PATTERN`）。"""
    return CLIENT_NAME_PATTERN.fullmatch(value) is not None


def current_call_client() -> str | None:
    """The client of the scope the code runs in; ``None`` outside any scope.

    当前代码所在作用域的客户端；不在任何作用域内时为 ``None``。"""
    return _current_client.get()


def set_call_client(client: str | None) -> None:
    """Set the client for the rest of the current context (an HTTP request's task, for example).

    Use it where the context ends with the unit of work (a request dependency); elsewhere use :func:`call_client_scope`,
    which restores the previous value.

    为当前上下文的剩余部分设定客户端（例如一个 HTTP 请求的 task）。

    只用于上下文随工作单元一起结束的地方（请求依赖）；其余地方用会恢复原值的 :func:`call_client_scope`。
    """
    _current_client.set(client)


@contextmanager
def call_client_scope(client: str | None) -> Iterator[None]:
    """Run the block with ``client`` as the current client, then restore the previous one.

    以 ``client`` 为当前客户端执行代码块，结束后恢复原值。"""
    token = _current_client.set(client)
    try:
        yield
    finally:
        _current_client.reset(token)


__all__ = [
    "CLIENT_NAME_PATTERN",
    "call_client_scope",
    "current_call_client",
    "is_valid_client_name",
    "set_call_client",
]
