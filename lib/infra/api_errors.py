"""Domain exceptions carrying (status_code, i18n key, params).

Conventions:
- the lib / service layer raises with only the i18n key and params, never finished text;
- the app-level exception handler registered by the server (``server/error_handlers.py``) does status code mapping,
  translation per request Accept-Language and redaction in one place; route function bodies keep only the happy path;
- ``str(exc)`` only goes to server logs, never output to clients.

携带 (status_code, i18n key, params) 的领域异常。

约定：
- lib / service 层抛出时只带 i18n key 与 params，不带成品文案；
- server 注册的 app 级 exception handler（``server/error_handlers.py``）单点完成
  状态码映射、按请求 Accept-Language 翻译与脱敏，路由函数体只保留 happy path；
- ``str(exc)`` 只进服务端日志，永不面向客户端输出。
"""

from __future__ import annotations

from typing import Self


class ApiError(Exception):
    """Base class of domain exceptions: the app-level exception handler translates them uniformly into a ``{"detail": ...}`` response.

    ``detail`` is the product-language summary (rendered from i18n key + params), aimed at users; developer-only info such
    as field names, schema and tool identifiers stays out of the summary and goes on the optional ``diagnostic`` instead,
    which the handler appends as a same-named field of the response body when non-empty.

    ``diagnostic`` is deliberately not in the constructor: ``params`` collects render params via ``**kwargs``; if the
    diagnostic also used that keyword space, any render param named ``diagnostic`` would be silently swallowed. Attach it
    with the chained :meth:`with_diagnostic` instead.

    ``diagnostic`` is sent with the response body as is (a note or a structured diagnostic list), so it only holds info the
    request side can restate (field names, schema expectations, raw text of exceptions triggered by client-submitted
    content); server absolute paths, credentials and internal stacks only go to logs.

    领域异常基类：由 app 级 exception handler 统一翻译为 ``{"detail": ...}`` 响应。

    ``detail`` 是产品语言摘要（i18n key + params 渲染），面向使用者；字段名、schema、
    工具标识这类只有开发者看得懂的信息不进摘要，改挂到可选的 ``diagnostic`` 上，
    由 handler 在非空时附加为响应体的同名字段。

    ``diagnostic`` 刻意不进构造函数：``params`` 以 ``**kwargs`` 收集渲染参数，若诊断
    信息也占用同一关键字空间，任何名为 ``diagnostic`` 的渲染参数都会被静默吞掉。
    改用链式的 :meth:`with_diagnostic` 附加。

    ``diagnostic`` 随响应体原样下发（可以是一段说明，也可以是结构化的诊断清单），因此只放
    请求侧可复述的信息（字段名、schema 期望、客户端提交内容触发的异常原文）；服务端绝对路径、
    凭证与内部栈只进日志。
    """

    def __init__(self, key: str, *, status_code: int, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.status_code = status_code
        self.params = params
        self.diagnostic: object | None = None

    def with_diagnostic(self, diagnostic: object) -> Self:
        """Attach technical diagnostic info and return self, for ``raise XxxError(key).with_diagnostic(...)``.

        附加技术诊断信息并返回自身，便于 ``raise XxxError(key).with_diagnostic(...)``。"""
        self.diagnostic = diagnostic
        return self


class BadRequestError(ApiError):
    """Client request error (HTTP 400). / 客户端请求错误（HTTP 400）。"""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, status_code=400, **params)


class UnprocessableError(ApiError):
    """Request format or content cannot be processed (HTTP 422). / 请求格式或内容不可处理（HTTP 422）。"""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, status_code=422, **params)


class NotFoundError(ApiError):
    """The requested resource does not exist (HTTP 404). / 请求的资源不存在（HTTP 404）。"""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, status_code=404, **params)


class ConflictError(ApiError):
    """Conflicts with the current state of the resource (HTTP 409). / 与资源当前状态冲突（HTTP 409）。"""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, status_code=409, **params)


class BadGatewayError(ApiError):
    """Content returned by upstream is unusable or was not retrieved (HTTP 502). / 上游返回的内容不可用或没有取回（HTTP 502）。"""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, status_code=502, **params)


class ServiceUnavailableError(ApiError):
    """Service temporarily unavailable (HTTP 503). / 服务暂时不可用（HTTP 503）。"""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, status_code=503, **params)
