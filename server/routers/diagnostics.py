"""YouReel: client error reports. The studio's browser UI reports the errors a member hits (error toasts, uncaught
exceptions, failed requests); they are kept in the studio's database and listed in Settings → Options → Diagnostics,
for us to see what went wrong where. Performance records share the table (kinds ``perf`` / ``jank`` from the page,
``server_slow`` from the server's own timing middleware, server/perf_timing.py), with their own cap so they never push
the errors out; their numbers live in ``detail`` (``metrics``, ``resource_groups``, ``top_resources``).

Nothing here trusts the page with secrets: query strings and fragments are stripped from every path and URL before
storing (a token in a URL never lands in the table), and every text is trimmed to a fixed size.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lib.db import get_async_session
from lib.db.models.client_error_report import (
    CLIENT_ERROR_REPORTS_KEPT,
    ERROR_KINDS,
    PERF_KINDS,
    PERF_REPORTS_KEPT,
    ClientErrorReport,
)
from lib.db.repositories.client_error_repo import ClientErrorReportRepository, NewClientErrorReport

router = APIRouter()

# Stored sizes; longer texts are cut (the request limits below are only there to refuse absurd bodies)
MESSAGE_MAX = 2000
STACK_MAX = 4000
USER_AGENT_MAX = 512
BATCH_MAX = 20

METRICS_MAX = 48
RESOURCE_GROUPS_MAX = 24
TOP_RESOURCES_MAX = 10

# What the page may report; ``server_slow`` is written by the server only (server/perf_timing.py), never posted
ClientErrorKind = Literal["toast", "exception", "request", "perf", "jank"]
_ALL_KINDS = ERROR_KINDS | PERF_KINDS
_METRIC_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_LABEL = re.compile(r"^[A-Za-z0-9_.:+/-]{1,32}$")


class PerfResourceGroup(BaseModel):
    """The page's resources of one type (the browser's ``initiatorType``: script, link, img, fetch, …)."""

    model_config = ConfigDict(extra="ignore")

    type: str = Field(..., max_length=32)
    count: int = Field(0, ge=0)
    transfer_bytes: float = Field(0, ge=0, description="Bytes over the network (0 when served from the cache)")
    decoded_bytes: float = Field(0, ge=0, description="Bytes after decoding (the body's size)")


class PerfResource(BaseModel):
    """One heavy resource: its path without query string, type, sizes and how long it took."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(..., max_length=2048)
    type: str | None = Field(None, max_length=32)
    transfer_bytes: float = Field(0, ge=0)
    decoded_bytes: float = Field(0, ge=0)
    duration_ms: float = Field(0, ge=0)


class ClientErrorDetail(BaseModel):
    """What helps to find the cause. Unknown fields are dropped, not refused: an older backend keeps the rest."""

    model_config = ConfigDict(extra="ignore")

    page: str | None = Field(None, max_length=2048, description="The page's path (no query string)")
    method: str | None = Field(None, max_length=16, description="HTTP method of a failed request")
    api_path: str | None = Field(None, max_length=2048, description="API path of a failed request (no query string)")
    status: int | None = Field(None, ge=0, le=999, description="HTTP status of a failed request; none for a network error")
    source: str | None = Field(None, max_length=200, description="Component / action label, or the script file")
    stack: str | None = Field(None, max_length=20_000, description="Stack trace, trimmed")
    # Performance records (perf / jank / server_slow)
    tag: str | None = Field(None, max_length=64, description="What the record is about: load, pack_open, api, …")
    client: str | None = Field(None, max_length=64, description="server_slow: the request's X-Client (webui default)")
    metrics: dict[str, Any] | None = Field(
        None, description="Numbers by name (``*_ms`` durations, ``*_bytes`` sizes, counts); non-numbers are dropped"
    )
    resource_groups: list[PerfResourceGroup] | None = Field(None, max_length=200)
    top_resources: list[PerfResource] | None = Field(None, max_length=200)


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
    kept: int = Field(..., description="How many error reports the studio keeps (older ones are pruned)")
    kept_perf: int = Field(..., description="How many performance records (perf / jank / server_slow) it keeps")


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


def _label(value: str | None) -> str | None:
    """A short machine label (tag, client), or nothing when it has other characters."""
    return value if value and _LABEL.match(value) else None


def _stored_metrics(metrics: dict[str, Any] | None) -> dict[str, float] | None:
    """Only finite numbers under plain snake_case names, at most METRICS_MAX of them: nothing else (no text, so no
    names or URLs) can land in a metric."""
    if not metrics:
        return None
    kept: dict[str, float] = {}
    for key, value in metrics.items():
        if len(kept) >= METRICS_MAX:
            break
        if isinstance(value, bool) or not isinstance(value, int | float) or not _METRIC_KEY.match(key):
            continue
        if math.isfinite(value):
            kept[key] = round(value, 1) if isinstance(value, float) else value
    return kept or None


def _resource_path(path: str) -> str:
    return _trim(strip_path_query(path), 512) or ""


def _stored_groups(groups: list[PerfResourceGroup] | None) -> list[dict[str, Any]] | None:
    if not groups:
        return None
    stored = [
        {
            "type": _label(group.type) or "other",
            "count": group.count,
            "transfer_bytes": round(group.transfer_bytes),
            "decoded_bytes": round(group.decoded_bytes),
        }
        for group in groups[:RESOURCE_GROUPS_MAX]
    ]
    return stored or None


def _stored_resources(resources: list[PerfResource] | None) -> list[dict[str, Any]] | None:
    if not resources:
        return None
    stored = [
        {
            "path": _resource_path(resource.path),
            "type": _label(resource.type),
            "transfer_bytes": round(resource.transfer_bytes),
            "decoded_bytes": round(resource.decoded_bytes),
            "duration_ms": round(resource.duration_ms, 1),
        }
        for resource in resources[:TOP_RESOURCES_MAX]
    ]
    return stored or None


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
        "tag": _label(detail.tag),
        "client": _label(detail.client),
        "metrics": _stored_metrics(detail.metrics),
        "resource_groups": _stored_groups(detail.resource_groups),
        "top_resources": _stored_resources(detail.top_resources),
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


def _kinds(kind: list[str] | None) -> set[str] | None:
    """The kinds asked for (repeated ``kind`` or comma-separated); unknown names are ignored, none = all."""
    if not kind:
        return None
    asked = {name.strip() for value in kind for name in value.split(",")}
    known = asked & _ALL_KINDS
    # Only unknown kinds asked for: match nothing rather than everything
    return known or {"-"}


@router.get("/diagnostics/client-errors", response_model=ClientErrorList)
async def list_client_errors(
    limit: int = Query(200, ge=1, le=CLIENT_ERROR_REPORTS_KEPT + PERF_REPORTS_KEPT),
    kind: list[str] | None = Query(None, description="Only these kinds (repeat or comma-separate); default all"),
    session: AsyncSession = Depends(get_async_session),
) -> ClientErrorList:
    """The newest reports, newest first."""
    rows = await ClientErrorReportRepository(session).list_recent(limit, kinds=_kinds(kind))
    return ClientErrorList(
        entries=[_to_out(row) for row in rows], kept=CLIENT_ERROR_REPORTS_KEPT, kept_perf=PERF_REPORTS_KEPT
    )


@router.delete("/diagnostics/client-errors", response_model=ClientErrorsCleared)
async def clear_client_errors(
    kind: list[str] | None = Query(None, description="Only these kinds (repeat or comma-separate); default all"),
    min_id: int | None = Query(None, ge=0, description="Only reports with this id or newer"),
    session: AsyncSession = Depends(get_async_session),
) -> ClientErrorsCleared:
    """Delete every report, or only those of the given kinds and / or from ``min_id`` on."""
    deleted = await ClientErrorReportRepository(session).clear(kinds=_kinds(kind), min_id=min_id)
    await session.commit()
    return ClientErrorsCleared(deleted=deleted)
