"""YouReel: repository for the client error reports (Settings → Options → Diagnostics)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select

from lib.db.base import utc_now
from lib.db.models.client_error_report import ClientErrorReport, kept_for, kind_group
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
    async def add_many(self, reports: Sequence[NewClientErrorReport], *, keep: int | None = None) -> int:
        """Insert the reports, then prune each kind group they touch (errors / performance records, see
        ``kind_group``) so only its newest rows stay: ``keep`` rows, or the group's own cap (``kept_for``) when not
        given. Returns how many were inserted."""
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
        groups = {kind_group(report.kind) for report in reports}
        for group in groups:
            kept = keep if keep is not None else kept_for(next(iter(group)))
            in_group = ClientErrorReport.kind.in_(sorted(group))
            # The id of the group's oldest row still kept; the group's older rows go (ids are monotonic, see the model)
            cutoff = await self.session.scalar(
                select(ClientErrorReport.id)
                .where(in_group)
                .order_by(ClientErrorReport.id.desc())
                .offset(kept - 1)
                .limit(1)
            )
            if cutoff is not None:
                await self.session.execute(delete(ClientErrorReport).where(in_group, ClientErrorReport.id < cutoff))
        return len(reports)

    async def list_recent(self, limit: int, *, kinds: Collection[str] | None = None) -> list[ClientErrorReport]:
        """The newest ``limit`` reports (of ``kinds`` when given), newest first."""
        query = select(ClientErrorReport)
        if kinds:
            query = query.where(ClientErrorReport.kind.in_(sorted(kinds)))
        result = await self.session.execute(query.order_by(ClientErrorReport.id.desc()).limit(limit))
        return list(result.scalars())

    async def clear(self, *, kinds: Collection[str] | None = None, min_id: int | None = None) -> int:
        """Delete every report, or only those of ``kinds`` and / or with an id of at least ``min_id``; returns how
        many were deleted."""
        query = delete(ClientErrorReport)
        if kinds:
            query = query.where(ClientErrorReport.kind.in_(sorted(kinds)))
        if min_id is not None:
            query = query.where(ClientErrorReport.id >= min_id)
        return rowcount(await self.session.execute(query))
