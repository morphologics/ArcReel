"""Model tiers of an Agent credential: the model name each tier actually calls, and the catalog entry it maps to (docs/adr/0102).

The tiers are the main model plus Claude Code's four model routes: the three capability levels ``haiku`` / ``sonnet`` /
``opus`` and subagents. Environment variable assembly, the pricing basis snapshot at session connect, and the tier
pricing status on the settings page share the "model name the tier actually calls" defined here, so all three get the
same names for the same credential.

Agent 凭证的模型档位：每个档位实际调用的模型名，以及它按模型目录对应到的条目（docs/adr/0102）。

档位是主模型与 Claude Code 的四个模型路由：``haiku`` / ``sonnet`` / ``opus`` 三个能力等级与子智能体。环境变量组装、
会话连接时的计价依据快照、设置页的档位定价状态共用这里的「档位实际调用的模型名」，三处对同一条凭证得到同样的名字。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from arcreel_catalog_core import normalize_model_name
from lib.catalog import query as catalog

if TYPE_CHECKING:
    from lib.db.models.agent_credential import AgentAnthropicCredential

Tier = Literal["model", "haiku", "sonnet", "opus", "subagent"]

#: Fixed order of tiers: main model first. When a model name appears in several tiers, the first tier with a binding is used.
#: 档位的固定顺序：主模型在前。同一模型名出现在几个档位时，取第一个带绑定的档位。
TIERS: tuple[Tier, ...] = ("model", "haiku", "sonnet", "opus", "subagent")

#: Anthropic official preset: empty tiers are resolved by the CLI itself, not filled with the main model on the user's behalf.
#: Anthropic 官方预设：留空的档位由 CLI 自行解析，不替用户填成主模型。
OFFICIAL_PRESET_ID = "anthropic-official"

#: Field names of the tiers on the credential. / 档位在凭证上的字段名。
_CREDENTIAL_FIELDS: Mapping[Tier, str] = {
    "model": "model",
    "haiku": "haiku_model",
    "sonnet": "sonnet_model",
    "opus": "opus_model",
    "subagent": "subagent_model",
}

#: Legacy keys of the tiers in system settings: used as fallback when a tier on the credential is empty (the configuration method before credentials).
#: 档位在系统设置里的旧键：凭证上的档位留空时以它兜底（凭证之前的配置方式）。
TIER_SETTING_KEYS: Mapping[Tier, str] = {
    "model": "anthropic_model",
    "haiku": "anthropic_default_haiku_model",
    "sonnet": "anthropic_default_sonnet_model",
    "opus": "anthropic_default_opus_model",
    "subagent": "claude_code_subagent_model",
}


def tier_models(credential: AgentAnthropicCredential, settings: Mapping[str, str]) -> dict[Tier, str]:
    """The model name each tier actually calls; an empty string means it is left to the CLI.

    Tiers left empty on the credential first take the legacy value from system settings. When the credential is not
    the Anthropic official preset, the four route tiers that are still empty are filled with the main model name, so the
    CLI doesn't fall back to the default Claude model name.

    各档位实际调用的模型名；空串表示交给 CLI 决定。

    凭证上留空的档位先取系统设置里的旧值。凭证不是 Anthropic 官方预设时，仍为空的四个路由档位填成主模型名，
    避免 CLI 退回默认的 Claude 模型名。
    """
    names: dict[Tier, str] = {
        tier: (getattr(credential, field) or settings.get(TIER_SETTING_KEYS[tier], "").strip())
        for tier, field in _CREDENTIAL_FIELDS.items()
    }
    main_model = names["model"]
    if main_model and credential.preset_id != OFFICIAL_PRESET_ID:
        for tier in TIERS[1:]:
            if not names[tier]:
                names[tier] = main_model
    return names


@dataclass(frozen=True)
class AgentPricing:
    """Pricing basis of an Agent session: the Agent credential whose environment variables were injected at session connect, with its multiplier and tier entry bindings at the time (docs/adr/0102).

    Taken from the same credential row together with the environment variables at session connect; all turns of that
    connection are priced by it: changing the credential's multiplier or tier bindings during a session doesn't affect
    sessions already started, only sessions started or resumed afterwards are priced with the new values. The defaults
    correspond to having no effective credential, with environment variables falling back to system settings.

    Agent 会话的计价依据：会话连接时注入环境变量的那条 Agent 凭证，以及它当时的倍率与档位条目绑定（docs/adr/0102）。

    会话连接时与环境变量一起从同一行凭证取下，这段连接的轮次都按它计价：会话进行中修改凭证的倍率或档位绑定，已经开始的
    会话不受影响，之后开始或恢复的会话才按新值计价。默认值对应没有生效凭证、环境变量回退到系统设置的情形。
    """

    credential_id: int | None = None
    multiplier: float = 1.0
    #: Tiers with a binding, in :data:`TIERS` order: (model name the tier actually calls, entry binding "model vendor/official model name").
    #: 带绑定的档位，按 :data:`TIERS` 的顺序：（档位实际调用的模型名，条目绑定「模型厂商/官方模型名」）。
    tier_bindings: tuple[tuple[str, str], ...] = ()

    def entry_binding_for(self, model: str) -> str | None:
        """Entry binding of the tier the API model name ``model`` belongs to; model name comparison ignores case and namespace. When the same
        model name is in several tiers the first tier with a binding is used; ``None`` when there is none, and the model name maps to an entry by name.

        API 模型名 ``model`` 所在档位的条目绑定；模型名比较忽略大小写与命名空间。同一模型名在几个档位时取第一个
        带绑定的档位，没有时为 ``None``，模型名按名字对应条目。"""
        target = normalize_model_name(model)
        return next((ref for name, ref in self.tier_bindings if normalize_model_name(name) == target), None)


def agent_pricing(credential: AgentAnthropicCredential, settings: Mapping[str, str]) -> AgentPricing:
    """Snapshot of the credential's current pricing basis; tier model name rules are the same as :func:`tier_models`.

    凭证此刻的计价依据快照；档位模型名的规则同 :func:`tier_models`。"""
    bindings = credential.tier_bindings or {}
    names = tier_models(credential, settings)
    return AgentPricing(
        credential_id=credential.id,
        multiplier=credential.multiplier,
        tier_bindings=tuple((names[tier], ref) for tier in TIERS if (ref := bindings.get(tier)) and names[tier]),
    )


@dataclass(frozen=True)
class TierStatus:
    """Pricing status of one tier, looked up against the current catalog, not stored.

    ``model_name`` being ``None`` means the tier is empty and the CLI decides which model to call; then only the two
    binding fields are meaningful. ``manually_bound`` means the tier's entry binding points to an entry in the catalog
    and the mapping comes from the binding; when the bound entry is no longer in the catalog it falls back to mapping by
    model name and this is false. ``binding_stale`` means the tier stores a binding but the bound entry is no longer in
    the catalog (the official data delisted it): the mapping has fallen back to model name but the binding is still
    stored, so the UI still offers "unbind". ``priced`` means the mapped entry has a price table the current code recognizes.

    一个档位的定价状态，按当前目录现查，不存储。

    ``model_name`` 为 ``None`` 表示档位留空、由 CLI 决定调用哪个模型，此时只有两个绑定字段有意义。``manually_bound`` 表示
    档位的条目绑定指向目录里的条目、对应关系取自绑定；绑定的条目已不在目录里时回到按模型名对应，记为假。
    ``binding_stale`` 表示档位存着绑定、但所绑条目已不在目录里（官方数据下架了条目）：对应关系已回到按模型名，
    绑定还存着，界面据此仍提供「解除绑定」。``priced`` 表示对应到的条目有当前代码认得的价目表。
    """

    tier: Tier
    model_name: str | None
    entry_display_name: str | None
    manually_bound: bool
    binding_stale: bool
    priced: bool


def tier_statuses(credential: AgentAnthropicCredential, settings: Mapping[str, str]) -> list[TierStatus]:
    """Pricing status of all of the credential's tiers, in :data:`TIERS` order. / 凭证全部档位的定价状态，按 :data:`TIERS` 的顺序。"""
    bindings = credential.tier_bindings or {}
    statuses: list[TierStatus] = []
    for tier, name in tier_models(credential, settings).items():
        ref = bindings.get(tier)
        bound = catalog.find_bound_entry(ref, "text") if ref else None
        stale = bool(ref) and bound is None
        if not name:
            # When the tier is empty the binding doesn't take effect but is still stored: report it truthfully so the user can clear it.
            # 档位留空时绑定不生效，但仍存着：照实报出，用户才清得掉。
            statuses.append(
                TierStatus(tier, None, None, manually_bound=bound is not None, binding_stale=stale, priced=False)
            )
            continue
        entry = bound or catalog.find_entry(name, media_type="text")
        statuses.append(
            TierStatus(
                tier,
                name,
                entry.display_name if entry is not None else None,
                manually_bound=bound is not None,
                binding_stale=stale,
                priced=entry is not None and entry.price is not None and entry.price.is_priceable,
            )
        )
    return statuses
