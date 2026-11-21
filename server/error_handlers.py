"""App-level exception handlers: the single point for the exception → status code → detail mapping.

Route function bodies keep only the happy path; domain exceptions and lib-layer exceptions are handled here
uniformly:

- Status code mapping (``ApiError`` carries its own; lib exceptions are fixed by type)
- Translation by the request's ``Accept-Language`` (reusing ``get_translator``), with provider and model IDs
  replaced by display names
- Redaction: apart from params explicitly declared by the i18n key, exception messages are never returned to the
  client -- ``str(exc)`` of ``FileNotFoundError`` / unexpected exceptions may contain absolute server paths and only
  goes to the log

- YouReel (G4): machine-readable ``code`` + ``params`` beside ``detail`` in every error body (see the block below
  ``logger``)

Gradual migration: routes that still do their own try/except are unaffected (the exception never propagates here);
migrating an endpoint = deleting its except ladder and letting the exception propagate naturally.

app 级异常处理器：异常→状态码→detail 映射的单点。

路由函数体只保留 happy path，领域异常与 lib 层异常在此统一完成：

- 状态码映射（``ApiError`` 自带；lib 异常按类型固定）
- 按请求 ``Accept-Language`` 翻译（复用 ``get_translator``），供应商与模型 ID 换成显示名
- 脱敏：除 i18n key 显式声明的 params 外，异常消息一律不回传客户端——
  ``FileNotFoundError`` / 未预期异常的 ``str(exc)`` 可能含服务器绝对路径，只进日志
- YouReel（G4）：每个错误体在 ``detail`` 旁带机器可读的 ``code`` + ``params``（见 ``logger`` 下方的代码块）

渐进迁移：仍自行 try/except 的路由不受影响（异常不会传播到这里）；
迁移一个端点 = 删掉它的 except 阶梯，让异常自然传播。
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from lib.catalog.errors import ModelNotInCatalogError
from lib.generation.generation_queue import ActiveTaskRequestConflict
from lib.generation.generation_queue_client import TaskSpecValidationError
from lib.generation.task_failure import FAILURE_CODE_KEYS
from lib.i18n import render_generation_input_error, render_message
from lib.infra.api_errors import ApiError
from lib.script.script_editor import ScriptEditError
from server.i18n import get_translator, rendered_message_source, request_display_names
from server.services.project.episode_display import present_request_diagnostics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YouReel (G4 machine-readable errors): every error body carries ``code`` + ``params`` beside the translated
# ``detail``, so API clients (YouReel Flow) branch on codes, never on localized text. The contract is documented in
# deploy/studio/reference/STUDIO-API-MAP.md §4.7 (sbi repo).
# YouReel（G4 机器可读错误）：每个错误体在已翻译的 ``detail`` 旁带 ``code`` + ``params``，
# API 客户端（YouReel Flow）按码分支，不按本地化文案分支。
# ---------------------------------------------------------------------------

#: Code of an error that has no own code, by HTTP status. / 没有自身错误码的错误按 HTTP 状态取的通用码。
GENERIC_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    408: "request_timeout",
    409: "conflict",
    410: "gone",
    412: "precondition_failed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    423: "locked",
    428: "precondition_required",
    429: "rate_limited",
    500: "internal_server_error",
    501: "not_implemented",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}

#: Code of FastAPI request validation failures (422 with the field errors). / 请求校验失败（422 带字段错误）的码。
VALIDATION_FAILED_CODE = "validation_failed"

_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# Param names whose values are never echoed in ``params`` (credentials). ``max_tokens`` / ``key_id`` stay.
# 值永不回显到 ``params`` 的参数名（凭证）。``max_tokens`` / ``key_id`` 保留。
_SECRET_PARAM = re.compile(
    r"(?:^|_)(?:api_?key|secret|password|passwd|token|credential|credentials|authorization|cookie)$", re.IGNORECASE
)
# Absolute server paths inside a param value (POSIX with two or more segments, Windows drive paths). Relative project
# paths (``scripts/episode_1.json``) and URLs stay.
# 参数值里的服务器绝对路径（两段以上的 POSIX 路径、Windows 盘符路径）。项目相对路径与 URL 保留。
_ABSOLUTE_PATH = re.compile(r"(?<![\w.:/~-])/(?:[^\s/'\"]+/)+[^\s'\"]*|\b[A-Za-z]:[\\/][^\s'\"]*")
_PATH_PLACEHOLDER = "<path>"


class ErrorResponse(BaseModel):
    """YouReel (G4): the body of every error response, for the OpenAPI document (``ERROR_RESPONSES``).

    ``detail`` is the translated text for people (a list of field errors for request validation, or a structured
    object on a few routes); clients branch on ``code`` and read values from ``params``.

    YouReel（G4）：所有错误响应的响应体，供 OpenAPI 文档使用。``detail`` 是给人看的译文（请求校验时是字段错误列表，
    少数路由是结构化对象）；客户端按 ``code`` 分支，从 ``params`` 取值。"""

    detail: Any = Field(description="Translated message (Accept-Language); a list for request validation")
    code: str = Field(description="Stable machine-readable error code (snake_case)", examples=["segment_not_found"])
    params: dict[str, Any] = Field(
        default_factory=dict, description="Values the message was rendered from; no secrets, no server paths"
    )
    diagnostic: Any | None = Field(default=None, description="Optional technical diagnostic (field names, schema)")


#: App-level OpenAPI ``responses``: every operation may answer 4XX / 5XX with ``ErrorResponse``.
#: app 级 OpenAPI ``responses``：每个操作都可能以 ``ErrorResponse`` 回 4XX / 5XX。
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "4XX": {"model": ErrorResponse, "description": "Client error"},
    "5XX": {"model": ErrorResponse, "description": "Server error"},
}


def generic_error_code(status_code: int) -> str:
    """Generic machine code for a status (``not_found``, ``conflict``, …); unlisted 4xx → ``client_error``, 5xx →
    ``server_error``. / 按状态取通用机器码；未列出的 4xx → ``client_error``，5xx → ``server_error``。"""
    if status_code in GENERIC_ERROR_CODES:
        return GENERIC_ERROR_CODES[status_code]
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500:
        return "server_error"
    return "error"


def _as_code(value: object) -> str | None:
    """``value`` when it is a usable snake_case code. / ``value`` 是可用的 snake_case 码时原样返回。"""
    return value if isinstance(value, str) and _CODE_PATTERN.match(value) else None


def code_or_generic(value: object, status_code: int) -> str:
    """``value`` when it is a usable snake_case code, else the generic code of the status; for routes that build an
    error body themselves. / ``value`` 是可用的 snake_case 码时用它，否则取状态的通用码；供自行构造错误体的路由使用。"""
    return _as_code(value) or generic_error_code(status_code)


def _json_safe(value: Any) -> Any:
    """JSON-safe copy of a param value: primitives as is, enums by value, containers recursively, pydantic models
    dumped, nested messages as ``{code, params}``, anything else as its text (never ``vars()`` of an object).

    参数值的 JSON 安全副本：基本类型原样，枚举取值，容器递归，pydantic 模型导出，其余取文本（不取对象的 ``vars()``）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseModel):
        return jsonable_encoder(value)
    key, params = getattr(value, "key", None), getattr(value, "params", None)
    if isinstance(key, str) and isinstance(params, Mapping):
        # A nested locale-neutral message (``ValidationMessage``): its code and params, not its repr.
        # 嵌套的 locale-neutral 消息（``ValidationMessage``）：取其码与参数，不取 repr。
        return {"code": key, "params": _json_safe(params)}
    return str(value)


def _scrub(value: Any) -> Any:
    """Mask absolute server paths in strings, recursively. / 递归遮蔽字符串里的服务器绝对路径。"""
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub(_PATH_PLACEHOLDER, value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def machine_params(request: Request | None, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """JSON-safe ``params`` of an error body: the values the message was rendered from.

    Credentials (by param name) are left out and absolute server paths are masked; a value that is itself a message
    rendered by the request's translator (e.g. ``reason=_t("…")``) also gets its code as ``<name>_code``.

    错误体的 JSON 安全 ``params``：渲染文案所用的值。按参数名剔除凭证、遮蔽服务器绝对路径；
    值本身是本请求 translator 渲染的文案时（如 ``reason=_t("…")``），另以 ``<name>_code`` 给出其码。
    """
    if not params:
        return {}
    result: dict[str, Any] = {}
    for name, value in params.items():
        name = str(name)
        if _SECRET_PARAM.search(name):
            continue
        result[name] = _scrub(_json_safe(value))
        if request is not None and (source := rendered_message_source(request, value)) is not None:
            if (code := _as_code(source[0])) is not None and f"{name}_code" not in params:
                result[f"{name}_code"] = code
    return result


def _structured_detail_code(detail: Mapping[str, Any]) -> str | None:
    """Code of a structured detail: its own ``code``, else the first problem's (speech admission, script edit result,
    reference projection). / 结构化 detail 的码：自身 ``code``，否则取第一个问题的码。"""
    if (code := _as_code(detail.get("code"))) is not None:
        return code
    problems = detail.get("problems")
    if isinstance(problems, list) and problems and isinstance(problems[0], Mapping):
        return _as_code(problems[0].get("code"))
    return None


def http_exception_code_and_params(request: Request, exc: StarletteHTTPException) -> tuple[str, dict[str, Any]]:
    """Machine ``code`` and ``params`` of an ``HTTPException``, from what its ``detail`` already is:

    - text rendered by the request translator (``detail=_t("key", **params)``) → that key and params;
    - a bare snake_case code as detail (``detail="no_editor_save"``) → that code;
    - a structured dict detail → its ``code`` (or the first problem's); params stay inside ``detail``;
    - anything else (literal text, lists) → the generic code of the status.

    ``HTTPException`` 的机器 ``code`` 与 ``params``，按其 ``detail`` 本身推出：translator 渲染的文案 → 其 key 与
    params；裸 snake_case 码 → 该码；结构化 dict → 其 ``code``（或第一个问题的码），params 留在 detail 内；
    其余（字面文本、列表）→ 状态的通用码。
    """
    detail = exc.detail
    if (source := rendered_message_source(request, detail)) is not None and (code := _as_code(source[0])):
        return code, machine_params(request, source[1])
    if (code := _as_code(detail)) is not None:
        return code, {}
    if isinstance(detail, Mapping) and (code := _structured_detail_code(detail)) is not None:
        return code, {}
    return generic_error_code(exc.status_code), {}


def error_body(detail: Any, code: str, params: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """The error body: ``{"detail", "code", "params", …extra}`` (``diagnostic`` etc. only when given).

    错误体：``{"detail", "code", "params", …extra}``（``diagnostic`` 等仅在给出时出现）。"""
    body: dict[str, Any] = {"detail": detail, "code": code, "params": dict(params or {})}
    body.update({key: value for key, value in extra.items() if value is not None})
    return body


def _validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Compact field errors for ``params.errors``: ``type`` / ``loc`` / ``msg`` only -- not the echoed ``input``
    (which can be a whole request body, e.g. base64 images) nor ``ctx``.

    ``params.errors`` 用的精简字段错误：只留 ``type`` / ``loc`` / ``msg``，不回显 ``input``（可能是整个请求体，
    如 base64 图片）与 ``ctx``。"""
    compact: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        compact.append(
            {
                "type": str(error.get("type", "")),
                "loc": [part if isinstance(part, (str, int)) else str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
            }
        )
    return compact


def script_edit_detail(exc: ScriptEditError, _t: Callable[..., str]) -> str:
    """Single-point mapping of ``ScriptEditError`` → user-visible detail.

    reason is translated in the request language via ``exc.key``/``exc.params`` and does not embed ``str(exc)`` --
    the latter has fixed Chinese text and would mix Chinese into en/vi responses. Routes that still do their own
    ``except ScriptEditError`` share this function with this module's global handler, so the same expression is not
    scattered around evolving separately.

    ``ScriptEditError`` → 用户可见 detail 的单点映射。

    reason 走 ``exc.key``/``exc.params`` 按请求语言翻译，不嵌 ``str(exc)``——后者文案固定
    中文，会在 en/vi 响应里混入中文原文。仍自行 ``except ScriptEditError`` 的路由与本模块
    的全局 handler 共用本函数，避免同一表达式散落多处各自演化。
    """
    return _t("script_data_corrupted", reason=_t(exc.key, **exc.params))


def _cors_headers_for(
    request: Request,
    *,
    allow_origins: Sequence[str],
    allow_credentials: bool,
) -> dict[str, str]:
    """Manually add CORS headers to the fallback 500 response, logic aligned with Starlette ``CORSMiddleware.send()``.

    When ``ServerErrorMiddleware`` sends the 500 response for an unexpected exception, it uses the outermost raw
    ``send`` (closure parameter), bypassing every inner middleware's send wrapper -- including ``CORSMiddleware``
    registered further inside. A cross-origin frontend therefore only sees a network error, not the real 500 body.
    Reordering middleware registration does not help (``Exception``/500 always routes to ``ServerErrorMiddleware``),
    so the only option is to compute and attach the headers by hand in the handler with the same CORS config as the
    app.

    为兜底 500 响应手工补 CORS 头，逻辑对齐 Starlette ``CORSMiddleware.send()``。

    ``ServerErrorMiddleware`` 发送未预期异常的 500 响应时用的是最外层原始
    ``send``（闭包参数），绕过所有内层中间件的 send 包装——包括注册在更内层的
    ``CORSMiddleware``。跨域前端因此只会看到 network error，看不到真正的
    500 body。调整中间件注册顺序无效（``Exception``/500 恒定路由到
    ``ServerErrorMiddleware``），只能在 handler 内按与 app 一致的 CORS 配置
    手工计算并附加响应头。
    """
    origin = request.headers.get("origin")
    if origin is None:
        return {}

    allow_all_origins = "*" in allow_origins
    headers: dict[str, str] = {}
    if allow_all_origins:
        headers["Access-Control-Allow-Origin"] = "*"
    if allow_credentials:
        headers["Access-Control-Allow-Credentials"] = "true"

    if (allow_all_origins and allow_credentials) or (not allow_all_origins and origin in allow_origins):
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"

    return headers


def register_error_handlers(
    app: FastAPI,
    *,
    cors_allow_origins: Sequence[str] = ("*",),
    cors_allow_credentials: bool = False,
) -> None:
    """Register all app-level exception handlers. Also works on a bare ``FastAPI()`` in tests.

    ``cors_allow_origins``/``cors_allow_credentials`` must match the ``CORSMiddleware`` config actually registered
    on the app (see the notes in ``_handle_unexpected`` and the call site in ``server/app.py``); the defaults
    correspond to the conservative "CORS not configured" case.

    注册全部 app 级异常处理器。测试中对 bare ``FastAPI()`` 同样适用。

    ``cors_allow_origins``/``cors_allow_credentials`` 须与 app 上实际注册的
    ``CORSMiddleware`` 配置一致（见 ``_handle_unexpected`` 内的说明与
    ``server/app.py`` 调用处），默认值对应「未配置 CORS」的保守场景。
    """

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        _t = get_translator(request)
        names = await request_display_names(request)
        # code = the i18n key (or the problem code raised as the key); params = the raw render params (machine IDs,
        # not display names).
        # code = i18n key（或作为 key 抛出的问题码）；params = 原始渲染参数（机器 ID，不是显示名）。
        content = error_body(
            render_generation_input_error(exc.key, exc.params, _t, names),
            _as_code(exc.key) or generic_error_code(exc.status_code),
            machine_params(request, exc.params),
            diagnostic=exc.diagnostic,
        )
        content = await present_request_diagnostics(content, request, _t)
        return JSONResponse(status_code=exc.status_code, content=content)

    # YouReel (G4): registered on Starlette's base class, so the router's own 404 / 405 (raised as Starlette
    # HTTPException, not FastAPI's subclass) carry a code too.
    # YouReel（G4）：注册在 Starlette 基类上，路由器自身的 404 / 405（Starlette HTTPException）同样带码。
    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code in {204, 304}:
            # Bodiless statuses, as Starlette's default handler. / 无响应体的状态，同 Starlette 默认处理。
            return Response(status_code=exc.status_code, headers=exc.headers)
        # Code and params come from the detail as raised, before presentation rewrites the text.
        # 码与参数按抛出时的 detail 取，在呈现改写文案之前。
        code, params = http_exception_code_and_params(request, exc)
        detail = await present_request_diagnostics(exc.detail, request, get_translator(request))
        return JSONResponse(
            status_code=exc.status_code, content=error_body(detail, code, params), headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        _t = get_translator(request)
        error_types = {error["type"] for error in exc.errors()}
        for key in ("assistant_image_too_large", "assistant_images_total_too_large"):
            if key in error_types:
                return JSONResponse(status_code=422, content=error_body(_t(key), key))
        # detail stays FastAPI's list (the studio UI joins its ``msg``s); params.errors repeats it compactly.
        # detail 保持 FastAPI 的列表（studio UI 拼接其 ``msg``）；params.errors 精简复述。
        return JSONResponse(
            status_code=422,
            content=error_body(
                jsonable_encoder(exc.errors()),
                VALIDATION_FAILED_CODE,
                {"errors": _validation_errors(exc.errors())},
            ),
        )

    @app.exception_handler(TaskSpecValidationError)
    async def _handle_task_spec_error(request: Request, exc: TaskSpecValidationError) -> JSONResponse:
        _t = get_translator(request)
        names = await request_display_names(request)
        detail = await present_request_diagnostics(render_message(exc.code, exc.params, _t, names), request, _t)
        code = _as_code(exc.code) or generic_error_code(400)
        return JSONResponse(status_code=400, content=error_body(detail, code, machine_params(request, exc.params)))

    @app.exception_handler(ModelNotInCatalogError)
    async def _handle_model_not_in_catalog(request: Request, exc: ModelNotInCatalogError) -> JSONResponse:
        # A built-in model referenced in settings or the project is no longer in the provider's model list (delisted):
        # prompt the user to choose again, do not treat it as a server error
        # 设置或项目里引用的内置模型已不在供应商的模型清单中（已下架）：提示用户重新选择，不当作服务器错误
        _t = get_translator(request)
        names = await request_display_names(request)
        message = render_message(FAILURE_CODE_KEYS[exc.code], exc.params, _t, names)
        detail = await present_request_diagnostics(message, request, _t)
        # code = the stable failure code (model_not_in_provider_catalog / provider_unsupported_media), as the worker
        # persists it. / code = 稳定失败码，与 worker 落库一致。
        return JSONResponse(status_code=400, content=error_body(detail, exc.code, machine_params(request, exc.params)))

    @app.exception_handler(ActiveTaskRequestConflict)
    async def _handle_active_task_request_conflict(
        request: Request,
        exc: ActiveTaskRequestConflict,
    ) -> JSONResponse:
        _t = get_translator(request)
        detail = await present_request_diagnostics(
            _t("video_request_conflicts_with_active_task", resource_id=exc.resource_id), request, _t
        )
        return JSONResponse(
            status_code=409,
            content=error_body(
                detail,
                "video_request_conflicts_with_active_task",
                machine_params(request, {"resource_id": exc.resource_id, "existing_task_id": exc.existing_task_id}),
            ),
        )

    @app.exception_handler(ScriptEditError)
    async def _handle_script_edit_error(request: Request, exc: ScriptEditError) -> JSONResponse:
        # Dirty script (corrupted shot array key, etc.) → 4xx client error / 脏脚本（分镜数组键损坏等）→ 4xx 客户端错误
        _t = get_translator(request)
        detail = await present_request_diagnostics(script_edit_detail(exc, _t), request, _t)
        # The reason's own key and params ride along as reason_code / reason_params.
        # reason 自身的 key 与 params 以 reason_code / reason_params 附带。
        params = {
            "reason_code": _as_code(exc.key) or "script_edit_error",
            "reason_params": machine_params(request, exc.params),
        }
        return JSONResponse(status_code=400, content=error_body(detail, "script_data_corrupted", params))

    @app.exception_handler(FileNotFoundError)
    async def _handle_file_not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
        # Do not return str(exc): exception messages from load_script etc. contain absolute server paths, log only
        # 不回传 str(exc)：load_script 等异常消息含服务器绝对路径，只进日志
        logger.warning("资源不存在: %s %s (%s)", request.method, request.url.path, exc)
        _t = get_translator(request)
        return JSONResponse(status_code=404, content=error_body(_t("resource_not_found"), "resource_not_found"))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Messages of unexpected exceptions may contain internal details such as server paths, so always return a
        # generic 500. Starlette re-raises after sending this response, and the stack is logged by
        # request_logging_middleware / uvicorn, so it is not printed again here.
        #
        # ServerErrorMiddleware always sends this response with the outermost raw send, bypassing CORSMiddleware (see
        # the _cors_headers_for docs) -- add the CORS headers by hand, otherwise a cross-origin frontend would treat
        # this 500 as a network error.
        # 未预期异常的消息可能含服务器路径等内部细节，一律通用 500。
        # Starlette 发送本响应后会 re-raise，堆栈由 request_logging_middleware /
        # uvicorn 记录，此处不重复打印。
        #
        # ServerErrorMiddleware 恒定用最外层原始 send 发送本响应，绕过
        # CORSMiddleware（见 _cors_headers_for 文档）——手工补上 CORS 头，
        # 否则跨域前端会把这个 500 当成 network error。
        _t = get_translator(request)
        headers = _cors_headers_for(
            request,
            allow_origins=cors_allow_origins,
            allow_credentials=cors_allow_credentials,
        )
        return JSONResponse(
            status_code=500,
            content=error_body(_t("internal_server_error"), "internal_server_error"),
            headers=headers,
        )
