"""assemble_backend — the unified construction entry "provider config + model → backend".

A single is_custom_provider branch routes to the built-in / custom families (one door, two symmetric rooms behind it,
see ADR 0039). Construction is split into two stages: async loading (query DB/config, producing a LoadedConfig envelope)
/ sync pure closure construction. The seam is stateless and pure construction; backend instance caching stays in the
caller's orchestration layer and doesn't enter the seam.

assemble_backend — 「provider config + model → backend」统一构造入口。

按 is_custom_provider 单分支分流到内置 / 自定义两族（一道门、门后两个对称房间，见 ADR 0039）。
构造拆两段：async 装载（查 DB/config，产出 LoadedConfig 信封）/ sync 纯闭包构造。缝无状态、纯构造，
backend 实例缓存留在调用方编排层、不进缝。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.backends.backend_assembly.loaded_config import LoadedConfig
from lib.backends.backend_assembly.specs import get_provider_spec
from lib.catalog import query as catalog
from lib.catalog.errors import ModelNotInCatalogError
from lib.config.registry import PROVIDER_REGISTRY
from lib.custom_provider import is_custom_provider

if TYPE_CHECKING:
    from lib.config.resolver import ConfigResolver


async def _load_builtin_config(resolver: ConfigResolver, provider_id: str, rate_limiter: Any | None) -> LoadedConfig:
    """Built-in async loading stage: query DB/config and produce the LoadedConfig envelope.

    The credential overlay comes from db_config (resolver.provider_config); registry meta provides the sources of
    default_base_url / api_model_name; rate_limiter is injected by the caller (shared instance). This stage is the only
    await/DB touchpoint; afterwards the sync construction closure only reads the envelope.

    内置侧 async 装载段：查 DB/config 产出 LoadedConfig 信封。

    凭证 overlay 来自 db_config（resolver.provider_config）；registry meta 提供 default_base_url /
    api_model_name 来源；rate_limiter 由调用方注入（共享实例）。这一段是唯一的 await/DB 触点，
    之后 sync 构造闭包只读信封。
    """
    db_config = await resolver.provider_config(provider_id)
    return LoadedConfig(
        credentials=dict(db_config),
        provider_meta=PROVIDER_REGISTRY.get(provider_id),
        rate_limiter=rate_limiter,
    )


async def assemble_backend(
    *,
    provider_id: str,
    media_type: str,
    model_id: str | None,
    resolver: ConfigResolver,
    rate_limiter: Any | None = None,
    generation_type: str | None = None,
) -> Any:
    """Unified construction entry. Routes by whether provider_id is custom; unregistered built-in provider × media fails loud.

    ``generation_type`` is the task type bucket of the call site, only consumed on the custom side (default models are
    slotted per bucket, see ``lib.custom_provider.loader.load_custom_backend``); on the built-in side the model is decided by
    the registry, without buckets.

    统一构造入口。按 provider_id 是否自定义分流；未登记的内置 provider × media fail-loud。

    ``generation_type`` 是调用点所属的任务类型桶，只在自定义侧消费（默认模型按桶分槽，见
    ``lib.custom_provider.loader.load_custom_backend``）；内置侧的 model 由 registry 定，不分桶。
    """
    if is_custom_provider(provider_id):
        from lib.custom_provider.loader import load_custom_backend

        # Borrow the resolver's session (reusing its transaction context when bound): custom loading and simple-family loading share the same
        # session_factory, so when the caller constructs inside resolver.session() the same connection is reused instead of opening another factory.
        # 借 resolver 的 session（bound 时复用其事务上下文）：自定义装载与简单族装载共用同一
        # session_factory，调用方在 resolver.session() 内构造时复用同一连接，避免另开 factory。
        async with resolver._open_session() as (session, _):
            return await load_custom_backend(
                session=session,
                provider_id=provider_id,
                model_id=model_id,
                media_type=media_type,
                generation_type=generation_type,
            )
    spec = get_provider_spec(provider_id, media_type)  # unregistered → ValueError (fail-loud) / 未登记 → ValueError（fail-loud）
    # Default models only come from the model catalog: when the caller gives no model, take the provider's default model for this media type.
    # 默认模型只来自模型目录：调用方没给 model 时取该供应商在这个媒体类型下的默认模型。
    if model_id is None:
        model_id = catalog.default_model(provider_id, media_type)
    if model_id is not None:
        _ensure_listed(provider_id, model_id, media_type)
    config = await _load_builtin_config(resolver, provider_id, rate_limiter)
    # The construction closure may read a credential file (Vertex service account JSON), so run it in a thread without blocking the event loop. The worker
    # thread has no event loop, so backend construction can't create SDK clients that require one (for how xai_sdk.AsyncClient is handled see grok_shared.LazyGrokClient).
    # 构造闭包可能读凭证文件（Vertex 服务账号 JSON），放线程里跑，不阻塞事件循环。工作线程里没有事件循环，
    # backend 构造时不能创建要求事件循环的 SDK 客户端（xai_sdk.AsyncClient 的做法见 grok_shared.LazyGrokClient）。
    return await asyncio.to_thread(spec.build_backend, config, model_id)


def _ensure_listed(provider_id: str, model_id: str, media_type: str) -> None:
    """A built-in provider's model must be in that provider's model list for the media type; otherwise raise and let the user pick again, without falling back to another model.

    内置供应商的模型必须在该供应商该媒体类型的模型清单中；不在时报错，由用户重新选择，不回落到别的模型。"""
    if catalog.get_model(provider_id, model_id, media_type=media_type) is None:
        raise ModelNotInCatalogError(provider_id=provider_id, model_id=model_id, media_type=media_type)


@dataclass(frozen=True)
class OutputLimitFacts:
    """Facts about the model needed for the request output limit and the truncation fallback. / 模型在请求输出上限与截断出路上需要的事实。"""

    #: The model's registered max output length (in tokens); None when unregistered. / 模型登记的最大输出长度（单位 token）；未登记时 None。
    registered_max_output_tokens: int | None
    #: The model is provided by a custom provider. / 模型由自定义供应商提供。
    custom_model: bool


async def output_limit_facts(*, provider_id: str, model_id: str, resolver: ConfigResolver) -> OutputLimitFacts:
    """Read the model's registered max output length.

    Routed by the same door as :func:`assemble_backend`: built-in models read the registry, custom provider models read the model entry.

    读取模型登记的最大输出长度。

    与 :func:`assemble_backend` 同一道门分流：内置模型读注册表，自定义供应商的模型读模型条目。
    """
    if is_custom_provider(provider_id):
        from lib.custom_provider.loader import custom_model_max_output_tokens

        async with resolver._open_session() as (session, _):
            registered = await custom_model_max_output_tokens(
                session=session, provider_id=provider_id, model_id=model_id
            )
        return OutputLimitFacts(registered_max_output_tokens=registered, custom_model=True)
    info = catalog.get_model(provider_id, model_id)
    return OutputLimitFacts(
        registered_max_output_tokens=info.max_output_tokens if info is not None else None, custom_model=False
    )
