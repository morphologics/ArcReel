"""Request-level language resolution: take the language from ``Accept-Language`` and inject the route's translator and
display name catalog.

The message tables and per-language rendering (``_`` / ``translate_or``) live in ``lib.i18n``, independent of the
HTTP framework; this only does the layer that maps a request to a language.

请求级语言解析：从 ``Accept-Language`` 取语言，注入路由的 translator 与显示名目录。

文案表与按语言成文（``_`` / ``translate_or``）在 ``lib.i18n``，与 HTTP 框架无关；
这里只做把请求映射到语言的那一层。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db import async_session_factory, get_async_session
from lib.db.repositories.display_names import build_display_names, load_display_names
from lib.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, _
from lib.i18n.display_names import DisplayNames

logger = logging.getLogger(__name__)


def get_locale(request: Request) -> str:
    """Get locale from Accept-Language header."""
    accept_lang = request.headers.get("accept-language", "")
    if not accept_lang:
        return DEFAULT_LOCALE

    # Simple parser for Accept-Language header
    # e.g., "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"
    for lang_range in accept_lang.split(","):
        lang = lang_range.split(";")[0].split("-")[0].strip().lower()
        if lang in SUPPORTED_LOCALES:
            return lang

    return DEFAULT_LOCALE


#: YouReel (G4 machine-readable errors): how many rendered messages one request remembers. A request renders a handful
#: of messages; the cap only guards against a loop rendering thousands (e.g. a list endpoint labelling every row).
#: YouReel（G4 机器可读错误）：单个请求记住的已渲染文案上限。请求通常只渲染几条；上限只防循环渲染成千上万条。
_RENDERED_LIMIT = 256


def _rendered_messages(request: Request) -> dict[str, tuple[str, dict[str, Any]]]:
    """YouReel (G4): the request's record of rendered text → (i18n key, params), kept in the request state.

    ``request.state`` lives in the ASGI scope, so the exception handlers (which get their own ``Request`` object for the
    same scope) read the same record. A stand-in request without state gets a throwaway record.

    YouReel（G4）：请求内「已渲染文案 → (i18n key, params)」的记录，存于 request state。
    ``request.state`` 在 ASGI scope 里，异常处理器（同一 scope 的另一个 ``Request`` 对象）读到同一份记录。
    """
    try:
        state = request.state
        record = getattr(state, "i18n_rendered", None)
        if record is None:
            record = {}
            state.i18n_rendered = record
        return record
    except (AttributeError, KeyError):
        return {}


def rendered_message_source(request: Request, text: object) -> tuple[str, dict[str, Any]] | None:
    """YouReel (G4): the (i18n key, params) a request translator used to render ``text``; None when ``text`` was not
    rendered by this request's translator (composed text, a literal, another request's translator).

    The error handlers use it to give an ``HTTPException(detail=_t("key", **params))`` its machine ``code`` and
    ``params`` without touching the ~360 raise sites.

    YouReel（G4）：本请求的 translator 渲染 ``text`` 时用的 (i18n key, params)；不是本请求 translator
    渲染的文案（拼接文本、字面量）返回 None。错误处理器据此为 ``HTTPException(detail=_t("key", **params))``
    补上机器 ``code`` 与 ``params``，不必改动约 360 处 raise。
    """
    if not isinstance(text, str):
        return None
    return _rendered_messages(request).get(text)


def get_translator(request: Request) -> Callable[..., str]:
    """Dependency to get a translator function for the current request.

    YouReel (G4): every rendering is also recorded (text → key, params) for the error handlers, see
    ``rendered_message_source``. The returned text is a plain ``str``, as before.
    """
    locale = get_locale(request)
    rendered = _rendered_messages(request)

    def translate(key: str, **kwargs: Any) -> str:
        text = _(key, locale=locale, **kwargs)
        # The latest rendering of a text wins: it is the one closest to the raise.
        # 同一文案以最近一次渲染为准：离 raise 最近。
        if text in rendered or len(rendered) < _RENDERED_LIMIT:
            rendered[text] = (key, dict(kwargs))
        return text

    return translate


Translator = Annotated[Callable[..., str], Depends(get_translator)]

#: The request language itself. Used where looking up a translated name needs "fall back to the original name in the
#: data source when the key is missing" (see translate_or); regular rendering still uses Translator.
#: 请求语言本身。取译名需要「键缺失时回退到数据源里的原名」的地方（见 translate_or）用它，
#: 常规成文仍用 Translator。
Locale = Annotated[str, Depends(get_locale)]


def _builtin_display_names(locale: str) -> DisplayNames:
    """Fallback when catalog loading fails: only recognizes built-in providers and models, and shows a generic name for
    custom providers.

    The catalog only decides how providers and models are named in messages; a load failure should not change the
    response itself.

    目录加载失败时的退路：只认内置供应商与模型，自定义供应商显示泛称。

    目录只决定文案里怎么称呼供应商与模型，加载失败不应改变响应本身。
    """
    logger.warning("显示名目录加载失败，使用内置名称", exc_info=True)
    return build_display_names(locale, custom_providers={}, custom_models={})


async def load_display_names_or_builtin(session: AsyncSession, locale: str) -> DisplayNames:
    """Load the display name catalog by language in the route's session; fall back to built-in names when the query
    fails.

    Routes that need to load the catalog on demand inside the function body also call this, not
    ``load_display_names`` directly.

    在路由的会话里按语言加载显示名目录；查询失败时退回内置名称。

    路由需要在函数体内按需加载目录时也调用这里，不直接调用 ``load_display_names``。
    """
    try:
        return await load_display_names(session, locale)
    except Exception:
        # The session is shared by the whole request: roll back the failed query so the route's own queries are not
        # affected.
        # 会话由整个请求共用：回滚失败的查询，路由自己的查询不受牵连。
        await session.rollback()
        return _builtin_display_names(locale)


async def get_display_names(request: Request, session: AsyncSession = Depends(get_async_session)) -> DisplayNames:
    """Route dependency: load the display name catalog of providers and models in the request language; fall back to
    built-in names when the query fails.

    路由依赖：按请求语言加载供应商与模型的显示名目录；查询失败时退回内置名称。"""
    return await load_display_names_or_builtin(session, get_locale(request))


async def request_display_names(request: Request) -> DisplayNames:
    """App-level exception handlers load the catalog in the request language; when the query or session fails, keep the
    original error and fall back to built-in names.

    app 级异常处理器按请求语言加载目录；查询或会话失败时保留原错误，退回内置名称。"""
    locale = get_locale(request)
    try:
        async with async_session_factory() as session:
            return await load_display_names(session, locale)
    except Exception:
        return _builtin_display_names(locale)


#: Display name catalog needed to render failure messages: provider and model IDs are replaced with names in the
#: request language.
#: 失败文案渲染所需的显示名目录：供应商与模型 ID 按请求语言换成名称。
DisplayNamesCatalog = Annotated[DisplayNames, Depends(get_display_names)]
