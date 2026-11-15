"""Display names of providers and models: registry names for built-in ones, user-entered names for custom ones.

Usage reports and failure messages share this name lookup. Names are looked up at read time, not snapshotted when
writing failures or billing: after a provider is renamed, old records show the new name, and the language follows the request reading it.

供应商与模型的显示名：内置的取注册表名称，自定义的取用户填写的名称。

用量报表与失败文案共用这里的查名逻辑。名称在读取时现查，不在写入失败或记账时快照：供应商
改名后旧记录跟着显示新名字，语言也跟随读取它的请求。
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from lib.backends.providers import PROVIDER_ANTHROPIC, PROVIDER_GEMINI
from lib.db.repositories.agent_credential_repo import AgentCredentialRepository
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.i18n import DEFAULT_LOCALE, _, translate_or
from lib.i18n.display_names import DisplayNames, agent_provider_id, is_agent_provider

# Display fallback for bare provider values outside the registry; these rows are not migrated, only given a friendly display name when shown:
# - "gemini": before the identity inversion, text gemini calls were recorded as bare "gemini" via backend.name (image/video already
#   "gemini-aistudio"); the registry only has the new-format keys (gemini-aistudio / gemini-vertex), so the bare value finds no meta.
# - "anthropic": Agent records were always "anthropic" before per-credential billing; still recorded when there is no active credential and env vars come from system settings.
# 注册表之外的裸 provider 值的显示兜底，这些行不迁移，仅在显示时补一个友好显示名：
# - "gemini"：身份反转前，文本 gemini 调用以 backend.name 落账为裸 "gemini"（图像/视频侧已是
#   "gemini-aistudio"）；registry 只登记新格式 key（gemini-aistudio / gemini-vertex），故裸值查不到 meta。
# - "anthropic"：Agent 记录在按凭证记账之前一律记 "anthropic"；没有生效凭证、环境变量取自系统设置时仍记它。
_LEGACY_PROVIDER_DISPLAY_NAMES = {PROVIDER_GEMINI: "Gemini", PROVIDER_ANTHROPIC: "Anthropic"}


def _builtin_provider_name(provider_id: str) -> str | None:
    """Default-language registry name of a built-in provider (incl. legacy bare values); ``None`` if not a built-in provider.

    内置供应商（含存量裸值）在注册表里的默认语言名称；不是内置供应商时返回 ``None``。"""
    # the registry imports usage_repo back via dashscope_shared; a top-level import would be circular
    from lib.config.registry import PROVIDER_REGISTRY  # 注册表经 dashscope_shared 回引 usage_repo，顶层导入成环

    meta = PROVIDER_REGISTRY.get(provider_id)
    return meta.display_name if meta else _LEGACY_PROVIDER_DISPLAY_NAMES.get(provider_id)


async def _agent_provider_names(session: AsyncSession) -> dict[str, str]:
    """``agent-<credential id>`` -> the credential's current display name. / ``agent-<凭证 id>`` → 凭证当前的显示名。"""
    return {
        agent_provider_id(cred.id): cred.display_name
        for cred in await AgentCredentialRepository(session).list_for_user()
    }


async def provider_display_names(
    session: AsyncSession, provider_ids: set[str], *, locale: str = DEFAULT_LOCALE
) -> dict[str, str]:
    """Provider id -> display name; unknown ones (incl. malformed ids like ``"custom-abc"``) fall back to the id itself.

    Built-in providers use the default-language registry name. ``agent-<credential id>`` uses the Agent credential's
    current display name; if the credential was deleted, "deleted Agent provider" per ``locale``.

    供应商 id → 显示名；查不到的（含畸形 id，如 ``"custom-abc"``）回退 id 本身。

    内置供应商取默认语言的注册表名称。``agent-<凭证 id>`` 取 Agent 凭证当前的显示名，凭证已删除时
    按 ``locale`` 给出「已删除的 Agent 供应商」。
    """
    agent_ids = {provider_id for provider_id in provider_ids if is_agent_provider(provider_id)}
    agents: dict[str, str] = {}
    if agent_ids:
        known = await _agent_provider_names(session)
        deleted = _("deleted_agent_provider_display_name", locale=locale)
        agents = {provider_id: known.get(provider_id, deleted) for provider_id in agent_ids}
    builtin = {
        provider_id: _builtin_provider_name(provider_id) for provider_id in provider_ids if provider_id not in agent_ids
    }
    custom: dict[str, str] = {}
    if None in builtin.values():
        custom = {cp.provider_id: cp.display_name for cp in await CustomProviderRepository(session).list_providers()}
    return {
        **agents,
        **{provider_id: name or custom.get(provider_id, provider_id) for provider_id, name in builtin.items()},
    }


def build_display_names(
    locale: str,
    *,
    custom_providers: Mapping[str, str],
    custom_models: Mapping[tuple[str, str], str],
    agent_providers: Mapping[str, str] | None = None,
) -> DisplayNames:
    """Merge custom provider and model names on top of the built-in names from the registry and model catalogue, rendered per ``locale``.

    Built-in provider names with a translation table are translated by language, otherwise the registry's original name
    is used; model names come from the catalogue entry's display name, language-independent. Hidden models are included
    too, since old failure records may still reference them. ``agent_providers`` maps ``agent-<credential id>`` to the Agent credential display name.

    在注册表与模型目录的内置名称之上合入自定义供应商与模型的名称，按 ``locale`` 成文。

    内置供应商的名称有译名表的按语言翻译，没有的用注册表的原名；模型名取模型目录条目的显示名，不分语言。
    已隐藏的模型同样收录，旧失败记录仍可能引用它们。``agent_providers`` 是 ``agent-<凭证 id>`` 到
    Agent 凭证显示名的映射。
    """
    from lib.catalog import query as catalog  # same as _builtin_provider_name / 同 _builtin_provider_name
    from lib.config.registry import PROVIDER_REGISTRY  # same as _builtin_provider_name / 同 _builtin_provider_name

    providers: dict[str, str] = dict(_LEGACY_PROVIDER_DISPLAY_NAMES)
    models: dict[tuple[str, str], str] = {}
    for provider_id, meta in PROVIDER_REGISTRY.items():
        providers[provider_id] = translate_or(f"provider_name_{provider_id}", meta.display_name, locale)
        for model_id, info in catalog.list_models(provider_id).items():
            models[(provider_id, model_id)] = info.display_name
    return DisplayNames(
        providers={**providers, **custom_providers, **(agent_providers or {})},
        models={**models, **custom_models},
        deleted_provider=_("deleted_provider_display_name", locale=locale),
        deleted_agent_provider=_("deleted_agent_provider_display_name", locale=locale),
    )


async def load_display_names(session: AsyncSession, locale: str) -> DisplayNames:
    """Full display name catalogue rendered per ``locale``; custom providers and models use the user-entered names; disabled models are included too.

    按 ``locale`` 成文的全量显示名目录；自定义供应商与模型用用户填写的名称，停用的模型同样收录。"""
    custom_providers: dict[str, str] = {}
    custom_models: dict[tuple[str, str], str] = {}
    for provider, provider_models in await CustomProviderRepository(session).list_providers_with_models():
        provider_id = provider.provider_id
        custom_providers[provider_id] = provider.display_name
        for model in provider_models:
            custom_models[(provider_id, model.model_id)] = model.display_name
    return build_display_names(
        locale,
        custom_providers=custom_providers,
        custom_models=custom_models,
        agent_providers=await _agent_provider_names(session),
    )
