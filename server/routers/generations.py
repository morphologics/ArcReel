"""Generation records over REST (API G16): every generation attempt with its inputs, provider calls and result.

A record is one media generation task (image, video, audio) of the member's studio, whatever became of it: succeeded,
failed, cancelled, refused before reaching a provider, or still queued / running. It joins the task (target, status,
failure, client, times, the request snapshot taken at enqueue), the usage ledger calls that served it (prompt sent,
options, reference paths, cost, duration) and the version it committed. Shapes: ``lib.generation.generation_records``.

``GET /generations`` lists records newest first with keyset paging and filters, across projects or for one;
``updated_since`` returns only records whose task changed at or after that moment, with ``server_time`` for the next
call (API G24). ``GET /generations/{task_id}`` reads one record in full. Prompts (the editable prompt at enqueue, the
request's prompt and each call's sent prompt) are in list items only with ``include=prompts``; the single read always
has them.

生成记录的 REST 接口（API G16）：每次生成尝试连同其输入、供应商调用与结果。

一条记录就是成员工作室里的一个媒体生成任务（图片、视频、音频），无论结局如何：成功、失败、取消、在到达供应商之前被拒，
或仍在排队 / 执行。它把任务（目标、状态、失败、客户端、时间、入队时取下的请求快照）、为它服务的用量账本调用（发出的提示词、
选项、参考路径、费用、耗时）与它提交的版本拼在一起。形状见 ``lib.generation.generation_records``。

``GET /generations`` 按新的在前列出记录，keyset 分页并可筛选，可跨项目也可限于一个项目；``updated_since`` 只返回任务在
该时刻或之后变化过的记录，并给出下一次用的 ``server_time``（API G24）。``GET /generations/{task_id}`` 读取一条完整记录。
提示词（入队时的可编辑提示词、请求自带的提示词、每次调用发出的提示词）只在 ``include=prompts`` 时出现在列表项里；单条读取
总是带上。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query

from lib.db import async_session_factory
from lib.db.repositories.generation_record_repo import (
    GenerationCursor,
    GenerationCursorError,
    GenerationFilters,
    GenerationRecordRepository,
)
from lib.generation.generation_records import (
    GenerationRecord,
    GenerationRecordPage,
    build_generation_record,
    load_result_versions,
)
from lib.generation.request_snapshot import RESOURCE_TYPE_BY_TASK_TYPE
from lib.i18n.display_names import DisplayNames
from lib.infra.api_errors import NotFoundError, UnprocessableError
from lib.project.project_manager import get_project_manager
from server.i18n import DisplayNamesCatalog, Translator
from server.routers.tasks import _localize_task
from server.routers.usage import utc_query_instant

router = APIRouter()

_RESOURCE_TYPES = "|".join(sorted(set(RESOURCE_TYPE_BY_TASK_TYPE.values())))
_INCLUDE_VALUES = frozenset({"prompts"})


def _multi(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _error(task: Mapping[str, Any], translate: Callable[..., str], names: DisplayNames) -> dict[str, Any]:
    """The task's failure as code, params and text in the request's language (same rendering as ``GET /tasks``).

    任务失败的码、参数与请求语言的文本（与 ``GET /tasks`` 同一渲染）。"""
    if not task.get("error_message"):
        return {}
    localized = _localize_task(
        {"error_message": task["error_message"], "result": task.get("result") or {}}, translate, names
    )
    return {
        "error_code": localized.get("error_code"),
        "error_params": localized.get("error_params"),
        "error_message": localized.get("error_message"),
    }


async def _records(
    tasks: Sequence[dict[str, Any]],
    calls: Mapping[str, list[dict[str, Any]]],
    *,
    include_prompts: bool,
    translate: Callable[..., str],
    names: DisplayNames,
) -> list[GenerationRecord]:
    # Result versions: one versions.json read per project on the page, off the event loop.
    # 结果版本：本页每个项目读一次 versions.json，不占事件循环。
    versions = await asyncio.to_thread(load_result_versions, tasks, get_project_manager().get_project_path)
    records: list[GenerationRecord] = []
    for task in tasks:
        result = task.get("result")
        version = result.get("version") if isinstance(result, dict) else None
        records.append(
            build_generation_record(
                task,
                calls.get(task["task_id"], []),
                version_record=versions.get((task["project_name"], task["task_id"], version)),
                error=_error(task, translate, names),
                include_prompts=include_prompts,
            )
        )
    return records


@router.get("/generations", response_model=GenerationRecordPage, response_model_by_alias=True)
async def list_generations(
    _t: Translator,
    names: DisplayNamesCatalog,
    project: str | None = Query(None, description="Project name; all projects when absent"),
    episode: int | None = Query(None, ge=1, description="Targets on this episode's script (episode_N.json)"),
    resource_type: str | None = Query(
        None,
        pattern=f"^({_RESOURCE_TYPES})$",
        description="Target's version-history resource type (storyboards, videos, characters, …)",
    ),
    resource_id: str | None = Query(None, description="Target id (shot / unit id, asset name, owner/derivative)"),
    task_type: str | None = Query(None, description="Task type; comma-separated for multiple"),
    batch_id: str | None = Query(None, description="Member tasks of this generation batch"),
    status: str | None = Query(
        None, description="Task status (queued, running, succeeded, failed, cancelled); comma-separated"
    ),
    client: str | None = Query(None, description="Client label that created the task; comma-separated"),
    since: datetime | None = Query(None, description="Queued at or after (ISO 8601, UTC if no time zone)"),
    updated_since: datetime | None = Query(
        None,
        description=(
            "Incremental sync: only records whose task changed at or after this moment (inclusive, dedupe by "
            "task_id). Pass the previous answer's server_time"
        ),
    ),
    include: str | None = Query(None, description="Comma-separated extras: prompts (editable, request, sent)"),
    cursor: str | None = Query(None, description="Opaque cursor returned by the previous page"),
    limit: int = Query(50, ge=1, le=200),
) -> GenerationRecordPage:
    """Generation records, newest first (``queued_at``), cursor-paged. Records whose attempt never reached a provider
    (refused, cancelled while queued) are included with their inputs and no calls.

    生成记录，按入队时间新的在前，游标分页。从未到达供应商的尝试（被拒、排队中取消）也在内，带输入、没有调用。"""
    extras = set(_multi(include))
    if extras - _INCLUDE_VALUES:
        raise UnprocessableError("generation_include_invalid", values=", ".join(sorted(extras - _INCLUDE_VALUES)))
    try:
        decoded = GenerationCursor.decode(cursor) if cursor else None
    except GenerationCursorError as exc:
        raise UnprocessableError("generation_cursor_invalid") from exc
    include_prompts = "prompts" in extras
    # Taken before the read, so a change committed while it runs is in the next window.
    # 在读取之前取，读取期间提交的变化落在下一个窗口里。
    server_time = datetime.now(UTC).isoformat()
    filters = GenerationFilters(
        project_name=project,
        episode=episode,
        resource_type=resource_type,
        resource_id=resource_id,
        batch_id=batch_id or None,
        task_types=_multi(task_type),
        statuses=_multi(status),
        clients=_multi(client),
        since=utc_query_instant(since),
        updated_since=utc_query_instant(updated_since),
    )
    async with async_session_factory() as session:
        repo = GenerationRecordRepository(session)
        tasks, next_cursor = await repo.list_tasks(filters, cursor=decoded, limit=limit)
        calls = await repo.calls_for_tasks([task["task_id"] for task in tasks], include_prompts=include_prompts)
    items = await _records(tasks, calls, include_prompts=include_prompts, translate=_t, names=names)
    return GenerationRecordPage(
        items=items,
        next_cursor=next_cursor.encode() if next_cursor is not None else None,
        server_time=server_time,
    )


@router.get("/generations/{task_id}", response_model=GenerationRecord, response_model_by_alias=True)
async def get_generation(task_id: str, _t: Translator, names: DisplayNamesCatalog) -> GenerationRecord:
    """One generation record in full (prompts included). 404 ``generation_record_not_found`` when there is no media
    generation task with this id.

    一条完整的生成记录（含提示词）。没有该 id 的媒体生成任务时 404 ``generation_record_not_found``。"""
    async with async_session_factory() as session:
        repo = GenerationRecordRepository(session)
        task = await repo.get_task(task_id)
        if task is None:
            raise NotFoundError("generation_record_not_found", id=task_id)
        calls = await repo.calls_for_tasks([task_id], include_prompts=True)
    [record] = await _records([task], calls, include_prompts=True, translate=_t, names=names)
    return record
