"""YouReel: client error reports. The studio's browser UI reports the errors a member hits (error toasts, uncaught
exceptions, failed requests); they are kept in the studio's database and listed in Settings → Options → Diagnostics,
for us to see what went wrong where.

Nothing here trusts the page with secrets: query strings and fragments are stripped from every path and URL before
storing (a token in a URL never lands in the table), and every text is trimmed to a fixed size.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db import get_async_session
from lib.db.models.client_error_report import CLIENT_ERROR_REPORTS_KEPT, ClientErrorReport
from lib.db.repositories.client_error_repo import ClientErrorReportRepository, NewClientErrorReport

router = APIRouter()

# Stored sizes; longer texts are cut (the request limits below are only there to refuse absurd bodies)
MESSAGE_MAX = 2000
STACK_MAX = 4000
USER_AGENT_MAX = 512
BATCH_MAX = 20

ClientErrorKind = Literal["toast", "exception", "request"]


class ClientErrorDetail(BaseModel):
    """What helps to find the cause. Unknown fields are dropped, not refused: an older backend keeps the rest."""

    model_config = ConfigDict(extra="ignore")

    page: str | None = Field(None, max_length=2048, description="The page's path (no query string)")
    method: str | None = Field(None, max_length=16, description="HTTP method of a failed request")
    api_path: str | None = Field(None, max_length=2048, description="API path of a failed request (no query string)")
    status: int | None = Field(None, ge=0, le=999, description="HTTP status of a failed request; none for a network error")
    source: str | None = Field(None, max_length=200, description="Component / action label, or the script file")
    stack: str | None = Field(None, max_length=20_000, description="Stack trace, trimmed")


class ClientErrorIn(BaseModel):
    kind: ClientErrorKind
    message: str = Field(..., min_length=1, max_length=10_000)
    detail: ClientErrorDetail | None = None


class ClientErrorBatch(BaseModel):
    entries: list[ClientErrorIn] = Field(..., min_length=1, max_length=BATCH_MAX)
    app_build: str | None = Field(None, max_length=128, description="Frontend build id, when the page knows it")


class ClientErrorAccepted(BaseModel):
    accepted: int


class ClientErrorReportOut(BaseModel):
    id: int
    created_at: str = Field(..., description="UTC, ISO 8601")
    kind: str
    message: str
    detail: ClientErrorDetail | None
    user_agent: str | None
    app_build: str | None


class ClientErrorList(BaseModel):
    entries: list[ClientErrorReportOut]
    kept: int = Field(..., description="How many reports the studio keeps (older ones are pruned)")


class ClientErrorsCleared(BaseModel):
    deleted: int


# An absolute URL in free text (a message, a stack line): its query string / fragment goes, a trailing ":line:col"
# (stack frames, e.g. Vite's "x.tsx?t=123:10:5") stays
_URL_QUERY = re.compile(r"(https?://[^\s?#\"'()<>]*)[?#][^\s\"'()<>]*?(?=(?::\d+){1,2}(?:[\s)\"'<>]|$)|[\s\"'()<>]|$)")


def strip_path_query(path: str | None) -> str | None:
    """A path or URL without its query string and fragment."""
    if path is None:
        return None
    return re.split(r"[?#]", path, maxsplit=1)[0]


def strip_text_queries(text: str) -> str:
    """Free text with the query string / fragment of every absolute URL in it removed."""
    return _URL_QUERY.sub(r"\1", text)


def _trim(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stored_detail(detail: ClientErrorDetail | None) -> dict[str, Any] | None:
    if detail is None:
        return None
    stored = {
        "page": strip_path_query(detail.page),
        "method": detail.method.upper() if detail.method else None,
        "api_path": strip_path_query(detail.api_path),
        "status": detail.status,
        "source": _trim(strip_text_queries(detail.source), 200) if detail.source else None,
        "stack": _trim(strip_text_queries(detail.stack), STACK_MAX) if detail.stack else None,
    }
    kept = {key: value for key, value in stored.items() if value is not None}
    return kept or None


def _iso_utc(value: datetime) -> str:
    # SQLite hands back naive datetimes; they were written as UTC
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_out(row: ClientErrorReport) -> ClientErrorReportOut:
    return ClientErrorReportOut(
        id=row.id,
        created_at=_iso_utc(row.created_at),
        kind=row.kind,
        message=row.message,
        detail=ClientErrorDetail.model_validate(row.detail) if row.detail else None,
        user_agent=row.user_agent,
        app_build=row.app_build,
    )


@router.post("/diagnostics/client-errors", response_model=ClientErrorAccepted)
async def report_client_errors(
    body: ClientErrorBatch,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> ClientErrorAccepted:
    """Store a batch of errors the page hit (at most 20); the user agent is taken from the request."""
    user_agent = _trim(request.headers.get("user-agent") or None, USER_AGENT_MAX)
    app_build = body.app_build or None
    reports = [
        NewClientErrorReport(
            kind=entry.kind,
            message=_trim(strip_text_queries(entry.message.strip()) or entry.kind, MESSAGE_MAX) or entry.kind,
            detail=_stored_detail(entry.detail),
            user_agent=user_agent,
            app_build=app_build,
        )
        for entry in body.entries
    ]
    accepted = await ClientErrorReportRepository(session).add_many(reports)
    await session.commit()
    return ClientErrorAccepted(accepted=accepted)


@router.get("/diagnostics/client-errors", response_model=ClientErrorList)
async def list_client_errors(
    limit: int = Query(200, ge=1, le=CLIENT_ERROR_REPORTS_KEPT),
    session: AsyncSession = Depends(get_async_session),
) -> ClientErrorList:
    """The newest reports, newest first."""
    rows = await ClientErrorReportRepository(session).list_recent(limit)
    return ClientErrorList(entries=[_to_out(row) for row in rows], kept=CLIENT_ERROR_REPORTS_KEPT)


@router.delete("/diagnostics/client-errors", response_model=ClientErrorsCleared)
async def clear_client_errors(session: AsyncSession = Depends(get_async_session)) -> ClientErrorsCleared:
    """Delete every report."""
    deleted = await ClientErrorReportRepository(session).clear()
    await session.commit()
    return ClientErrorsCleared(deleted=deleted)
