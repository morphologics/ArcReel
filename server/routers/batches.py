"""Generation batches over REST: read a batch, preview and cancel its queued members.

Every paid batch submission (storyboard images, shot videos, asset sheets, reference video units, and the agent / MCP
generation tools) returns a ``batch_id``. These routes read and cancel such a batch by that id alone; the batch's
project is looked up. The shapes are the ones the MCP tools ``get_generation_batch`` / ``cancel_generation_batch``
return, with the batch's client label and its actual spend added to the read.

Cancelling follows the task rule (``GLOSSARY.md``「取消」): only queued members are cancelled; members already running
finish as usual and their results become artifacts as usual (``skipped_running``); finished members are left alone
(``skipped_terminal``).

生成批次的 REST 接口：读取批次，预览并取消其排队中的成员。

每次付费批量提交（分镜图、分镜视频、资产图、参考视频单元，以及 Agent / MCP 的生成工具）都返回 ``batch_id``。
这些路由只凭该 id 读取与取消批次，批次所属项目由服务端查出。形状与 MCP 工具 ``get_generation_batch`` /
``cancel_generation_batch`` 的返回相同，读取时另加批次的客户端标签与实际花费。

取消遵循任务的规则（``GLOSSARY.md``「取消」）：只取消排队中的成员；已在执行的成员照常跑完、结果照常成为产物
（``skipped_running``）；已结束的成员不动（``skipped_terminal``）。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from lib.db import async_session_factory
from lib.db.repositories.usage_repo import UsageRepository
from lib.generation.generation_batch import GenerationBatchCancelResult, GenerationBatchReadModel
from lib.generation.generation_queue import GenerationBatchNotFound, get_generation_queue
from lib.infra.api_errors import NotFoundError
from lib.project.project_manager import get_project_manager
from server.auth import CurrentUser
from server.tool_runtime import batch_currency_resolver

router = APIRouter()


class GenerationBatchCost(BaseModel):
    """Actual spend of the batch's member tasks, from the usage records (same rules as the usage summary).

    批次成员任务的实际花费，取自使用记录（口径与使用汇总相同）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Settled provider calls. / 已结算的供应商调用数。
    calls: int
    #: Priced spend per currency; calls costing 0 or unpriced are not in it.
    #: 按币种合计的已定价花费；费用为 0 或未定价的调用不计入。
    cost: dict[str, float] = Field(default_factory=dict)
    #: Settled calls without a price. / 未定价的已结算调用数。
    unpriced_calls: int
    #: Calls still in flight, which carry no cost yet. / 仍在进行、尚无费用的调用数。
    pending_calls: int


class GenerationBatchResponse(GenerationBatchReadModel):
    """A generation batch: members with their task ids and statuses, counts by status, whether it is done, the final
    per-unit result once done, plus the client that submitted it and its actual spend so far.

    一个生成批次：成员及其任务 id 与状态、按状态的计数、是否结束、结束后的逐单元终态结果，另有提交它的客户端与
    目前的实际花费。"""

    #: Client label that submitted the batch (``webui``, ``mcp``, ``embedded``, or the request's ``X-Client``).
    #: 提交该批次的客户端标签（``webui``、``mcp``、``embedded``，或请求的 ``X-Client``）。
    source: str
    cost: GenerationBatchCost


async def _batch_project(batch_id: str, user_id: str) -> tuple[str, str]:
    found = await get_generation_queue().find_generation_batch(batch_id=batch_id, user_id=user_id)
    if found is None:
        raise NotFoundError("generation_batch_not_found", id=batch_id)
    return found["project_name"], found["source"]


@router.get("/batches/{batch_id}", response_model=GenerationBatchResponse)
async def get_generation_batch(batch_id: str, user: CurrentUser) -> GenerationBatchResponse:
    """Read a generation batch: members, counts by status, ``done``, ``poll_after_seconds`` while running, the final
    ``generation_result`` once done, the submitting client and the actual spend.

    读取一个生成批次：成员、按状态计数、``done``、进行中时的 ``poll_after_seconds``、结束后的
    ``generation_result``、提交它的客户端与实际花费。"""
    project_name, source = await _batch_project(batch_id, user.id)
    resolver = await asyncio.to_thread(batch_currency_resolver, get_project_manager(), project_name)
    try:
        batch = await get_generation_queue().get_generation_batch(
            project_name=project_name, batch_id=batch_id, user_id=user.id, resolver=resolver
        )
    except GenerationBatchNotFound as exc:
        raise NotFoundError("generation_batch_not_found", id=batch_id) from exc
    async with async_session_factory() as session:
        cost = await UsageRepository(session).get_batch_cost(batch_id)
    return GenerationBatchResponse.model_validate(
        {**batch.model_dump(mode="json"), "source": source, "cost": GenerationBatchCost.model_validate(cost)}
    )


@router.get("/batches/{batch_id}/cancel-preview", response_model=GenerationBatchCancelResult)
async def preview_generation_batch_cancel(batch_id: str, user: CurrentUser) -> GenerationBatchCancelResult:
    """What cancelling would do now, without cancelling: queued members under ``cancelled``, running ones under
    ``skipped_running``, finished ones under ``skipped_terminal`` (task ids). States can change before the cancel.

    此刻取消会做什么，但不取消：排队中的成员列在 ``cancelled``，执行中的在 ``skipped_running``，已结束的在
    ``skipped_terminal``（任务 id）。真正取消前状态可能变化。"""
    project_name, _source = await _batch_project(batch_id, user.id)
    try:
        return await get_generation_queue().preview_generation_batch_cancel(
            project_name=project_name, batch_id=batch_id, user_id=user.id
        )
    except GenerationBatchNotFound as exc:
        raise NotFoundError("generation_batch_not_found", id=batch_id) from exc


@router.post("/batches/{batch_id}/cancel", response_model=GenerationBatchCancelResult)
async def cancel_generation_batch(batch_id: str, user: CurrentUser) -> GenerationBatchCancelResult:
    """Cancel the batch's queued members (task ids in ``cancelled``); running members run to completion
    (``skipped_running``), finished ones are left alone (``skipped_terminal``). Same semantics as the MCP tool.

    取消批次中排队的成员（任务 id 列在 ``cancelled``）；执行中的成员照常跑完（``skipped_running``），已结束的不动
    （``skipped_terminal``）。语义与 MCP 工具相同。"""
    project_name, _source = await _batch_project(batch_id, user.id)
    try:
        return await get_generation_queue().cancel_generation_batch(
            project_name=project_name, batch_id=batch_id, user_id=user.id
        )
    except GenerationBatchNotFound as exc:
        raise NotFoundError("generation_batch_not_found", id=batch_id) from exc
