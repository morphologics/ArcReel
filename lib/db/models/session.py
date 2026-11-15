"""Agent session ORM model."""

from __future__ import annotations

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin, UserOwnedMixin


class AgentSession(TimestampMixin, UserOwnedMixin, Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    sdk_session_id: Mapped[str] = mapped_column(String, unique=True)
    project_name: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, server_default="")
    status: Mapped[str] = mapped_column(String, server_default="idle")
    # Branch session pointer: non-null means this session has been superseded by a rewrite fork (see docs/adr/0058).
    # The pointer is the marker and does not use status: status carries the lifecycle, and idle eviction and startup
    # interrupt both run on it.
    # 分支会话指针：非空即表示本会话已被改写分叉取代（见 docs/adr/0058）。
    # 指针即标记，不占用 status——status 承载生命周期，闲置驱逐与启动期
    # interrupt 都按它运转。
    superseded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # Fork lineage: which message of which session this session was forked from; written at row creation and never changed.
    # fork_anchor_uuid is in the transcript domain: the anchor message itself is not copied with the prefix and stays only
    # in the original session, while the same historical message has the same transcript uuid in all branches, so cross-branch alignment comes for free.
    # 分叉血统：本会话是从哪个会话的哪条消息处分叉出来的，建行时写入即不再变化。
    # fork_anchor_uuid 取 transcript 域——锚点消息本身不随前缀复制、只留在原会话，
    # 而同一条历史消息在所有分支里的 transcript uuid 相同，跨分支对齐由此免费。
    fork_parent_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fork_anchor_uuid: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_agent_sessions_project", "project_name", "updated_at"),
        Index("idx_agent_sessions_status", "status"),
    )
