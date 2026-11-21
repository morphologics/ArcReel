"""YouReel: errors a member hit in the studio's browser UI, reported by the page (Settings → Options → Diagnostics)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, utc_now

# Only the newest rows are kept: every insert prunes older ones beyond this (ClientErrorReportRepository.add_many).
# Errors and performance records are kept apart, each group with its own cap, so a run of performance records (one per
# page load, jank, slow server answers) never pushes the errors out.
CLIENT_ERROR_REPORTS_KEPT = 1000
PERF_REPORTS_KEPT = 500

#: The error kinds (reported by the page)
ERROR_KINDS: frozenset[str] = frozenset({"toast", "exception", "request"})
#: The performance kinds: ``perf`` (one per page load), ``jank`` (the page's main thread blocked), both reported by
#: the page; ``server_slow`` (a slow or very large server answer), written by the server itself (server/perf_timing.py)
PERF_KINDS: frozenset[str] = frozenset({"perf", "jank", "server_slow"})


def kind_group(kind: str) -> frozenset[str]:
    """The kinds that share a cap with ``kind``."""
    return PERF_KINDS if kind in PERF_KINDS else ERROR_KINDS


def kept_for(kind: str) -> int:
    """How many rows of ``kind``'s group are kept."""
    return PERF_REPORTS_KEPT if kind in PERF_KINDS else CLIENT_ERROR_REPORTS_KEPT


class ClientErrorReport(Base):
    """One error the studio's browser UI reported: an error toast, an uncaught exception or a failed request; or a
    performance record (``perf`` / ``jank`` from the page, ``server_slow`` from the server, see :data:`PERF_KINDS`).

    ``detail`` holds what helps to find the cause (page path, method + API path + status, component / action label,
    a trimmed stack); query strings were stripped from every path in it before storing, so tokens in URLs never land
    here. A global table without a user dimension: a studio serves one member, and the list is for us to look at.
    ``id`` is monotonic (``AUTOINCREMENT``) so pruning by id keeps the newest rows.
    """

    __tablename__ = "client_error_reports"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    app_build: Mapped[str | None] = mapped_column(String(128), nullable=True)
