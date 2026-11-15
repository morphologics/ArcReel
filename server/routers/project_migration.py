"""Web entry point for project data upgrades: retry the upgrade, acknowledge the upgrade report.

Retry calls the same service command as the Agent's ``retry_project_migration``; the route only maps the result to
HTTP. When an upgrade fails, the project's other write entry points are all blocked and retrying is exactly how the
block is lifted, so ``require_project_migration_ok`` is not attached; acknowledging the report only records that the
user has seen it, so it is not attached either.

项目数据升级的 Web 入口：重试升级，确认升级报告。

重试与 Agent 的 ``retry_project_migration`` 调用同一个服务命令，路由只把结果映射成 HTTP。升级失败时项目的
其余写入入口都被阻断，重试正是解除阻断的途径，所以不挂 ``require_project_migration_ok``；确认报告只记下
用户已看过，同样不挂。
"""

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from lib.infra.api_errors import NotFoundError, UnprocessableError
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_change_hints import emit_project_change_hint, project_change_source
from lib.project.project_manager import ProjectManager, get_project_manager
from lib.project.project_migration_report import acknowledge_migration_report
from server.auth import CurrentUser
from server.tool_runtime import (
    CallerContext,
    NoArguments,
    ProjectScope,
    Services,
    ToolRequest,
    retry_project_migration,
)

router = APIRouter()


@router.post("/projects/{name}/migration/retry")
async def retry_migration(name: str, user: CurrentUser):
    """重跑本项目的数据升级链（含产物补录）；已是最新版本时直接成功。

    升级仍失败时 422，诊断里的 ``reason`` 是失败原文，``details`` 是结构化明细（集 / 文件 / 违约）。
    """
    pm = get_project_manager()
    if not await asyncio.to_thread(pm.project_exists, name):
        raise NotFoundError("project_not_found", name=name)
    with project_change_source("webui"):
        try:
            outcome = await retry_project_migration(
                ToolRequest(NoArguments()),
                ProjectScope(project_name=name, data_root=pm.data_root),
                CallerContext(user_id=user.id, source="webui"),
                Services.defaults(pm),
            )
        finally:
            # The upgrade chain rewrites project.json and scripts directly without sending hints; send them here, so
            # changes from the upgrade are broadcast as webui instead of being found by timed polling and broadcast as
            # filesystem.
            # 升级链直接改写 project.json 与剧本，不发 hint；在这里补发，升级带来的变更按 webui 广播，
            # 而不是等定时轮询发现、按 filesystem 广播。
            emit_project_change_hint(name)
    if outcome.problem is not None:
        params = outcome.problem.params or {}
        raise UnprocessableError("project_migration_retry_failed").with_diagnostic(
            {"reason": outcome.problem.detail, "details": params.get("details", [])}
        )
    return {"success": True}


class AcknowledgeReportRequest(BaseModel):
    #: Generation time of the report the user saw. If the report on disk has been replaced by another one (upgraded
    #: again since), do not acknowledge, and the new report is shown as usual.
    #: 用户看过的那份报告的生成时间。盘上已换成另一份报告（之后又升级过）时不确认，新报告照常提示。
    migrated_at: str


@router.post("/projects/{name}/migration-report/acknowledge")
async def acknowledge_report(name: str, body: AcknowledgeReportRequest, _user: CurrentUser):
    """记下用户已看过上一次升级的报告：项目页不再提示。没有待确认的报告时同样成功。

    ``acknowledged`` 为假表示盘上没有这份待确认的报告：要么已确认过，要么已换成另一份。
    """
    with project_change_source("webui"):
        acknowledged = await run_sync_transaction(_acknowledge, get_project_manager(), name, body.migrated_at)
    return {"success": True, "acknowledged": acknowledged}


def _acknowledge(pm: ProjectManager, name: str, migrated_at: str) -> bool:
    if not pm.project_exists(name):
        raise NotFoundError("project_not_found", name=name)
    return acknowledge_migration_report(pm.get_project_path(name), migrated_at=migrated_at)
