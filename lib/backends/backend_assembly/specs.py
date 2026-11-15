"""Declarative table (provider_id, media_type) → ProviderSpec for built-in providers.

Mirrors the custom side's ENDPOINT_REGISTRY (lib/custom_provider/endpoints.py): each spec is a frozen dataclass carrying
a build closure; the closure reads the LoadedConfig envelope + model_id to build the backend, without querying the DB or
awaiting. It registers image/video/audio for the simple family (built-in providers whose media side only needs api_key +
model + base_url), sharing one _build_simple closure; plus two special families, gemini (backend_type dual mode,
image/video/audio pass base_url through equally) and kling (JWT dual secrets + decoupled api_model_name), each with its
own build closure. All four media_type values including text (media_type=text) go through this table: simple text
(ark/ark-agent-plan/grok/agnes; agnes uses OpenAI-compatible chat/completions but its auth / base_url are handled in the
dedicated AgnesTextBackend, so it belongs to the simple family rather than the OpenAI-compat aliases), gemini text
(aistudio/vertex), and OpenAI-compat text (openai/dashscope/minimax; dashscope/minimax derive base_url via a helper +
pass provider_name through for billing attribution), each with its own closure; the text-side alias mapping
(dashscope/minimax → openai registry backend) is merged into the spec's registry_backend field, eliminating the old text
factory's second PROVIDER_ID_TO_BACKEND. The table validates its invariants at import time (except that registry names
are registered, see the note at the end of the module); misconfiguration fails fast.

内置 provider 的 (provider_id, media_type) → ProviderSpec 声明式表。

镜像自定义侧 ENDPOINT_REGISTRY（lib/custom_provider/endpoints.py）：每条 spec 是 frozen
dataclass，挂一个 build 闭包；闭包读 LoadedConfig 信封 + model_id 拼 backend，不查 DB、不 await。
登记简单族（媒体侧只需 api_key + model + base_url 的内置 provider）的 image/video/audio，
共享一个 _build_simple 闭包；外加 gemini（backend_type 双模式，image/video/audio 对等透传 base_url）与
kling（JWT 双 secret + api_model_name 解耦）两个特例族，各自挂专属 build 闭包。文本（media_type=text）
四条 media_type 同经本表：简单文本（ark/ark-agent-plan/grok/agnes，agnes 走 OpenAI 兼容
chat/completions 但鉴权 / base_url 收口在专属 AgnesTextBackend，故归简单族而非 OpenAI-compat 别名）、gemini 文本（aistudio/vertex）、
OpenAI-compat 文本（openai/dashscope/minimax，dashscope/minimax 经 helper 派生 base_url + 透传
provider_name 计费归因）各挂专属闭包；文本侧别名映射（dashscope/minimax → openai registry backend）
并入 spec 的 registry_backend 字段，根除原文本工厂第二份 PROVIDER_ID_TO_BACKEND。表在 import 期校验
不变式（registry 名已注册除外，见模块末尾说明），misconfig fail-fast。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from lib.backends.kling_shared import kling_auth_mode
from lib.backends.providers import PROVIDER_KLING
from lib.catalog import query as catalog

if TYPE_CHECKING:
    from arcreel_market_core.video_backend_contract import (
        VideoCapabilities,
        VideoGenerationRequest,
        VideoGenerationResult,
    )
    from lib.backends.backend_assembly.loaded_config import LoadedConfig
    from lib.billing.metering import Usage


@dataclass(frozen=True)
class ProviderSpec:
    """Backend construction spec for one built-in (provider, media). / 单条内置 (provider, media) 的 backend 构造规格。"""

    provider_id: str  # registry / config provider id, e.g. "ark" / registry / config provider id，如 "ark"
    media_type: str  # "image" | "video" | "audio" | "text"
    # which media backend registry name it maps to (merging the two PROVIDER_ID_TO_BACKEND tables)
    registry_backend: str  # 映射到哪个 media backend registry 名（合并两份 PROVIDER_ID_TO_BACKEND）
    # model_id is None only when the model catalog designates no default model for this provider and media type; the backend handles it itself
    # model_id 为 None 只在模型目录没有给该供应商该媒体类型指定默认模型时出现，由 backend 自行处理
    build_backend: Callable[[LoadedConfig, str | None], Any]


def _media_create_backend(media_type: str) -> Callable[..., Any]:
    """Get the create_backend of the registry for media_type (fetched at runtime so tests can patch module attributes).

    The four media_type values text and image/video/audio share one construction seam; text backends are registered in the
    separate lib.backends.text_backends.registry, hence the routing here.

    按 media_type 取对应 registry 的 create_backend（运行时取，便于测试 patch 模块属性）。

    text 与 image/video/audio 四条 media_type 共用同一构造缝，文本 backend 注册在
    独立的 lib.backends.text_backends.registry，故在此分流。
    """
    if media_type == "image":
        from lib.backends.image_backends.registry import create_backend
    elif media_type == "video":
        from lib.backends.video_backends.registry import create_backend
    elif media_type == "audio":
        from lib.backends.audio_backends.registry import create_backend
    elif media_type == "text":
        from lib.backends.text_backends.registry import create_backend
    else:
        raise ValueError(f"unknown media_type: {media_type!r}")
    return create_backend


def _resolve_base_url(config: LoadedConfig) -> str | None:
    """base_url precedence: explicitly entered by the user in db_config > ProviderMeta.default_base_url > None.

    The simple family and kling share this fallback semantics; each caller decides whether to omit the base_url argument when None.

    base_url 优先级：用户在 db_config 显式填写 > ProviderMeta.default_base_url > None。

    简单族与 kling 共用此兜底语义；调用方各自决定 None 时是否省略 base_url 参数。
    """
    default = config.provider_meta.default_base_url if config.provider_meta else None
    return config.credentials.get("base_url") or default


def _build_simple(
    config: LoadedConfig,
    model_id: str | None,
    *,
    media_type: str,
    registry_backend: str,
    extra_keys: tuple[str, ...] = (),
) -> Any:
    """Generic simple-family construction: api_key + model + base_url, plus db_config pass-through keys declared in extra_keys.

    api_key and base_url both follow "only written into kwargs when non-empty": passing None explicitly might override the
    underlying SDK's env var fallback (e.g. the OpenAI SDK reads OPENAI_API_KEY); defaults are handled by each backend (either
    reading env vars or failing loud). For base_url precedence see _resolve_base_url -- providers like grok with no default
    and nothing configured by the user don't accept a base_url argument, and passing None would raise TypeError, so it is
    only written when non-empty. extra_keys follow the same rule: key names are backend constructor parameter names, letting
    an individual provider's dedicated endpoint config (e.g. dashscope's wan3_base_url) reuse the same construction seam.

    简单族通用构造：api_key + model + base_url，外加 extra_keys 声明的 db_config 直传键。

    api_key 与 base_url 同遵「仅非空才写入 kwargs」：显式传 None 可能覆盖底层 SDK 的环境变量兜底
    （如 OpenAI SDK 读 OPENAI_API_KEY），缺省由 backend 各自处理（要么读环境变量、要么 fail-loud）。
    base_url 优先级见 _resolve_base_url —— grok 等无 default 且用户未配的 provider 不接受 base_url
    参数，传 None 会触发 TypeError，故仅非空才写入。extra_keys 同规则：键名即 backend 构造参数名，
    供个别 provider 的专属 endpoint 配置（如 dashscope 的 wan3_base_url）复用同一条构造缝。
    """
    kwargs: dict[str, Any] = {"model": model_id}
    api_key = config.credentials.get("api_key")
    if api_key:
        kwargs["api_key"] = api_key
    base_url = _resolve_base_url(config)
    if base_url:
        kwargs["base_url"] = base_url
    for key in extra_keys:
        value = config.credentials.get(key)
        if value:
            kwargs[key] = value
    return _media_create_backend(media_type)(registry_backend, **kwargs)


def _simple_spec(provider_id: str, media_type: str, *, extra_keys: tuple[str, ...] = ()) -> ProviderSpec:
    """Register one simple-family spec: registry_backend is the provider_id itself (no alias mapping on the media side).

    登记一条简单族 spec：registry_backend 即 provider_id 自身（媒体侧无别名映射）。"""
    return ProviderSpec(
        provider_id=provider_id,
        media_type=media_type,
        registry_backend=provider_id,
        build_backend=partial(
            _build_simple, media_type=media_type, registry_backend=provider_id, extra_keys=extra_keys
        ),
    )


def _minimax_video_endpoint(model_id: str | None) -> str:
    """model → declarative endpoint key. One definition defines one request shape + one capability set, so Fast and 2.3 live under separate keys.

    Shares its model name rules with :func:`lib.custom_provider.endpoints.infer_endpoint` and alembic ``8c2b1e7d4a90``
    (each of the three keeps its own literal; changing one requires changing the other two): Fast only accepts
    image-to-video, and its definition declares the first frame as a required input, from which ``text_to_video`` is derived as False.

    model → 声明式端点键。一份定义一种请求形状 + 一组能力，故 Fast 与 2.3 分居两键。

    与 :func:`lib.custom_provider.endpoints.infer_endpoint` 和 alembic ``8c2b1e7d4a90``
    的模型名口径同源（三处各存一份字面量，改其一须同改另两处）：Fast 只接受图生视频，
    定义把首帧声明为必需输入，``text_to_video`` 才据此推导为 False。
    """
    model = (model_id or "").lower()
    if model_id == "S2V-01":
        return "minimax-s2v-01"
    if "minimax-h3" in model:
        return "minimax-h3"
    if "hailuo" in model and "fast" in model:
        return "minimax-hailuo-v1-fast"
    return "minimax-hailuo-v1"


def builtin_declarative_video_diagnostics(
    provider_id: str, model_id: str, config: Mapping[str, str]
) -> tuple[Mapping[str, Any], str, str] | None:
    """``(definition, base_url, api_key)`` for trial run diagnostics when a built-in model row uses declarative assembly.

    Returns None when the (provider, model) is implemented by a Python backend. Endpoint resolution and credential fallback
    are the same as :func:`_build_minimax_declarative_video` -- the request rendered in diagnostics must match the one
    actually sent. Callers must first confirm ``api_key`` is non-empty via their own credential gate (an empty value fails loud here).

    内置模型行走声明式装配时，测试连接诊断用的 ``(definition, base_url, api_key)``。

    该 (provider, model) 由 Python backend 实现时返回 None。端点判定与凭证回落与
    :func:`_build_minimax_declarative_video` 同一份——诊断里渲出的请求必须与真发的一致。
    调用方须先按各自的凭证闸确认 ``api_key`` 非空（空值在此处会 fail-loud）。
    """
    if provider_id != "minimax":
        return None
    from lib.backends.minimax_shared import MINIMAX_BASE_URL, resolve_minimax_api_key
    from lib.custom_provider.endpoints import get_endpoint_spec

    definition = get_endpoint_spec(_minimax_video_endpoint(model_id)).definition
    if definition is None:
        return None
    return definition, config.get("base_url") or MINIMAX_BASE_URL, resolve_minimax_api_key(config.get("api_key"))


def _build_minimax_declarative_video(config: LoadedConfig, model_id: str | None) -> Any:
    from lib.backends.minimax_shared import MINIMAX_BASE_URL, resolve_minimax_api_key
    from lib.custom_provider.declarative_backend import DeclarativeVideoBackend
    from lib.custom_provider.endpoints import get_endpoint_spec

    model = model_id or "MiniMax-H3"
    endpoint = _minimax_video_endpoint(model)
    definition = get_endpoint_spec(endpoint).definition
    if definition is None:
        raise ValueError(f"builtin endpoint {endpoint!r} is not declarative")
    return DeclarativeVideoBackend(
        api_key=resolve_minimax_api_key(config.credentials.get("api_key")),
        base_url=_resolve_base_url(config) or MINIMAX_BASE_URL,
        model=model,
        definition=definition,
        provider="minimax",
    )


# ── gemini special family ──────────────────────────────────────────────────
# Both provider_ids gemini-aistudio / gemini-vertex map to the same "gemini" media backend,
# differing only in backend_type (aistudio | vertex) -- declared by spec rows (one per provider_id), not by an if inside the closure.
# image/video/audio pass base_url through equally: aistudio uses the user-configured custom endpoint. api_key and base_url are passed through
# unconditionally (including None); the backend's resolve_gemini_api_key / normalize_base_url handle None (None base_url is omitted);
# the vertex branch uses vertexai=True + service account credentials and structurally ignores base_url (no injection point), so the passed None is discarded.
# ── gemini 特例族 ──────────────────────────────────────────────────
# gemini-aistudio / gemini-vertex 两个 provider_id 都映射到同一个 "gemini" media backend，
# 差异只在 backend_type（aistudio | vertex）—— 由 spec 行声明（每个 provider_id 各一行），不在闭包内 if。
# image/video/audio 对等透传 base_url：aistudio 用用户配置的自定义 endpoint。api_key 与 base_url 无条件
# 透传（含 None），由 backend 内 resolve_gemini_api_key / normalize_base_url 处理 None（None base_url 省略）；
# vertex 分支走 vertexai=True + 服务账号凭证，结构性忽略 base_url（无注入点），透传的 None 被丢弃。

_GEMINI_REGISTRY_BACKEND = "gemini"


def _build_gemini_image(config: LoadedConfig, model_id: str | None, *, backend_type: str) -> Any:
    return _media_create_backend("image")(
        _GEMINI_REGISTRY_BACKEND,
        backend_type=backend_type,
        api_key=config.credentials.get("api_key"),
        base_url=config.credentials.get("base_url"),
        rate_limiter=config.rate_limiter,
        image_model=model_id,
        **({"credentials_path": config.credentials.get("credentials_path")} if backend_type == "vertex" else {}),
    )


def _build_gemini_video(config: LoadedConfig, model_id: str | None, *, backend_type: str) -> Any:
    return _media_create_backend("video")(
        _GEMINI_REGISTRY_BACKEND,
        backend_type=backend_type,
        api_key=config.credentials.get("api_key"),
        base_url=config.credentials.get("base_url"),
        rate_limiter=config.rate_limiter,
        video_model=model_id,
        **({"credentials_path": config.credentials.get("credentials_path")} if backend_type == "vertex" else {}),
    )


def _build_gemini_audio(config: LoadedConfig, model_id: str | None, *, backend_type: str) -> Any:
    return _media_create_backend("audio")(
        _GEMINI_REGISTRY_BACKEND,
        backend_type=backend_type,
        api_key=config.credentials.get("api_key"),
        base_url=config.credentials.get("base_url"),
        model=model_id,
        **({"credentials_path": config.credentials.get("credentials_path")} if backend_type == "vertex" else {}),
    )


_GEMINI_BUILDS = {"image": _build_gemini_image, "video": _build_gemini_video, "audio": _build_gemini_audio}


def _gemini_spec(provider_id: str, media_type: str, *, backend_type: str) -> ProviderSpec:
    build = _GEMINI_BUILDS[media_type]
    return ProviderSpec(
        provider_id=provider_id,
        media_type=media_type,
        registry_backend=_GEMINI_REGISTRY_BACKEND,
        build_backend=partial(build, backend_type=backend_type),
    )


# ── kling special family ──────────────────────────────────────────────────
# Dual-mode auth, one of two (api_key wins if both are filled; the decision lives in lib.backends.kling_shared.kling_auth_mode,
# shared by backend_assembly and providers._test_kling to avoid two separate copies of the dispatch logic drifting):
# - api_key non-empty → auth_mode=bearer (official API Key, works for all models);
# - otherwise → auth_mode=jwt (access_key + secret_key, officially marked as only for 3.0 and earlier models),
#   the two secrets are taken directly by column name and passed through unconditionally including None, handled by the backend's resolve_kling_jwt_credentials.
# base_url fallback: explicitly entered in db_config > registry default_base_url > not passed (KlingBackend has its own
# KLING_BASE_URL fallback). image/video share a single construction helper; only the image side additionally injects the decoupled api_model_name
# (amphibious alias keys like kling-v3-omni-image read the registry api_model_name to send the real API name); the video backend doesn't
# accept an api_model_name parameter, so the injection only applies to the image media_type.
# ── kling 特例族 ──────────────────────────────────────────────────
# 双模式鉴权二选一（同时填写 api_key 优先，判定收口于 lib.backends.kling_shared.kling_auth_mode，
# backend_assembly 与 providers._test_kling 共用同一判定，避免两处各写一份分派逻辑漂移）：
# - api_key 非空 → auth_mode=bearer（官方 API Key，全模型适用）；
# - 否则 → auth_mode=jwt（access_key + secret_key，官方标注仅 3.0 及更早模型），
#   双 secret 按列名直取，无条件透传含 None，由 backend 内 resolve_kling_jwt_credentials 处理。
# base_url 兜底：db_config 显式填写 > registry default_base_url > 不传（KlingBackend 自带
# KLING_BASE_URL 兜底）。image/video 共用单一构造 helper，仅 image 侧额外注入 api_model_name 解耦
# （两栖别名键如 kling-v3-omni-image 读 registry api_model_name 发真实 API 名）；video backend 不
# 接受 api_model_name 参数，故该注入仅对 image media_type 生效。

_KLING_REGISTRY_BACKEND = "kling"


def _build_kling(config: LoadedConfig, model_id: str | None, *, media_type: str) -> Any:
    """Generic kling construction: dispatch auth_mode by credential shape (api_key before the two secrets); the image side adds api_model_name injection.

    kling 通用构造：按凭证形态分派 auth_mode（api_key 优先于双 secret）；image 侧叠加 api_model_name 注入。"""
    if kling_auth_mode(config.credentials) == "bearer":
        kwargs: dict[str, Any] = {
            "auth_mode": "bearer",
            "api_key": config.credentials.get("api_key"),
            "model": model_id,
        }
    else:
        kwargs = {
            "auth_mode": "jwt",
            "access_key": config.credentials.get("access_key"),
            "secret_key": config.credentials.get("secret_key"),
            "model": model_id,
        }
    if media_type == "image":
        # Amphibious alias keys read the model catalog's api_model_name to send the real API name; the video backend doesn't accept that parameter, so only image injects it.
        # 两栖别名键读模型目录的 api_model_name 发真实 API 名；video backend 不接受该参数，仅 image 注入。
        model_info = catalog.get_model(PROVIDER_KLING, model_id) if model_id else None
        if model_info is not None and model_info.api_model_name:
            kwargs["api_model_name"] = model_info.api_model_name
    base_url = _resolve_base_url(config)
    if base_url:
        kwargs["base_url"] = base_url
    return _media_create_backend(media_type)(_KLING_REGISTRY_BACKEND, **kwargs)


def _kling_spec(media_type: str) -> ProviderSpec:
    return ProviderSpec(
        provider_id=_KLING_REGISTRY_BACKEND,
        media_type=media_type,
        registry_backend=_KLING_REGISTRY_BACKEND,
        build_backend=partial(_build_kling, media_type=media_type),
    )


# ── text family ────────────────────────────────────────────────────────
# Text backends are registered in the separate lib.backends.text_backends.registry (not the media registry), and their construction shape differs from media:
# api_key/base_url pass-through rules, OpenAI-compat alias mapping and provider_name billing attribution pass-through all differ, so the text side
# doesn't reuse _build_simple; each shape has its own closure. Three kinds:
#   (1) simple text (ark / ark-agent-plan / grok / agnes) -- model + api_key (passed unconditionally) + base_url (user >
#      registry default, only passed when non-empty, symmetric with the media simple family's base_url precedence). Although agnes uses
#      OpenAI-compatible chat/completions, its auth / base_url normalization lives in the dedicated AgnesTextBackend, so it belongs here rather than (3).
#   (2) gemini text -- aistudio (model + api_key + base_url passed unconditionally, including None/empty string) / vertex
#      (model + backend=vertex + gcs_bucket) split into two rows by provider_id, not by an if inside the closure.
#   (3) OpenAI-compat (openai / dashscope / minimax) -- all map to the "openai" registry backend; openai passes the user's base_url
#      directly (unconditionally, including None), dashscope/minimax derive base_url from the host via their helpers and pass the
#      real provider_name through, so usage accounting and price lookup hit their own (Bailian/MiniMax) CNY rates rather than OpenAI USD.
# ── 文本族 ────────────────────────────────────────────────────────
# 文本 backend 注册在独立的 lib.backends.text_backends.registry（非 media registry），构造形态与媒体有别：
# api_key/base_url 透传规则、OpenAI-compat 别名映射、provider_name 计费归因透传各不相同，故文本侧
# 不复用 _build_simple，各类形态挂专属闭包。三类：
#   ① 简单文本（ark / ark-agent-plan / grok / agnes）—— model + api_key（无条件透传）+ base_url（user >
#      registry default，仅非空才传，对称媒体简单族 base_url 优先级）。agnes 虽走 OpenAI 兼容
#      chat/completions，但鉴权 / base_url 归一化收口在专属 AgnesTextBackend，故归此族而非 ③。
#   ② gemini 文本 —— aistudio（model + api_key + base_url 无条件透传，含 None/空串）/ vertex
#      （model + backend=vertex + gcs_bucket）按 provider_id 分两行，不在闭包内 if。
#   ③ OpenAI-compat（openai / dashscope / minimax）—— 都映射到 "openai" registry backend；openai 直传
#      用户 base_url（无条件，含 None），dashscope/minimax 由各自 helper 从 host 派生 base_url 并透传
#      真实 provider_name，确保 usage 记账与计费查表命中自身（百炼/MiniMax）的 CNY 费率而非 OpenAI USD。


def _build_text_simple(config: LoadedConfig, model_id: str | None, *, registry_backend: str) -> Any:
    """Simple text family: model + api_key (passed unconditionally) + base_url (user > registry default, only passed when non-empty).

    Passing api_key unconditionally (including None) keeps the semantics of the pre-migration imperative branch
    (db_config.get("api_key") written directly into kwargs); base_url is only written when non-empty, symmetric with the media
    simple family -- ark/ark-agent-plan have a registry default fallback, grok has no default and the parameter is omitted
    when the user hasn't configured it.

    简单文本族：model + api_key（无条件透传）+ base_url（user > registry default，仅非空才传）。

    api_key 无条件透传（含 None）保留迁移前命令式分支语义（db_config.get("api_key") 直写 kwargs）；
    base_url 仅非空才写入，对称媒体简单族 —— ark/ark-agent-plan 有 registry default 回落，grok 无 default
    且用户未配时省略该参数。
    """
    kwargs: dict[str, Any] = {"model": model_id, "api_key": config.credentials.get("api_key")}
    base_url = _resolve_base_url(config)
    if base_url:
        kwargs["base_url"] = base_url
    return _media_create_backend("text")(registry_backend, **kwargs)


def _text_simple_spec(provider_id: str) -> ProviderSpec:
    """Register one simple text spec: registry_backend is the provider_id itself (no alias mapping on the text side).

    登记一条简单文本 spec：registry_backend 即 provider_id 自身（文本侧无别名映射）。"""
    return ProviderSpec(
        provider_id=provider_id,
        media_type="text",
        registry_backend=provider_id,
        build_backend=partial(_build_text_simple, registry_backend=provider_id),
    )


_TEXT_GEMINI_REGISTRY_BACKEND = "gemini"


def _build_text_gemini_aistudio(config: LoadedConfig, model_id: str | None) -> Any:
    # aistudio lets the user enter a custom endpoint and has no registry default: base_url passes the user value through unconditionally (including None/empty string),
    # handled inside GeminiTextBackend. Text gemini doesn't accept rate_limiter (keeping the pre-migration asymmetry).
    # aistudio 允许用户填自定义 endpoint，无 registry default：base_url 无条件透传用户值（含 None/空串），
    # 由 GeminiTextBackend 内部处理。文本 gemini 不接受 rate_limiter（保留迁移前非对称）。
    return _media_create_backend("text")(
        _TEXT_GEMINI_REGISTRY_BACKEND,
        model=model_id,
        api_key=config.credentials.get("api_key"),
        base_url=config.credentials.get("base_url"),
    )


def _build_text_gemini_vertex(config: LoadedConfig, model_id: str | None) -> Any:
    # vertex uses a service account credential file + gcs_bucket and doesn't pass api_key/base_url (keeping the pre-migration imperative branch).
    # vertex 走服务账号凭证文件 + gcs_bucket，不传 api_key/base_url（保留迁移前命令式分支）。
    return _media_create_backend("text")(
        _TEXT_GEMINI_REGISTRY_BACKEND,
        model=model_id,
        backend="vertex",
        gcs_bucket=config.credentials.get("gcs_bucket"),
        credentials_path=config.credentials.get("credentials_path"),
    )


def _text_gemini_spec(provider_id: str, *, build: Callable[[LoadedConfig, str | None], Any]) -> ProviderSpec:
    return ProviderSpec(
        provider_id=provider_id,
        media_type="text",
        registry_backend=_TEXT_GEMINI_REGISTRY_BACKEND,
        build_backend=build,
    )


_TEXT_OPENAI_COMPAT_REGISTRY_BACKEND = "openai"
# Built-in openai text uses Responses: models only available through Responses, such as o3-pro and gpt-5.5-pro, can be called too.
# 内置 openai 的文本走 Responses：o3-pro、gpt-5.5-pro 等只开放 Responses 的模型也能调用。
_TEXT_OPENAI_RESPONSES_REGISTRY_BACKEND = "openai-responses"


def _dashscope_text_base_url(configured: str | None) -> str:
    # Runtime import: so lightweight scenarios (CLI / migrations) don't pull in dashscope_shared just by importing this table.
    # 运行时 import：免轻量场景（CLI / 迁移）因 import 本表被动拉起 dashscope_shared。
    from lib.backends.dashscope_shared import dashscope_text_base_url

    return dashscope_text_base_url(configured)


def _minimax_text_base_url(configured: str | None) -> str:
    from lib.backends.minimax_shared import minimax_text_base_url

    return minimax_text_base_url(configured)


def _build_text_openai_compat(
    config: LoadedConfig,
    model_id: str | None,
    *,
    derive_base_url: Callable[[str | None], str] | None,
    provider_name: str | None,
    registry_backend: str = _TEXT_OPENAI_COMPAT_REGISTRY_BACKEND,
) -> Any:
    """OpenAI-protocol text construction: dashscope / minimax use Chat Completions (OpenAITextBackend),
    openai uses Responses (OpenAIResponsesTextBackend); both have the same constructor arguments, distinguished by registry_backend.

    derive_base_url=None (openai): pass the user's base_url directly (unconditionally, including None; the backend decides the endpoint).
    derive_base_url non-empty (dashscope/minimax): the helper derives the OpenAI-compatible base from the user's host and
    provider_name is passed through to the backend, so usage accounting and price lookup hit their own CNY rates rather than OpenAI USD.

    OpenAI 协议文本构造：dashscope / minimax 走 Chat Completions（OpenAITextBackend），
    openai 走 Responses（OpenAIResponsesTextBackend），二者构造参数同形，由 registry_backend 区分。

    derive_base_url=None（openai）：直传用户 base_url（无条件，含 None，由 backend 决定 endpoint）。
    derive_base_url 非空（dashscope/minimax）：由 helper 从用户 host 派生 OpenAI 兼容 base，并透传
    provider_name 给 backend，确保 usage 记账与计费查表命中自身 CNY 费率而非 OpenAI USD。
    """
    user_base_url = config.credentials.get("base_url")
    kwargs: dict[str, Any] = {
        "model": model_id,
        "api_key": config.credentials.get("api_key"),
        "base_url": derive_base_url(user_base_url) if derive_base_url else user_base_url,
    }
    if provider_name:
        kwargs["provider_name"] = provider_name
    return _media_create_backend("text")(registry_backend, **kwargs)


def _text_openai_compat_spec(
    provider_id: str,
    *,
    derive_base_url: Callable[[str | None], str] | None,
    provider_name: str | None,
    registry_backend: str = _TEXT_OPENAI_COMPAT_REGISTRY_BACKEND,
) -> ProviderSpec:
    return ProviderSpec(
        provider_id=provider_id,
        media_type="text",
        registry_backend=registry_backend,
        build_backend=partial(
            _build_text_openai_compat,
            derive_base_url=derive_base_url,
            provider_name=provider_name,
            registry_backend=registry_backend,
        ),
    )


# ── PROVIDER_SPEC_REGISTRY ──────────────────────────────────
# Key = (provider_id, media_type). Simple family = built-in providers whose media side only needs api_key + model + base_url,
# sharing the _build_simple closure. The "simple family" is defined by construction shape (not a whitelist of provider names) and includes ark/ark-agent-plan/
# grok/openai/vidu and dashscope/minimax (the latter two use native simple construction on the media side; for their text-side OpenAI-compat shape see the
# text family below). ark-agent-plan's media side reuses the Ark image/video backends (registered under the same name), simple-shaped like ark.
# Special families = gemini (backend_type dual mode, image/video/audio pass base_url through equally) and kling (JWT dual secrets +
# decoupled api_model_name), each with its own build closure; gemini's two provider_ids each register one row per backend_type (bare
# "gemini" is a dead path -- the resolver only produces suffixed ids, so per fail-loud no fallback row is registered). The text family (media_type=text) is
# registered in three kinds by construction shape (simple text / gemini text / OpenAI-compat text, see each closure), with dashscope/minimax text
# mapping to the "openai" registry backend and the alias mapping merged into the spec.registry_backend field. Each pair is registered explicitly as one row, fail-loud
# (unregistered provider × media raises ValueError rather than "absent means default" producing a silently wrong backend). Only pairs that actually have a
# registered backend today are registered: the image/video simple family covers all seven, audio is dashscope, minimax and the two gemini ids, text is eight (six providers, two gemini ids).
# ── PROVIDER_SPEC_REGISTRY 注册表 ──────────────────────────────────
# 键 = (provider_id, media_type)。简单族 = 媒体侧只需 api_key + model + base_url 的内置 provider，
# 共享 _build_simple 闭包。「简单族」按构造形态界定（不是 provider 名白名单），含 ark/ark-agent-plan/
# grok/openai/vidu 与 dashscope/minimax（后两者媒体侧走原生简单构造；其文本侧 OpenAI-compat 形态见下方
# 文本族）。ark-agent-plan 媒体侧复用 Ark image/video backend（registry 同名注册），与 ark 同为简单形态。
# 特例族 = gemini（backend_type 双模式，image/video/audio 对等透传 base_url）与 kling（JWT 双 secret +
# api_model_name 解耦），各挂专属 build 闭包；gemini 的两个 provider_id 各按 backend_type 登记一行（裸
# "gemini" 是死路径——resolver 只产出带后缀 id，按 fail-loud 不登记兜底行）。文本族（media_type=text）按
# 构造形态分三类登记（简单文本 / gemini 文本 / OpenAI-compat 文本，见各闭包），其中 dashscope/minimax 文本
# 映射到 "openai" registry backend，别名映射并入 spec.registry_backend 字段。每对显式登记一行，fail-loud
# （未登记的 provider × media 抛 ValueError，不「缺席即默认」造静默错误 backend）。只登记今天确有注册 backend
# 的对：image/video 简单族七家齐全，audio 为 dashscope、minimax 与 gemini 两个 id，text 八家（六 provider，gemini 两 id）。

_SIMPLE_IMAGE_VIDEO_PROVIDERS = ("ark", "ark-agent-plan", "grok", "openai", "vidu", "dashscope", "minimax")
_SIMPLE_MEDIA_PAIRS: list[tuple[str, str]] = [
    *((p, "image") for p in _SIMPLE_IMAGE_VIDEO_PROVIDERS),
    *((p, "video") for p in _SIMPLE_IMAGE_VIDEO_PROVIDERS if p != "dashscope"),
    ("dashscope", "audio"),
    ("minimax", "audio"),
]

# The two gemini provider_ids → backend_type, one row each × image/video/audio.
# gemini 两个 provider_id → backend_type，每个 × image/video/audio 登记一行。
_GEMINI_BACKEND_TYPES: dict[str, str] = {"gemini-aistudio": "aistudio", "gemini-vertex": "vertex"}

PROVIDER_SPEC_REGISTRY: dict[tuple[str, str], ProviderSpec] = {
    (provider_id, media_type): _simple_spec(provider_id, media_type) for provider_id, media_type in _SIMPLE_MEDIA_PAIRS
}
PROVIDER_SPEC_REGISTRY.update(
    {
        (provider_id, media_type): _gemini_spec(provider_id, media_type, backend_type=backend_type)
        for provider_id, backend_type in _GEMINI_BACKEND_TYPES.items()
        for media_type in ("image", "video", "audio")
    }
)
PROVIDER_SPEC_REGISTRY.update(
    {(_KLING_REGISTRY_BACKEND, media_type): _kling_spec(media_type) for media_type in ("image", "video")}
)
# dashscope video has one more key than the simple family, wan3_base_url: Wanxiang 3.0 uses a separate maas domain (including region and
# workspace, which can't be derived); when not configured the backend falls back to the generic base_url. The image/audio lanes don't consume it
# and still use the simple family.
# dashscope video 比简单族多一个 wan3_base_url：万相 3.0 走独立 maas 域名（含地域与
# workspace，推不出），未配置时 backend 自行回落通用 base_url。image/audio 两条 lane 不消费它，
# 仍走简单族。
PROVIDER_SPEC_REGISTRY[("dashscope", "video")] = _simple_spec("dashscope", "video", extra_keys=("wan3_base_url",))
PROVIDER_SPEC_REGISTRY[("minimax", "video")] = ProviderSpec(
    provider_id="minimax",
    media_type="video",
    registry_backend="declarative",
    build_backend=_build_minimax_declarative_video,
)

# agnes simple family image + video registered explicitly (the text family is registered below in the text section with _TEXT_SIMPLE_PROVIDERS);
# like kling it's registered separately and explicitly, not merged into the _SIMPLE_IMAGE_VIDEO_PROVIDERS tuple.
# agnes 简单族 image + video 显式登记（文本族在下方文本区随 _TEXT_SIMPLE_PROVIDERS 登记）；
# 与 kling 同走独立显式登记，不并入 _SIMPLE_IMAGE_VIDEO_PROVIDERS 元组。
PROVIDER_SPEC_REGISTRY[("agnes", "image")] = _simple_spec("agnes", "image")
PROVIDER_SPEC_REGISTRY[("agnes", "video")] = _simple_spec("agnes", "video")

# ── text family registration ────────────────────────────────────────────────────
# Four simple text providers (registry_backend = the provider_id itself); gemini's two provider_ids split into two rows by backend
# (aistudio/vertex each with its own closure, registry_backend both "gemini"); the three OpenAI-compat providers share one construction closure:
# dashscope/minimax map to "openai" (Chat Completions), deriving base_url via helper + passing provider_name through for billing attribution;
# openai maps to "openai-responses" (Responses), passing the user's base_url directly.
# Although agnes uses OpenAI-compatible chat/completions, its auth / base_url normalization lives in the dedicated AgnesTextBackend (reusing
# agnes_shared), so it belongs to the simple text family (registry_backend = "agnes") and isn't merged into the OpenAI-compat alias mapping.
# ── 文本族注册 ────────────────────────────────────────────────────
# 简单文本四家（registry_backend = provider_id 自身）；gemini 两个 provider_id 按 backend 分两行
# （aistudio/vertex 各自闭包，registry_backend 同为 "gemini"）；OpenAI-compat 三家共用一个构造闭包：
# dashscope/minimax 映射到 "openai"（Chat Completions），经 helper 派生 base_url + 透传 provider_name 计费归因；
# openai 映射到 "openai-responses"（Responses），直传用户 base_url。
# agnes 虽走 OpenAI 兼容 chat/completions，但鉴权 / base_url 归一化收口在专属 AgnesTextBackend（复用
# agnes_shared），故归简单文本族（registry_backend = "agnes"），不并入 OpenAI-compat 别名映射。
_TEXT_SIMPLE_PROVIDERS = ("ark", "ark-agent-plan", "grok", "agnes")
PROVIDER_SPEC_REGISTRY.update({(p, "text"): _text_simple_spec(p) for p in _TEXT_SIMPLE_PROVIDERS})
PROVIDER_SPEC_REGISTRY.update(
    {
        ("gemini-aistudio", "text"): _text_gemini_spec("gemini-aistudio", build=_build_text_gemini_aistudio),
        ("gemini-vertex", "text"): _text_gemini_spec("gemini-vertex", build=_build_text_gemini_vertex),
        ("openai", "text"): _text_openai_compat_spec(
            "openai", derive_base_url=None, provider_name=None, registry_backend=_TEXT_OPENAI_RESPONSES_REGISTRY_BACKEND
        ),
        ("dashscope", "text"): _text_openai_compat_spec(
            "dashscope", derive_base_url=_dashscope_text_base_url, provider_name="dashscope"
        ),
        ("minimax", "text"): _text_openai_compat_spec(
            "minimax", derive_base_url=_minimax_text_base_url, provider_name="minimax"
        ),
    }
)


_VALID_MEDIA_TYPES = frozenset({"image", "video", "audio", "text"})


def _validate_provider_specs() -> None:
    """Validate the built-in table's own invariants at import time; misconfiguration fails fast (mirrors endpoints._validate_video_caps_declarations).

    Only self-consistency checks of the table that don't require importing backends: build is callable, dict keys match the
    spec fields, media_type is valid. "All registry names are in the corresponding backend registry" can only be asserted by
    importing all of lib.{image,video,audio,text}_backends, so to keep lightweight scenarios (CLI / migrations) from pulling in
    every backend just by importing this seam, it lives in unit tests rather than at import time.

    import 期校验内置表自身不变式，misconfig fail-fast（镜像 endpoints._validate_video_caps_declarations）。

    只做不需 import 后端的内表自洽检查：build 可调用、字典键与 spec 字段一致、media_type 合法。
    「registry 名都在对应后端 registry 里」需 import 全部 lib.{image,video,audio,text}_backends 才能断言，
    为免轻量场景（CLI / 迁移）因 import 本缝而被动拉起全部后端，归入单测，不进 import 期。
    """
    for key, spec in PROVIDER_SPEC_REGISTRY.items():
        if not callable(spec.build_backend):
            raise ValueError(f"ProviderSpec {key!r} declares non-callable build_backend: {spec.build_backend!r}")
        if (spec.provider_id, spec.media_type) != key:
            raise ValueError(
                f"PROVIDER_SPEC_REGISTRY key {key!r} does not match spec fields "
                f"(provider_id={spec.provider_id!r}, media_type={spec.media_type!r})"
            )
        if spec.media_type not in _VALID_MEDIA_TYPES:
            raise ValueError(f"ProviderSpec {key!r} declares unknown media_type: {spec.media_type!r}")


_validate_provider_specs()


def get_provider_spec(provider_id: str, media_type: str) -> ProviderSpec:
    spec = PROVIDER_SPEC_REGISTRY.get((provider_id, media_type))
    if spec is None:
        raise ValueError(f"no builtin ProviderSpec for provider={provider_id!r} media={media_type!r}")
    return spec


def builtin_video_capabilities_for_model(provider_id: str, model_id: str) -> VideoCapabilities:
    """Read built-in video capabilities: the backend's declaration, with backend-fact capability overrides layered on for user-added models (docs/adr/0104).

    Overrides replace whole dimensions and have top priority; when the two audio dimensions together are meaningless, it's
    treated as not supporting reference audio, same rule as custom providers.

    读取内置视频能力：调用通道的声明，用户添加的模型再叠加通道事实的能力覆盖（docs/adr/0104）。

    覆盖整维替换、优先级最高；音频两维合起来无意义时按不支持参考音频处理，与自定义供应商同一口径。
    """
    return apply_user_added_video_overrides(
        declared_builtin_video_capabilities(provider_id, model_id), provider_id, model_id
    )


def apply_user_added_video_overrides(caps: VideoCapabilities, provider_id: str, model_id: str) -> VideoCapabilities:
    """Layer backend-fact overrides of user-added models under built-in providers on top of the capabilities given by the backend (docs/adr/0104).

    The base is given by the caller: the planning layer passes the declaration without request context, the execution gate
    passes the tier-resolved capabilities; both are layered the same way.

    在调用通道给出的能力上叠加内置供应商下用户添加的模型的通道事实覆盖（docs/adr/0104）。

    基底由调用方给定：规划层传无请求上下文的声明，执行闸门传按档位解析的能力，两边叠加口径相同。
    """
    overrides = catalog.user_added_capability_overrides(provider_id, model_id)
    if not overrides:
        return caps
    from lib.custom_provider.capabilities import (
        CHANNEL_FACT_OVERRIDE_KEYS,
        enforce_audio_capability_invariant,
        merge_overrides,
    )

    channel = {key: value for key, value in overrides.items() if key in CHANNEL_FACT_OVERRIDE_KEYS}
    return enforce_audio_capability_invariant(merge_overrides(caps, channel), endpoint=provider_id, model_id=model_id)


def declared_builtin_video_capabilities(provider_id: str, model_id: str) -> VideoCapabilities:
    """Capabilities the built-in video backend declares for the model, without the user's capability overrides; declarative providers read from the versioned definition, others use the backend registry.

    内置视频调用通道对模型声明的能力，不含用户的能力覆盖；声明式供应商从随版定义读，其余沿用 backend registry。"""
    spec = get_provider_spec(provider_id, "video")
    if spec.registry_backend == "declarative":
        from lib.custom_provider.builtin_definitions import declarative_video_capabilities
        from lib.custom_provider.endpoints import get_endpoint_spec

        definition = get_endpoint_spec(_minimax_video_endpoint(model_id)).definition
        if definition is None:
            raise ValueError(f"builtin declarative definition missing for {provider_id}/{model_id}")
        return declarative_video_capabilities(definition)
    from lib.backends.video_backends.registry import video_capabilities_for_model

    return video_capabilities_for_model(spec.registry_backend, model_id)


def builtin_video_metered_usage(
    provider_id: str,
    model_id: str,
    request: VideoGenerationRequest,
    result: VideoGenerationResult | None = None,
) -> Usage:
    """Metering the built-in provider's video backend reports for a request, without constructing an instance; declarative providers read from the versioned definition.

    ``result`` being ``None`` means a pre-submit estimate; when given, it matches the metering reported by the backend
    instance at settlement. When the request carries no capabilities, the capabilities with user overrides layered on are
    filled in, from the same source as those carried by the request at execution, so relaxed dimensions are metered per the
    request actually sent.

    内置供应商的视频调用通道对一个请求上报的计量，不构造实例；声明式供应商从随版定义读。

    ``result`` 为 ``None`` 时是提交前预估；给出时与结算时调用通道实例上报的计量相同。请求没带能力时补上
    叠加用户覆盖后的能力，与执行时请求携带的那份同源，放宽的维度按实际发出的请求计量。
    """
    spec = get_provider_spec(provider_id, "video")
    if spec.registry_backend == "declarative":
        from lib.custom_provider.declarative_backend import declarative_video_usage
        from lib.custom_provider.endpoints import get_endpoint_spec

        definition = get_endpoint_spec(_minimax_video_endpoint(model_id)).definition
        if definition is None:
            raise ValueError(f"builtin declarative definition missing for {provider_id}/{model_id}")
        return declarative_video_usage(definition, request, result)
    from lib.backends.video_backends.registry import metered_usage_for_model

    if request.video_capabilities is None:
        request = replace(request, video_capabilities=builtin_video_capabilities_for_model(provider_id, model_id))
    return metered_usage_for_model(spec.registry_backend, model_id, request, result)


def builtin_effective_generate_audio_for_model(provider_id: str, model_id: str) -> bool:
    spec = get_provider_spec(provider_id, "video")
    if spec.registry_backend == "declarative":
        # Pricing shares its source with the capability declaration: when the definition declares the video never has an audio track, the default execution tier can't produce voice.
        # 计价口径与能力声明同源：定义声明成片恒无音轨时，默认执行档不可能产出人声。
        from arcreel_market_core.video_backend_contract import VideoAudioMode

        return builtin_video_capabilities_for_model(provider_id, model_id).audio_track is not VideoAudioMode.ALWAYS_OFF
    from lib.backends.video_backends.registry import effective_generate_audio_for_model

    return effective_generate_audio_for_model(spec.registry_backend, model_id)
