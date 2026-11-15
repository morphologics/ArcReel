"""Agent Anthropic credential ORM.

Each user has at most one is_active=True row, guaranteed by a partial unique index (same pattern as ProviderCredential).

Agent Anthropic 凭证 ORM。

每个 user 至多一条 is_active=True，由 partial unique index 保证 (与
ProviderCredential 同模式)。
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import DEFAULT_USER_ID, Base, TimestampMixin


class AgentAnthropicCredential(TimestampMixin, Base):
    """Multiple Anthropic credentials saved by the user; active can be switched with one click in the UI.

    用户保存的多套 Anthropic 凭证；可在 UI 上一键切换 active。"""

    __tablename__ = "agent_anthropic_credentials"
    __table_args__ = (
        Index("ix_agent_credential_user", "user_id"),
        # At most one is_active=True per user / 每个 user 至多一条 is_active=True
        Index(
            "uq_agent_credential_one_active_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default=DEFAULT_USER_ID)
    preset_id: Mapped[str] = mapped_column(String(64), nullable=False)  # "deepseek" | "__custom__" | ...
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # plaintext; read-out API masks it with mask_secret
    api_key: Mapped[str] = mapped_column(Text, nullable=False)  # 明文，读出 API mask_secret 脱敏
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    haiku_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sonnet_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opus_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subagent_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Agent credential multiplier (docs/adr/0100, 0102): applied to the reference cost of every Agent call via this credential, at this single level only; 0 records 0.
    # Agent 凭证倍率（docs/adr/0100、0102）：乘在经这条凭证的每次 Agent 调用的参考费用上，只此一级；为 0 时记 0。
    multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default=text("1"))
    # Mapping from tier to entry binding ("model vendor/official model name"); tiers in lib.agent.credential_tiers.TIERS. NULL or a missing tier =
    # look up by model name; when the bound entry is no longer in the catalogue, also fall back to lookup by model name.
    # 档位到条目绑定（「模型厂商/官方模型名」）的映射，档位见 lib.agent.credential_tiers.TIERS。NULL 或缺某个档位 =
    # 按模型名现查；绑定的条目已不在目录里时同样回到按模型名现查。
    tier_bindings: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
