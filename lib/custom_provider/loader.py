"""DB loading of custom provider backends.

Look up the provider, validate the requested model (exists / enabled / media_type derived from endpoint matches); if invalid,
fall back to the single default enabled model of that media_type × task type bucket, and finally delegate to the existing
create_custom_backend (ENDPOINT_REGISTRY unchanged).
Loading lives in lib so the media path and the text factory share one custom resolution, and lib does not depend back on server.

自定义供应商 backend 的 DB 装载。

查 provider、校验请求的 model（存在 / 启用 / endpoint 推算 media_type 相符），失效则回退该
media_type × 任务类型桶上唯一的默认启用 model，最后委托现成 create_custom_backend
（ENDPOINT_REGISTRY 不改）。
装载落在 lib 让媒体路径与文本工厂共用一份自定义解析，且 lib 不反向依赖 server。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from lib.custom_provider import parse_provider_id
from lib.custom_provider.backends import (
    CustomAudioBackend,
    CustomImageBackend,
    CustomTextBackend,
    CustomVideoBackend,
)
from lib.custom_provider.default_models import resolve_default_model
from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
from lib.custom_provider.factory import create_custom_backend
from lib.custom_provider.model_facts import text_model_facts
from lib.db.models.custom_provider import CustomProviderModel
from lib.db.repositories.custom_provider_repo import CustomProviderRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def custom_model_max_output_tokens(*, session: AsyncSession, provider_id: str, model_id: str) -> int | None:
    """Max output length of a custom provider model: capability override > matching entry; None when neither exists.

    自定义供应商模型的最大输出长度：能力覆盖 > 对应条目；都没有时 None。"""
    row = await CustomProviderRepository(session).get_model_by_ids(parse_provider_id(provider_id), model_id)
    if row is None:
        return None
    return text_model_facts(model_id, row.capability_overrides, entry_binding=row.entry_binding).max_output_tokens


async def load_custom_backend(
    *,
    session: AsyncSession,
    provider_id: str,
    model_id: str | None,
    media_type: str,
    generation_type: str | None = None,
) -> CustomTextBackend | CustomImageBackend | CustomVideoBackend | CustomAudioBackend:
    """Load and build a custom provider backend.

    media_type is used to check that the requested model's endpoint matches, and to group when falling back to the default; actual
    dispatch follows model.endpoint. Requested model missing / disabled / wrong media type → treated as invalid and falls back to
    that media_type's default enabled model.

    ``generation_type`` is the task type bucket of the call site and only affects the fallback: default models are slotted per
    bucket (image has one default each for t2i and i2i), and only with a bucket can the single row be picked; see
    ``lib.custom_provider.default_models``. Call sites that commit to no bucket pass None, no filtering.

    Raises:
        ValueError: provider does not exist, or the default enabled model for that media_type × bucket is not unique (zero or several).

    装载并构造自定义供应商 backend。

    media_type 用于校验请求 model 的 endpoint 是否相符、以及回退默认时分组；实际派发以 model.endpoint
    为准。请求 model 不存在 / 已禁用 / 媒体类型不符 → 视为失效并回退该 media_type 的默认启用 model。

    ``generation_type`` 是调用点所属的任务类型桶，只作用于回退：默认模型按桶分槽（image 的 t2i
    与 i2i 各有一个默认），带桶才挑得出唯一那一行，见 ``lib.custom_provider.default_models``。
    调用点不承诺桶时传 None、不过滤。

    Raises:
        ValueError: provider 不存在，或该 media_type × 桶上的默认启用 model 不唯一（零个或多个）。
    """
    repo = CustomProviderRepository(session)
    from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository

    endpoint_repo = CustomEndpointRepository(session)
    db_id = parse_provider_id(provider_id)
    provider = await repo.get_provider(db_id)
    if provider is None:
        raise ValueError(f"Custom provider {provider_id} does not exist")

    model = None
    if model_id:
        stmt = select(CustomProviderModel).where(
            CustomProviderModel.provider_id == db_id,
            CustomProviderModel.model_id == model_id,
            CustomProviderModel.is_enabled,
        )
        result = await session.execute(stmt)
        candidate = result.scalar_one_or_none()
        if candidate:
            try:
                candidate_spec = await resolve_endpoint_spec(candidate.endpoint, endpoint_repo.get)
            except ValueError:
                candidate_spec = None
            if candidate_spec is not None and candidate_spec.media_type == media_type:
                model = candidate
        if model is None:
            logger.warning(
                "自定义模型 %s/%s 已不存在 / 已禁用 / 媒体类型不符（期望 %s），回退到默认模型",
                provider_id,
                model_id,
                media_type,
            )
            model_id = None

    if model is None:
        default_model = await resolve_default_model(
            session,
            provider_id=provider_id,
            db_id=db_id,
            media_type=media_type,
            generation_type=generation_type,
        )
        model = default_model
        model_id = default_model.model_id

    if model_id is None:
        raise ValueError(f"Custom provider {provider_id} is still missing model_id after resolution")
    spec = await resolve_endpoint_spec(model.endpoint, endpoint_repo.get)
    return create_custom_backend(
        provider=provider,
        model_id=model_id,
        endpoint=model.endpoint,
        capability_overrides=model.capability_overrides,
        path_overrides=model.path_overrides,
        endpoint_spec=spec,
        entry_binding=model.entry_binding,
    )
