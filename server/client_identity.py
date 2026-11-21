"""Caller identity of an HTTP request: the optional ``X-Client`` request header.

A program calling the REST API can name itself with ``X-Client: <name>`` (1-32 characters of ``a-z``, ``0-9``, ``.``,
``_``, ``-``). The name is a self-declared label for attribution and filtering, not an authenticated identity:

- generation tasks the request creates record it as ``source`` (``GET /tasks?source=<name>``);
- provider calls made for the request, or for its tasks, record it as the usage record's ``client``
  (``GET /usage/records?client=<name>``).

Without the header the request is the bundled web UI's: ``webui``, as before. A header that does not match the format
(or is sent more than once) is refused with 422 ``client_header_invalid`` on every route, so a typo does not silently
fall back to ``webui``. The agent hosts keep their own labels (``mcp`` for the remote MCP endpoint, ``embedded`` for
the in-process agent); the header does not change how a request is handled, only how its work is labelled.

The dependency is mounted app-wide (it validates the header and sets the client of the request's scope,
``lib/billing/call_client.py``); routes that create tasks take the value through :data:`RequestClient`.

HTTP 请求的调用方身份：可选请求头 ``X-Client``。

调用 REST API 的程序可以用 ``X-Client: <name>`` 报出自己的名字（1–32 个 ``a-z``、``0-9``、``.``、``_``、``-``）。
它是用于归属与筛选的自报标签，不是经过认证的身份：

- 请求创建的生成任务把它记为 ``source``（``GET /tasks?source=<name>``）；
- 为请求或其任务发出的供应商调用把它记为使用记录的 ``client``（``GET /usage/records?client=<name>``）。

不带该请求头即自带 Web 界面的请求，仍记 ``webui``。格式不符（或重复发送）的请求头在所有路由上以 422
``client_header_invalid`` 拒绝，免得拼写错误悄悄回落成 ``webui``。两种 Agent 宿主保留各自的标签（远程 MCP 端点
``mcp``、进程内 Agent ``embedded``）；该请求头不改变请求的处理方式，只改变其产出的标签。

依赖挂在整个 app 上（校验请求头，并设定请求作用域的客户端，见 ``lib/billing/call_client.py``）；创建任务的路由
经 :data:`RequestClient` 取值。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from lib.billing.call_client import is_valid_client_name, set_call_client
from lib.infra.api_errors import UnprocessableError

CLIENT_HEADER = "x-client"
#: The client of a request without ``X-Client``: the bundled web UI. / 不带 ``X-Client`` 的请求的客户端：自带 Web 界面。
DEFAULT_CLIENT = "webui"


async def request_client(connection: HTTPConnection) -> str:
    """The request's client from ``X-Client`` (default ``webui``); also the client of the request's scope.

    Async on purpose: an async dependency runs in the request's own task, so the scope it sets is the one the route
    runs in (a sync dependency would run in a worker thread with a copied context).

    请求的客户端，取自 ``X-Client``（缺省 ``webui``），同时设为请求作用域的客户端。

    刻意写成异步：异步依赖在请求自己的 task 里运行，设定的作用域正是路由运行的那个（同步依赖会在
    工作线程里以复制的上下文运行）。
    """
    values = connection.headers.getlist(CLIENT_HEADER)
    if not values:
        client = DEFAULT_CLIENT
    elif len(values) == 1 and is_valid_client_name(values[0]):
        client = values[0]
    else:
        raise UnprocessableError("client_header_invalid")
    set_call_client(client)
    return client


#: The request's client label, for routes that record it on tasks. / 请求的客户端标签，供把它记到任务上的路由使用。
RequestClient = Annotated[str, Depends(request_client)]

__all__ = ["CLIENT_HEADER", "DEFAULT_CLIENT", "RequestClient", "request_client"]
