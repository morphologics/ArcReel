"""Tests for ConfigResolver.resolve_resolution.

resolve_resolution goes through the public interface (no assertions on private functions) and resolves via
project.model_settings → legacy video_model_settings → custom provider default → None;
the custom provider default path is asserted with a real in-memory DB + real CustomProviderModel.

测试 ConfigResolver.resolve_resolution。

resolve_resolution 走公开接口（不断言私有函数），按
project.model_settings → legacy video_model_settings → 自定义供应商默认 → None 解析；
自定义供应商默认路径用真实内存 DB + 真实 CustomProviderModel 断言。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.config.resolver import (
    ConfigResolver,
    constrain_durations,
)
from lib.custom_provider import make_provider_id
from lib.db.models.custom_provider import CustomProvider, CustomProviderModel


@pytest.fixture
async def resolver(db_session: AsyncSession) -> ConfigResolver:
    factory = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    return ConfigResolver(factory, _bound_session=db_session)


async def _add_custom_video_model(db_session: AsyncSession, model_id: str, resolution: str | None) -> str:
    """Create a custom video model with the given resolution and return its registry provider_id string.

    建一个带指定 resolution 的自定义视频 model，返回其 registry provider_id 字符串。"""
    provider = CustomProvider(
        display_name="VideoProv",
        discovery_format="openai",
        base_url="https://api.example.com",
        api_key="k",
    )
    db_session.add(provider)
    await db_session.flush()

    model = CustomProviderModel(
        provider_id=provider.id,
        model_id=model_id,
        display_name="Vid Model",
        endpoint="newapi-video",
        is_default=True,
        is_enabled=True,
        resolution=resolution,
    )
    db_session.add(model)
    await db_session.flush()
    return make_provider_id(provider.id)


# --- Pure project dict precedence (non-custom provider, DB default always None) --- / --- 纯项目字典优先级（非自定义供应商，DB 默认恒 None） ---


@pytest.mark.asyncio
async def test_returns_none_when_nothing_configured(resolver: ConfigResolver):
    assert await resolver.resolve_resolution({}, "gemini-aistudio", "veo-3.1-lite-generate-preview") is None


@pytest.mark.asyncio
async def test_legacy_only(resolver: ConfigResolver):
    project = {"video_model_settings": {"veo-3.1": {"resolution": "1080p"}}}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "veo-3.1") == "1080p"


@pytest.mark.asyncio
async def test_model_settings_overrides_legacy(resolver: ConfigResolver):
    project = {
        "model_settings": {"gemini-aistudio/veo-3.1": {"resolution": "720p"}},
        "video_model_settings": {"veo-3.1": {"resolution": "1080p"}},
    }
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "veo-3.1") == "720p"


@pytest.mark.asyncio
async def test_empty_string_override_treated_as_unset(resolver: ConfigResolver):
    project = {"model_settings": {"gemini-aistudio/m": {"resolution": ""}}}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") is None


@pytest.mark.asyncio
async def test_composite_key_format_uses_slash(resolver: ConfigResolver):
    project = {"model_settings": {"gemini-aistudio/b": {"resolution": "4K"}}}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "b") == "4K"


@pytest.mark.asyncio
async def test_tolerates_null_entries(resolver: ConfigResolver):
    # project.json may be hand-edited to a null value; it should neither crash nor be treated as configured.
    # project.json 可能被手编为 null 值；既不应崩也不应当作已配置。
    project = {
        "model_settings": {"gemini-aistudio/b": None},
        "video_model_settings": {"m": None},
    }
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "b") is None
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") is None


@pytest.mark.asyncio
async def test_tolerates_top_level_field_as_string(resolver: ConfigResolver):
    # Hand-edited dirty data: the model_settings / video_model_settings top level itself written as a string.
    # 手编脏数据：model_settings / video_model_settings 顶层本身被写成字符串。
    project = {"model_settings": "oops", "video_model_settings": "also-broken"}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") is None


@pytest.mark.asyncio
async def test_tolerates_top_level_field_as_list(resolver: ConfigResolver):
    # Hand-edited dirty data: the model_settings / video_model_settings top level itself written as a list.
    # 手编脏数据：model_settings / video_model_settings 顶层本身被写成列表。
    project = {"model_settings": ["gemini-aistudio/m"], "video_model_settings": ["m"]}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") is None


@pytest.mark.asyncio
async def test_tolerates_composite_key_entry_as_string(resolver: ConfigResolver):
    # Hand-edited dirty data: the entry of a specific composite key in model_settings written as a string instead of a dict.
    # 手编脏数据：model_settings 里具体某个复合 key 的 entry 被写成字符串而非 dict。
    project = {"model_settings": {"gemini-aistudio/m": "1080p"}}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") is None


@pytest.mark.asyncio
async def test_tolerates_legacy_model_entry_as_list(resolver: ConfigResolver):
    # Hand-edited dirty data: the entry of a specific model in legacy video_model_settings written as a list.
    # 手编脏数据：legacy video_model_settings 里具体某个 model 的 entry 被写成列表。
    project = {"video_model_settings": {"m": ["1080p"]}}
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") is None


@pytest.mark.asyncio
async def test_dirty_model_settings_falls_through_to_legacy(resolver: ConfigResolver):
    # Dirty data at the model_settings top level should not drag down normal resolution on the legacy fallback path.
    # model_settings 顶层脏数据不应连坐拖垮 legacy 兜底路径的正常解析。
    project = {
        "model_settings": "oops",
        "video_model_settings": {"m": {"resolution": "1080p"}},
    }
    assert await resolver.resolve_resolution(project, "gemini-aistudio", "m") == "1080p"


# --- Custom provider default (real DB) --- / --- 自定义供应商默认（真实 DB） ---


@pytest.mark.asyncio
async def test_returns_custom_default_when_only_custom(resolver: ConfigResolver, db_session: AsyncSession):
    provider_id = await _add_custom_video_model(db_session, "my-model", "720p")
    assert await resolver.resolve_resolution({}, provider_id, "my-model") == "720p"


@pytest.mark.asyncio
async def test_custom_default_none_when_model_has_no_resolution(resolver: ConfigResolver, db_session: AsyncSession):
    provider_id = await _add_custom_video_model(db_session, "my-model", None)
    assert await resolver.resolve_resolution({}, provider_id, "my-model") is None


@pytest.mark.asyncio
async def test_custom_default_none_when_model_missing(resolver: ConfigResolver, db_session: AsyncSession):
    provider_id = await _add_custom_video_model(db_session, "my-model", "720p")
    assert await resolver.resolve_resolution({}, provider_id, "other-model") is None


@pytest.mark.asyncio
async def test_project_override_wins_over_custom_default(resolver: ConfigResolver, db_session: AsyncSession):
    provider_id = await _add_custom_video_model(db_session, "m", "1K")
    project = {"model_settings": {f"{provider_id}/m": {"resolution": "2K"}}}
    assert await resolver.resolve_resolution(project, provider_id, "m") == "2K"


@pytest.mark.asyncio
async def test_legacy_wins_over_custom_default(resolver: ConfigResolver, db_session: AsyncSession):
    provider_id = await _add_custom_video_model(db_session, "m", "720p")
    project = {"video_model_settings": {"m": {"resolution": "1080p"}}}
    assert await resolver.resolve_resolution(project, provider_id, "m") == "1080p"


@pytest.mark.asyncio
async def test_falls_through_to_custom_when_project_empty_string(resolver: ConfigResolver, db_session: AsyncSession):
    provider_id = await _add_custom_video_model(db_session, "m", "1K")
    project = {"model_settings": {f"{provider_id}/m": {"resolution": ""}}}
    assert await resolver.resolve_resolution(project, provider_id, "m") == "1K"


# ---------------------------------------------------------------------------
# Duration coupling constraints / 时长联动约束
# ---------------------------------------------------------------------------

# Declares {1080p:[8], 4k:[8]} + reference images [8]
_VEO = ("gemini-aistudio", "veo-3.1-generate-preview")  # 声明 {1080p:[8], 4k:[8]} + 参考图 [8]
_HAILUO = ("minimax", "MiniMax-Hailuo-2.3")  # Declares {1080p:[6]}, no reference image declaration / 声明 {1080p:[6]}，无参考图声明


def test_constrain_durations_by_resolution():
    """Registered with a declaration: narrow by the declaration, case-insensitive.

    已登记且有声明时按声明收窄，大小写不敏感。"""
    assert constrain_durations(*_VEO, [4, 6, 8], resolution="4K") == [8]
    assert constrain_durations(*_VEO, [4, 6, 8], resolution="1080p") == [8]


def test_constrain_durations_by_reference_images():
    """The reference image constraint triggers independently of resolution; not applied when the reference image path is not taken.

    参考图约束独立于分辨率触发；未走参考图路径时不施加。"""
    assert constrain_durations(*_VEO, [4, 6, 8], uses_reference_images=True) == [8]
    assert constrain_durations(*_VEO, [4, 6, 8], uses_reference_images=False) == [4, 6, 8]
    # Model with no reference image declaration: that dimension is not narrowed / 无参考图声明的型号：该维度不收窄
    assert constrain_durations(*_HAILUO, [6, 10], uses_reference_images=True) == [6, 10]


def test_constrain_durations_drops_vidu_r2v_ghost_tiers():
    """The Vidu reference-to-video endpoint has a 3-second duration minimum: on the reference image path the ghost 1s / 2s tiers are removed.

    If they are not removed, a user picking 1s in an r2v project gets silently bumped to 3 seconds by the backend after submit and billed for 3 seconds.

    Vidu 参考生视频端点时长下限为 3 秒：走参考图路径时 1s / 2s 幽灵档位被剔除。

    不剔除的后果是用户在 r2v 项目选中 1s，提交后被 backend 静默取到 3 秒并按 3 秒计费。
    """
    full = list(range(1, 17))
    assert constrain_durations("vidu", "viduq3-turbo", full, uses_reference_images=True) == list(range(3, 17))
    # Non-reference-image paths (text/image-to-video) are unaffected; 1s / 2s are valid there / 非参考图路径（文/图生视频）不受影响，1s / 2s 在那里合法
    assert constrain_durations("vidu", "viduq3-turbo", full, uses_reference_images=False) == full


def test_constrain_durations_both_dimensions_intersect():
    """When both constraints apply, take the intersection.

    两条约束同时生效时取交集。"""
    assert constrain_durations(*_HAILUO, [6, 10], resolution="1080p", uses_reference_images=True) == [6]


def test_constrain_durations_falls_back():
    """No declaration / unregistered model / empty intersection / missing parameters: return the original candidates, never emptying them.

    无声明 / 未登记型号 / 交集为空 / 缺参数时返回原候选，不把候选清空。"""
    # No declaration for this resolution / 该分辨率无声明
    assert constrain_durations(*_VEO, [4, 6, 8], resolution="720p") == [4, 6, 8]
    # Model not registered (relay station / custom provider wrapper) / 型号未登记（中转站 / 自定义供应商包装）
    assert constrain_durations("gemini-aistudio", "veo-3.1-via-relay", [4, 6, 8], resolution="4k") == [4, 6, 8]
    # An empty intersection stays an empty set; the fact consumer handles it. / 交集为空时保持空集，由事实消费方处理。
    assert constrain_durations(*_VEO, [4, 6], resolution="4k") == []
    assert constrain_durations(*_VEO, [4, 6], uses_reference_images=True) == []
    # resolution missing and not on the reference image path: neither dimension triggers / resolution 缺失且不走参考图：两维都不触发
    assert constrain_durations(*_VEO, [4, 6, 8]) == [4, 6, 8]
    # Identity missing (capabilities not resolvable) / 身份缺失（能力不可解析）
    assert constrain_durations(None, None, [4, 6, 8], resolution="4k") == [4, 6, 8]
    # Empty candidates returned as is / 空候选原样返回
    assert constrain_durations(*_VEO, [], resolution="4k") == []
