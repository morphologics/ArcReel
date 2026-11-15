"""Shared httpx AsyncClient singleton.

Initialized at startup and released at shutdown by the lifespan in server/app.py, so external requests don't each
create a new client and lose connection pool reuse. Callers get the instance via `get_http_client()`.

共享 httpx AsyncClient 单例。

由 server/app.py 的 lifespan 在启动时初始化、关闭时释放，避免每次外部请求
都新建客户端、损失连接池复用。调用方使用 `get_http_client()` 获取实例。
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("httpx client not initialized; call startup_http_client() first")
    return _client


# httpx.AsyncClient I/O timeout config, not an async cancellation deadline
async def startup_http_client(timeout: float = 5.0) -> None:  # noqa: ASYNC109 -- httpx.AsyncClient 的 I/O 超时配置，非 async 取消 deadline
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=timeout)


async def shutdown_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
