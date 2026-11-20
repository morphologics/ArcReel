"""YouReel: repository for the client error reports (Settings → Options → Diagnostics)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select

from lib.db.base import utc_now
from lib.db.models.client_error_report import CLIENT_ERROR_REPORTS_KEPT, ClientErrorReport
from lib.db.repositories.base import BaseRepository, rowcount


@dataclass(frozen=True)
class NewClientErrorReport:
    """One report as it is stored: already trimmed and with query strings stripped by the caller."""

    kind: str
    message: str
    detail: dict[str, Any] | None
    user_agent: str | None
    app_build: str | None


class ClientErrorReportRepository(BaseRepository):
    async def add_many(self, reports: Sequence[NewClientErrorReport], *, keep: int = CLIENT_ERROR_REPORTS_KEPT) -> int:
        """Insert the reports, then prune so only the newest ``keep`` rows stay. Returns how many were inserted."""
        now = utc_now()
        for report in reports:
            self.session.add(
                ClientErrorReport(
                    created_at=now,
                    kind=report.kind,
                    message=report.message,
                    detail=report.detail,
                    user_agent=report.user_agent,
                    app_build=report.app_build,
                )
            )
        await self.session.flush()
        # The id of the oldest row still kept; everything older goes (ids are monotonic, see the model)
        cutoff = await self.session.scalar(
            select(ClientErrorReport.id).order_by(ClientErrorReport.id.desc()).offset(keep - 1).limit(1)
        )
        if cutoff is not None:
            await self.session.execute(delete(ClientErrorReport).where(ClientErrorReport.id < cutoff))
        return len(reports)

    async def list_recent(self, limit: int) -> list[ClientErrorReport]:
        """The newest ``limit`` reports, newest first."""
        result = await self.session.execute(
            select(ClientErrorReport).order_by(ClientErrorReport.id.desc()).limit(limit)
        )
        return list(result.scalars())

    async def clear(self) -> int:
        """Delete every report; returns how many were deleted."""
        return rowcount(await self.session.execute(delete(ClientErrorReport)))
