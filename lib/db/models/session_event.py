"""Session event log ORM model — the only read source for the UI timeline. / 会话事件日志 ORM 模型 — UI 时间线唯一读源。"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin, UserOwnedMixin


class AgentSessionEventLogEntry(TimestampMixin, UserOwnedMixin, Base):
    """Session event log entry — seq increases monotonically per session, append-only.

    Coexisting with the SDK transcript mirror table (agent_session_entries) is an intentional double write: this table is
    the UI read model, entries are finalised at the write point; the transcript is agent memory.

    会话事件日志条目 — 每会话单调递增 seq、append-only。

    与 SDK transcript 镜像表（agent_session_entries）并存是有意双写：
    本表是 UI 读模型，条目在写入点定型；transcript 是 agent 记忆。
    """

    __tablename__ = "agent_session_event_log"

    session_id: Mapped[str] = mapped_column(String, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Request-side idempotency key for accepting user messages; a retry with the same key returns the existing entry without duplicating.
    # 用户消息受理的请求侧幂等键；同键重试返回既有条目，不产生重复。
    client_key: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("session_id", "seq"),
        Index(
            "uq_agent_event_log_client_key",
            "session_id",
            "client_key",
            unique=True,
            postgresql_where=text("client_key IS NOT NULL"),
            sqlite_where=text("client_key IS NOT NULL"),
        ),
        # Lookup index for the cross-session idempotency fallback query (find_new_session_by_client_key):
        # the unique index starts with session_id and cannot serve lookups by client_key alone.
        # 跨会话幂等兜底查询（find_new_session_by_client_key）的检索索引：
        # 唯一索引以 session_id 打头，服务不了仅按 client_key 的查找。
        Index(
            "ix_agent_event_log_client_key",
            "client_key",
            postgresql_where=text("client_key IS NOT NULL"),
            sqlite_where=text("client_key IS NOT NULL"),
        ),
    )
