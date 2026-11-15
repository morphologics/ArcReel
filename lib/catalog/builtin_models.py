"""Provider models of built-in providers: enabling and disabling officially listed models, adding and removing by ID by the user (docs/adr/0099).

The model list and all fields of officially listed models come from catalog queries; locally only the enabled state and
user-added models are recorded in the built-in provider model table. An officially listed model without a state row is
enabled as long as its entry isn't delisted. After writing, the caller commits the transaction, then reloads user-added
models into catalog queries (``server.routers.provider_models.reload_user_added_models``).

内置供应商的供应商模型：官方收录模型的启用与停用，用户按 ID 添加与移除（docs/adr/0099）。

模型清单与官方收录模型的全部字段来自目录查询；本地只在内置供应商模型表里记启用状态与用户添加的模型。
官方收录的模型没有状态行时，条目未下架即为启用。写入后由调用方提交事务，再把用户添加的模型重新装入目录查询
（``server.routers.provider_models.reload_user_added_models``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from arcreel_catalog_core import MEDIA_TYPES
from lib.catalog import query as catalog
from lib.catalog.model_id import has_dot_segment
from lib.config import registry
from lib.db.models.builtin_provider_model import BuiltinProviderModelState
from lib.db.repositories.builtin_provider_model_repo import BuiltinProviderModelRepository


class BuiltinModelError(Exception):
    """An operation on a built-in provider model was rejected. ``code`` is the i18n key of the error message; ``not_found`` distinguishes "doesn't exist" from "not allowed".

    内置供应商模型的操作被拒绝。``code`` 是错误文案的 i18n key，``not_found`` 区分「不存在」与「不允许」。"""

    def __init__(self, code: str, *, not_found: bool = False, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.not_found = not_found
        self.params = params


@dataclass(frozen=True)
class BuiltinModelItem:
    """A provider model under a built-in provider, with its enabled state. / 内置供应商名下的一个供应商模型及其启用状态。"""

    view: catalog.ProviderModelView
    enabled: bool

    @property
    def is_default(self) -> bool:
        """The default model designated by official data, and not disabled. / 官方数据指定的默认模型，且没有被停用。"""
        return self.enabled and self.view.model.default


def _enabled(view: catalog.ProviderModelView, row: BuiltinProviderModelState | None) -> bool:
    if row is not None:
        return row.enabled
    return view.entry is None or not view.entry.discontinued


def _ensure_builtin(provider_id: str) -> None:
    if provider_id not in registry.PROVIDER_REGISTRY:
        raise BuiltinModelError("provider_not_found", not_found=True)


async def list_user_added_rows(session: AsyncSession) -> list[BuiltinProviderModelState]:
    """All user-added model rows, for loading into catalog queries. / 全部用户添加的模型行，供装入目录查询。"""
    return [row for row in await BuiltinProviderModelRepository(session).list_all() if row.user_added]


async def list_builtin_models(session: AsyncSession, provider_id: str) -> list[BuiltinModelItem]:
    """All provider models of the provider (officially listed first, user-added after) with enabled state.

    供应商的全部供应商模型（官方收录在前、用户添加在后）及启用状态。"""
    _ensure_builtin(provider_id)
    rows = {row.model_id: row for row in await BuiltinProviderModelRepository(session).list_for_provider(provider_id)}
    return [
        BuiltinModelItem(view, _enabled(view, rows.get(view.model_id))) for view in catalog.provider_models(provider_id)
    ]


def disabled_without_state(provider_id: str) -> frozenset[str]:
    """Model keys that are disabled when there is no state row: officially listed models whose entry has been delisted.

    没有任何状态行时停用的模型键：条目已下架的官方收录模型。"""
    return frozenset(view.model_id for view in catalog.provider_models(provider_id) if not _enabled(view, None))


async def disabled_builtin_models(session: AsyncSession) -> dict[str, frozenset[str]]:
    """Disabled model keys per built-in provider; candidates for the global and project default model settings come only from the rest (enabled) models.

    每个内置供应商停用的模型键；全局与项目默认模型设置的候选只来自其余（已启用的）模型。"""
    rows: dict[tuple[str, str], BuiltinProviderModelState] = {
        (row.provider_id, row.model_id): row for row in await BuiltinProviderModelRepository(session).list_all()
    }
    return {
        provider_id: frozenset(
            view.model_id
            for view in catalog.provider_models(provider_id)
            if not _enabled(view, rows.get((provider_id, view.model_id)))
        )
        for provider_id in registry.PROVIDER_REGISTRY
    }


def _find_item(items: list[BuiltinModelItem], model_id: str) -> BuiltinModelItem:
    item = next((i for i in items if i.view.model_id == model_id), None)
    if item is None:
        raise BuiltinModelError("provider_model_not_found", not_found=True, model=model_id)
    return item


async def set_builtin_model_enabled(
    session: AsyncSession, provider_id: str, model_id: str, enabled: bool
) -> BuiltinModelItem:
    """Enable or disable a provider model, effective immediately. / 启用或停用一个供应商模型，立即生效。"""
    item = _find_item(await list_builtin_models(session, provider_id), model_id)
    await BuiltinProviderModelRepository(session).set_enabled(provider_id, model_id, enabled)
    return BuiltinModelItem(item.view, enabled)


def _media_types_of(views: list[catalog.ProviderModelView]) -> set[str]:
    return {view.model.media_type for view in views if view.official}


@dataclass(frozen=True)
class AddedModel:
    """Result of adding by ID: the model key after adding; when ``official`` is true it matched an officially listed model, and adding enables it.

    按 ID 添加的结果：添加后的模型键；``official`` 为真时对上了官方收录的模型，添加即启用它。"""

    model_id: str
    official: bool


async def add_builtin_model(
    session: AsyncSession,
    provider_id: str,
    model_id: str,
    *,
    media_type: str | None = None,
    entry_binding: str | None = None,
    inferred_media_type: str | None = None,
) -> AddedModel:
    """Add a model by ID.

    First match exactly against the API model names of officially listed models in the list, with amphibious models further
    distinguished by ``media_type``; if matched, enable it. If not matched, it becomes a user-added model, mapped to an entry
    by entry binding or by name (docs/adr/0105); the media type is ``media_type``, or if not given, the media type of the
    entry matched by name, and if none matches, the caller's name-inferred ``inferred_media_type``.

    按 ID 添加模型。

    先按清单中的 API 模型名精确匹配官方收录的模型，两栖模型再按 ``media_type`` 区分；对上了就启用它。
    匹配不上的作为用户添加的模型，经条目绑定或名字对应条目（docs/adr/0105），媒体类型取 ``media_type``，
    没给时取名字对应上的条目的媒体类型，对应不上再取调用方按名字推断的 ``inferred_media_type``。
    """
    _ensure_builtin(provider_id)
    model_id = model_id.strip()
    if not model_id:
        raise BuiltinModelError("builtin_model_id_required")
    if has_dot_segment(model_id):
        raise BuiltinModelError("model_id_dot_segment", model=model_id)
    if media_type is not None and media_type not in MEDIA_TYPES:
        raise BuiltinModelError("builtin_model_media_type_unsupported", media_type=media_type)
    views = catalog.provider_models(provider_id)
    official = [
        v for v in views if v.official and v.api_model_name == model_id and media_type in (None, v.model.media_type)
    ]
    if len(official) > 1:
        raise BuiltinModelError("builtin_model_media_type_required", model=model_id)
    binding = entry_binding.strip() if entry_binding else None
    if not official and binding:
        # When an entry is picked from the "model catalog", model_id is the official model name, which may differ from the API model name in the list
        # (e.g. Ark's dated names): if this provider already lists a model mapping to this entry, enable it instead of creating a user-added model
        # the provider can't call. One entry can have several alias API names (e.g. the two names of Ark's Seedream 5.0 Lite); enable the first in list order.
        # 从「模型目录」选条目时 model_id 是官方模型名，可能与清单里的 API 模型名不同（如方舟带日期的名字）：
        # 这个供应商已收录对应这个条目的模型时启用它，不另建一个供应商调不通的用户添加模型。同一条目可以有多个
        # 别名 API 名（如方舟 Seedream 5.0 Lite 的两个名字），启用清单顺序里的第一个。
        official = [
            v
            for v in views
            if v.official
            and v.entry is not None
            and v.entry.key.ref == binding
            and media_type in (None, v.model.media_type)
        ][:1]
    repo = BuiltinProviderModelRepository(session)
    if official:
        view = official[0]
        await repo.set_enabled(provider_id, view.model_id, True)
        return AddedModel(view.model_id, official=True)

    existing = next((v for v in views if v.model_id == model_id), None)
    if existing is not None:
        raise BuiltinModelError("builtin_model_duplicate", model=model_id)
    resolved_media = media_type or _entry_media_type(model_id) or inferred_media_type
    if resolved_media is None:
        raise BuiltinModelError("builtin_model_media_type_required", model=model_id)
    if resolved_media not in _media_types_of(views):
        raise BuiltinModelError("builtin_model_media_type_unsupported", media_type=resolved_media)
    if binding and catalog.binding_key(binding, resolved_media) is None:
        raise BuiltinModelError("builtin_model_binding_invalid", binding=binding)

    row = await repo.get(provider_id, model_id)
    if row is None:
        await repo.add_user_model(provider_id, model_id, media_type=resolved_media, entry_binding=binding)
    else:
        # Rows no longer listed by official data, with only an enabled state left: convert to user-added models.
        # 官方数据已不再收录、只剩启用状态的行：转为用户添加的模型。
        row.user_added = True
        row.enabled = True
        row.media_type = resolved_media
        row.entry_binding = binding
        await session.flush()
    return AddedModel(model_id, official=False)


def _entry_media_type(model_id: str) -> str | None:
    """The entry's media type when the name maps to an entry; ``None`` for amphibious models or no match.

    名字对应上条目时取条目的媒体类型；两栖模型或对应不上时为 ``None``。"""
    entry = catalog.find_entry(model_id)
    return entry.media_type if entry is not None else None


async def remove_builtin_model(session: AsyncSession, provider_id: str, model_id: str) -> None:
    """Remove a user-added model; officially listed models can only be disabled. / 移除用户添加的模型；官方收录的模型只能停用。"""
    item = _find_item(await list_builtin_models(session, provider_id), model_id)
    if item.view.official:
        raise BuiltinModelError("builtin_model_not_removable", model=model_id)
    await BuiltinProviderModelRepository(session).delete(provider_id, model_id)


_UNCHANGED = object()


async def update_builtin_model(
    session: AsyncSession,
    provider_id: str,
    model_id: str,
    *,
    enabled: bool | None = None,
    new_model_id: str | None = None,
    multiplier: float | None = None,
    capability_overrides: object = _UNCHANGED,
    entry_binding: object = _UNCHANGED,
) -> str:
    """Save the provider settings from the provider model Sheet, returning the model key after saving.

    Officially listed models can only be enabled or disabled, other settings are maintained officially; user-added models can
    additionally change the model ID, the provider model multiplier and capability overrides. ``capability_overrides`` not
    passed means unchanged, ``None`` clears it; the value must already have passed write validation
    (``lib.custom_provider.capability_view.check_user_added_overrides``). ``entry_binding`` likewise: passing
    "model maker/official model name" links to an entry in the catalog, ``None`` unbinds and reverts to mapping by name;
    whether the entry exists is checked by the caller.

    保存供应商模型 Sheet 上的供应商设置，返回保存后的模型键。

    官方收录的模型只能启用、停用，其余设置由官方维护；用户添加的模型另可修改模型 ID、供应商模型倍率与能力覆盖。
    ``capability_overrides`` 不传时不改，传 ``None`` 时清除；值须已经过写入校验
    （``lib.custom_provider.capability_view.check_user_added_overrides``）。``entry_binding`` 同理：传
    「模型厂商/官方模型名」关联到目录里的条目，传 ``None`` 解除，改回按名字对应；条目是否存在由调用方核对。
    """
    item = _find_item(await list_builtin_models(session, provider_id), model_id)
    renamed = new_model_id is not None and new_model_id.strip() != model_id
    if item.view.official:
        if (
            renamed
            or multiplier is not None
            or capability_overrides is not _UNCHANGED
            or entry_binding is not _UNCHANGED
        ):
            raise BuiltinModelError("builtin_model_official_settings", model=model_id)
        if enabled is not None:
            await BuiltinProviderModelRepository(session).set_enabled(provider_id, model_id, enabled)
        return model_id

    repo = BuiltinProviderModelRepository(session)
    row = await repo.get(provider_id, model_id)
    if row is None:
        raise BuiltinModelError("provider_model_not_found", not_found=True, model=model_id)
    target = model_id
    if renamed and new_model_id is not None:
        target = new_model_id.strip()
        if not target:
            raise BuiltinModelError("builtin_model_id_required")
        if has_dot_segment(target):
            raise BuiltinModelError("model_id_dot_segment", model=target)
        views = catalog.provider_models(provider_id)
        if any(v.model_id == target or (v.official and v.api_model_name == target) for v in views):
            raise BuiltinModelError("builtin_model_duplicate", model=target)
        if await repo.get(provider_id, target) is not None:
            raise BuiltinModelError("builtin_model_duplicate", model=target)
        row.model_id = target
    if enabled is not None:
        row.enabled = enabled
    if multiplier is not None:
        row.multiplier = multiplier
    if capability_overrides is not _UNCHANGED:
        row.capability_overrides = capability_overrides if isinstance(capability_overrides, dict) else None
    if entry_binding is not _UNCHANGED:
        binding = entry_binding.strip() if isinstance(entry_binding, str) else ""
        if binding and catalog.binding_key(binding, row.media_type or "") is None:
            raise BuiltinModelError("builtin_model_binding_invalid", binding=binding)
        row.entry_binding = binding or None
    await session.flush()
    return target
