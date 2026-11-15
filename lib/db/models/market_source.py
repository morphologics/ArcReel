"""Market source ORM model: global entity without a user dimension. / 市场源 ORM 模型：全局实体，无 user 维度。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin


class MarketSource(TimestampMixin, Base):
    """A market source registered on this instance.

    ``cached_index`` is the raw index of the last successful fetch, ``fetched_at`` the time of the last successful
    refresh (incl. 304); a failed refresh only changes ``status`` / ``last_error`` and keeps both.

    ``id`` is monotonic and never reused even on SQLite (``AUTOINCREMENT``): frontend filters, in-flight refreshes and
    reordering all identify sources by id, so a source re-added after deletion must not inherit an old id.

    一个登记在本实例的市场源。

    ``cached_index`` 是最近一次成功抓取的索引原文，``fetched_at`` 是最近一次成功刷新（含 304）
    的时间；刷新失败只改 ``status`` / ``last_error``，两者保留。

    ``id`` 在 SQLite 上也单调不复用（``AUTOINCREMENT``）：前端筛选、在途刷新与重排都以 id 指认源，
    删除后再添加的源不能继承旧 id。
    """

    __tablename__ = "market_source"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    address: Mapped[str] = mapped_column(String(2048), nullable=False)
    index_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_index: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
