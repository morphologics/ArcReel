"""Per-bucket resolution of custom provider default models: default fallback and the resolve-time capability gate share one filter.

Default models are slotted per task type bucket: the save-time uniqueness check (``_check_unique_defaults`` in
``server/routers/custom_providers.py``) judges mutual exclusion by image capability set, and setting one default each for
t2i and i2i is allowed, so "provider + media_type" only locates a group of candidate rows, and picking one also needs the
bucket. Bucket membership is always judged via ``lib.backends.generation_type_buckets.custom_model_buckets``; no separate
capability reading is written here.

When the bucket is unknown (video side resolves without a bucket, text / audio have no buckets) nothing is filtered: defaults
of these media_types are at most one per media_type already at save time, so candidates never exceed one row.

自定义供应商默认模型的按桶解析 —— 默认回退与解析期能力闸共用一份过滤。

默认模型是按任务类型桶分槽的：保存期的唯一性检查（``server/routers/custom_providers.py``
``_check_unique_defaults``）按 image 能力集判互斥，t2i 与 i2i 各设一个默认是放行的，故
「provider + media_type」只定位到一组候选行，挑哪一行须再带上桶。桶归属一律经
``lib.backends.generation_type_buckets.custom_model_buckets`` 判，不在此另写一份能力解读。

桶未知（视频侧不定桶解析、text / audio 无桶）时不过滤：这些 media_type 的默认在保存期就是
同一 media_type 至多一个，候选恒不超过一行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.backends.generation_type_buckets import (
    BUCKETS_BY_MEDIA_TYPE,
    GenerationTypeBucket,
    custom_model_buckets,
)
from lib.custom_provider.endpoint_resolution import resolve_endpoint_spec
from lib.db.repositories.custom_endpoint_repo import CustomEndpointRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from lib.db.models.custom_provider import CustomProviderModel


async def model_buckets(session: AsyncSession, model: CustomProviderModel) -> frozenset[GenerationTypeBucket]:
    """Task type buckets a model row has; empty set when the endpoint is removed / cannot be resolved (better no candidate than a wrong one).

    模型行具备的任务类型桶；端点已下线 / 解析不出时空集（候选宁缺勿滥）。"""
    try:
        endpoint_spec = await resolve_endpoint_spec(model.endpoint, CustomEndpointRepository(session).get)
    except ValueError:
        endpoint_spec = None
    return custom_model_buckets(
        endpoint=model.endpoint,
        model_id=model.model_id,
        capability_overrides=model.capability_overrides,
        endpoint_spec=endpoint_spec,
        entry_binding=model.entry_binding,
    )


#: Bucket → its media type, obtained by inverting ``BUCKETS_BY_MEDIA_TYPE``, not listed separately.
#: 桶 → 所属媒体类型，由 ``BUCKETS_BY_MEDIA_TYPE`` 反转得到，不另列一份。
_MEDIA_TYPE_BY_BUCKET: dict[str, str] = {
    bucket: media_type for media_type, buckets in BUCKETS_BY_MEDIA_TYPE.items() for bucket in buckets
}


async def lacks_bucket(session: AsyncSession, model: CustomProviderModel, generation_type: str) -> bool:
    """The model row does produce this media but lacks this bucket: the criterion of the resolve-time capability gate.

    Returns False when the endpoint cannot be resolved or has been changed to another media type: that is a broken reference,
    handled by the default fallback, not a missing capability.

    模型行确实产该媒体、但不具备该桶 —— 解析期能力闸的判据。

    端点解析不出、或端点已改成别的媒体类型时返回 False：那是引用失效，由默认回退接手，不是能力缺失。
    """
    buckets = await model_buckets(session, model)
    same_media = BUCKETS_BY_MEDIA_TYPE.get(_MEDIA_TYPE_BY_BUCKET.get(generation_type, ""), ())
    return bool(buckets & frozenset(same_media)) and generation_type not in buckets


async def filter_by_bucket(
    session: AsyncSession,
    models: list[CustomProviderModel],
    generation_type: str | None,
) -> list[CustomProviderModel]:
    """Keep model rows that have this bucket, preserving input order; returned unchanged when ``generation_type`` is None.

    保留具备该桶的模型行，保持入参顺序；``generation_type`` 为 None 时原样返回。"""
    if generation_type is None:
        return models
    return [model for model in models if generation_type in await model_buckets(session, model)]


async def resolve_default_model(
    session: AsyncSession,
    *,
    provider_id: str,
    db_id: int,
    media_type: str,
    generation_type: str | None = None,
) -> CustomProviderModel:
    """Get the provider's single default enabled model for this media_type × bucket.

    Zero and multiple rows both fail loud: silently picking one when no single row can be chosen means silently switching models,
    and multiple rows only appear in states like "two defaults set for this bucket" that should have been blocked at save time.

    Raises:
        ValueError: no default model, or several default models for this bucket.

    取该供应商在该 media_type × 桶上唯一的默认已启用模型。

    零行与多行都 fail loud：挑不出唯一那一行时静默取其中之一等于静默换模型，而多行只在
    「该桶配了两个默认」这种保存期本该拦住的状态下出现。

    Raises:
        ValueError: 没有默认模型，或该桶有多个默认模型。
    """
    candidates = await CustomProviderRepository(session).list_default_models(db_id, media_type)
    candidates = await filter_by_bucket(session, candidates, generation_type)
    label = f"{generation_type} {media_type}" if generation_type is not None else media_type
    if not candidates:
        raise ValueError(f"自定义供应商 {provider_id} 没有默认 {label} 模型")
    if len(candidates) > 1:
        conflict = ", ".join(model.model_id for model in candidates)
        raise ValueError(f"自定义供应商 {provider_id} 有多个默认 {label} 模型：{conflict}")
    return candidates[0]
