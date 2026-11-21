"""Reads behind generation records (API G16): media generation tasks with their ledger calls.

A generation record is one attempt: one row of ``tasks`` whose media type is image, video or audio (text and render
tasks are not generation attempts), joined with the ``api_calls`` rows that served it (none when the attempt was
refused or cancelled before reaching a provider; more than one when a call was retried). The list is keyset-paged
newest first by ``(queued_at, task_id)`` and only touches the indexes on ``tasks`` (``idx_tasks_project_queued_at``,
``idx_tasks_updated_at``) and ``idx_api_calls_task_id`` for the page's calls: no project file is read here.

生成记录（API G16）背后的读取：媒体生成任务及其账本调用。

一条生成记录就是一次尝试：``tasks`` 里媒体类型为图片、视频或音频的一行（文本与渲染任务不算生成尝试），连同为它服务的
``api_calls`` 行（尝试在到达供应商之前被拒绝或取消时没有；调用重试过时不止一条）。列表按 ``(queued_at, task_id)`` 新的在前
keyset 分页，只用 ``tasks`` 上的索引（``idx_tasks_project_queued_at``、``idx_tasks_updated_at``）与本页调用的
``idx_api_calls_task_id``：这里不读任何项目文件。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select

from lib.db.models.api_call import ApiCall
from lib.db.models.task import BatchTask, Task
from lib.db.repositories.base import BaseRepository
from lib.episode.episode_paths import episode_script_filename
from lib.generation.request_snapshot import (
    GENERATION_MEDIA_TYPES,
    IMAGE_EDIT_TASK_TYPE,
    task_types_for_resource_type,
)


class GenerationCursorError(ValueError):
    """The cursor cannot be decoded. / 游标无法解码。"""


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    """UTC ISO string (SQLite returns naive moments that are UTC). / UTC ISO 串（SQLite 取回的 naive 时刻即 UTC）。"""
    return _as_utc(value).isoformat() if value is not None else None


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class GenerationCursor:
    """Keyset position: ``(queued_at, task_id)`` of the previous page's last record; opaque to clients.

    keyset 位置：上一页最后一条记录的 ``(queued_at, task_id)``；对客户端不透明。"""

    queued_at: datetime
    task_id: str

    def encode(self) -> str:
        payload = json.dumps({"queued_at": _as_utc(self.queued_at).isoformat(), "task_id": self.task_id})
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")

    @classmethod
    def decode(cls, raw: str) -> GenerationCursor:
        try:
            payload = json.loads(base64.b64decode(raw.encode("ascii"), altchars=b"-_", validate=True))
            queued_at = payload["queued_at"]
            task_id = payload["task_id"]
            if not isinstance(queued_at, str) or not isinstance(task_id, str) or not task_id:
                raise ValueError("Cursor field has an invalid type")
            return cls(queued_at=_as_utc(datetime.fromisoformat(queued_at)), task_id=task_id)
        except (binascii.Error, LookupError, OverflowError, TypeError, UnicodeError, ValueError) as exc:
            raise GenerationCursorError(f"Undecodable cursor: {raw!r}") from exc


@dataclass(frozen=True)
class GenerationFilters:
    """Filters of the generation record list; empty tuples / ``None`` do not filter.

    生成记录列表的筛选；空元组 / ``None`` 表示不筛。"""

    project_name: str | None = None
    #: Tasks on ``episode_N.json`` (asset sheets have no script and never match). / ``episode_N.json`` 上的任务。
    episode: int | None = None
    #: Version-history resource type of the target (``storyboards``, ``videos``, ``characters``, …). / 目标的资源类型。
    resource_type: str | None = None
    resource_id: str | None = None
    #: Member tasks of this batch (``batch_tasks``: a deduplicated member counts for every batch that adopted it).
    batch_id: str | None = None
    task_types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    #: Client labels (``tasks.source``). / 客户端标签（``tasks.source``）。
    clients: tuple[str, ...] = ()
    #: Queued at or after. / 入队时间不早于。
    since: datetime | None = None
    #: Changed (``tasks.updated_at``) at or after (API G24). / 变化时间不早于（API G24）。
    updated_since: datetime | None = None


_TASK_COLUMNS = (
    Task.task_id,
    Task.batch_id,
    Task.project_name,
    Task.task_type,
    Task.media_type,
    Task.resource_id,
    Task.resource_type,
    Task.script_file,
    Task.payload_json,
    Task.request_snapshot_json,
    Task.status,
    Task.result_json,
    Task.error_message,
    Task.source,
    Task.cancelled_by,
    Task.provider_id,
    Task.queued_at,
    Task.started_at,
    Task.finished_at,
    Task.updated_at,
)

_CALL_COLUMNS = (
    ApiCall.id,
    ApiCall.task_id,
    ApiCall.call_type,
    ApiCall.provider,
    ApiCall.model,
    ApiCall.status,
    ApiCall.resolution,
    ApiCall.duration_seconds,
    ApiCall.aspect_ratio,
    ApiCall.generate_audio,
    ApiCall.inputs,
    ApiCall.output_path,
    ApiCall.error_code,
    ApiCall.error_params,
    ApiCall.cost_amount,
    ApiCall.currency,
    ApiCall.unpriced_reason,
    ApiCall.started_at,
    ApiCall.finished_at,
    ApiCall.duration_ms,
    ApiCall.client,
)


def _task_row(row: Any) -> dict[str, Any]:
    return {
        "task_id": row.task_id,
        "batch_id": row.batch_id,
        "project_name": row.project_name,
        "task_type": row.task_type,
        "media_type": row.media_type,
        "resource_id": row.resource_id,
        "resource_type": row.resource_type,
        "script_file": row.script_file,
        "payload": _loads(row.payload_json) or {},
        "request_snapshot": _loads(row.request_snapshot_json),
        "status": row.status,
        "result": _loads(row.result_json),
        "error_message": row.error_message,
        "source": row.source,
        "cancelled_by": row.cancelled_by,
        "provider_id": row.provider_id,
        "queued_at": _iso(row.queued_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "updated_at": _iso(row.updated_at),
        "_queued_at": row.queued_at,
    }


class GenerationRecordRepository(BaseRepository):
    def _filter_clauses(self, filters: GenerationFilters) -> list[Any]:
        clauses: list[Any] = [Task.media_type.in_(GENERATION_MEDIA_TYPES)]
        if filters.project_name is not None:
            clauses.append(Task.project_name == filters.project_name)
        if filters.episode is not None:
            clauses.append(Task.script_file == episode_script_filename(filters.episode))
        if filters.resource_type is not None:
            kinds = task_types_for_resource_type(filters.resource_type)
            clauses.append(
                or_(
                    and_(Task.task_type != IMAGE_EDIT_TASK_TYPE, Task.task_type.in_(kinds)),
                    and_(Task.task_type == IMAGE_EDIT_TASK_TYPE, Task.resource_type.in_(kinds)),
                )
            )
        if filters.resource_id is not None:
            clauses.append(Task.resource_id == filters.resource_id)
        if filters.batch_id is not None:
            clauses.append(Task.task_id.in_(select(BatchTask.task_id).where(BatchTask.batch_id == filters.batch_id)))
        if filters.task_types:
            clauses.append(Task.task_type.in_(filters.task_types))
        if filters.statuses:
            clauses.append(Task.status.in_(filters.statuses))
        if filters.clients:
            clauses.append(Task.source.in_(filters.clients))
        if filters.since is not None:
            clauses.append(Task.queued_at >= _as_utc(filters.since))
        if filters.updated_since is not None:
            clauses.append(Task.updated_at >= _as_utc(filters.updated_since))
        return clauses

    async def list_tasks(
        self, filters: GenerationFilters, *, cursor: GenerationCursor | None, limit: int
    ) -> tuple[list[dict[str, Any]], GenerationCursor | None]:
        """One page of generation tasks, newest first, and the cursor of the next page (``None`` on the last).

        一页生成任务，新的在前，及下一页的游标（末页为 ``None``）。"""
        clauses = self._filter_clauses(filters)
        if cursor is not None:
            clauses.append(
                or_(
                    Task.queued_at < cursor.queued_at,
                    and_(Task.queued_at == cursor.queued_at, Task.task_id < cursor.task_id),
                )
            )
        stmt = (
            select(*_TASK_COLUMNS).where(*clauses).order_by(Task.queued_at.desc(), Task.task_id.desc()).limit(limit + 1)
        )
        stmt = self._scope_query(stmt, Task)
        rows = [_task_row(row) for row in (await self.session.execute(stmt)).all()]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = GenerationCursor(queued_at=last["_queued_at"], task_id=last["task_id"])
        return rows, next_cursor

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """The generation task ``task_id``; ``None`` when absent or not a media generation task.

        生成任务 ``task_id``；不存在或不是媒体生成任务时为 ``None``。"""
        stmt = select(*_TASK_COLUMNS).where(Task.task_id == task_id, Task.media_type.in_(GENERATION_MEDIA_TYPES))
        stmt = self._scope_query(stmt, Task)
        row = (await self.session.execute(stmt)).first()
        return _task_row(row) if row is not None else None

    async def calls_for_tasks(
        self, task_ids: Collection[str], *, include_prompts: bool
    ) -> dict[str, list[dict[str, Any]]]:
        """The ledger calls of these tasks, oldest first per task; the sent prompt only with ``include_prompts``.

        这些任务的账本调用，每个任务内旧的在前；``include_prompts`` 时才带发出的提示词。"""
        if not task_ids:
            return {}
        columns = (*_CALL_COLUMNS, ApiCall.prompt) if include_prompts else _CALL_COLUMNS
        stmt = (
            select(*columns)
            .where(ApiCall.task_id.in_(list(task_ids)))
            .order_by(ApiCall.started_at.asc(), ApiCall.id.asc())
        )
        stmt = self._scope_query(stmt, ApiCall)
        calls: dict[str, list[dict[str, Any]]] = {}
        for row in (await self.session.execute(stmt)).all():
            call = {
                "id": row.id,
                "media_type": row.call_type,
                "provider": row.provider,
                "model": row.model,
                "status": row.status,
                "resolution": row.resolution,
                "duration_seconds": row.duration_seconds,
                "aspect_ratio": row.aspect_ratio,
                "generate_audio": row.generate_audio,
                "inputs": row.inputs,
                "output_path": row.output_path,
                "error_code": row.error_code,
                "error_params": row.error_params,
                "cost_amount": row.cost_amount,
                "currency": row.currency,
                "unpriced_reason": row.unpriced_reason,
                "started_at": _iso(row.started_at),
                "finished_at": _iso(row.finished_at),
                "duration_ms": row.duration_ms,
                "client": row.client,
                "prompt": row.prompt if include_prompts else None,
            }
            calls.setdefault(row.task_id, []).append(call)
        return calls


__all__ = [
    "GenerationCursor",
    "GenerationCursorError",
    "GenerationFilters",
    "GenerationRecordRepository",
]
