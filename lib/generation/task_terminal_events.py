"""Task terminal state -> conversion to and publishing of project events.

Terminal transitions of the task queue (succeeded / failed / cancelled) are pushed over the existing project event
channel, so the frontend doesn't need polling to find out a task has ended. Events go through the batch bus of
``project_change_hints``, on the same SSE as other project changes (``/projects/{name}/events/stream``), with no new
connection.

These changes are **refresh signals**, not entity changes: ``important=False`` + ``focus=None``, so the frontend
shows no notification and triggers no focus jump, only refetches the task list and affected canvas data.

任务终态 → 项目事件的转换与发布。

任务队列的终态迁移（succeeded / failed / cancelled）复用既有的项目事件通道推送，
让前端不必靠轮询才发现任务结束。事件走 ``project_change_hints`` 的 batch 总线，
与其它项目变更同一条 SSE（``/projects/{name}/events/stream``），不新增连接。

这些变更是**刷新信号**而非实体变更：``important=False`` + ``focus=None``，
前端据此不弹通知、不触发聚焦跳转，只用来重拉任务列表与受影响的画布数据。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from lib.project.project_change_hints import emit_project_change_batch

logger = logging.getLogger(__name__)

#: End states of the task lifecycle. Same rule as the frontend's ``isTerminalStatus``. / 任务生命周期末端状态。与前端 ``isTerminalStatus`` 同口径。
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

#: entity_type for task terminal changes, distinct from project entity changes. / 任务终态变更的 entity_type，与项目实体变更区分开。
TASK_ENTITY_TYPE = "task"

_STATUS_ACTIONS: dict[str, str] = {
    "succeeded": "task_succeeded",
    "failed": "task_failed",
    "cancelled": "task_cancelled",
}


def build_task_terminal_change(event: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one terminal record into a project change dict; return None when the status is not terminal.

    把一条终态记录转成项目变更 dict；状态非终态时返回 None。"""
    action = _STATUS_ACTIONS.get(str(event.get("status") or ""))
    if action is None:
        return None

    task_id = str(event.get("task_id") or "")
    if not task_id:
        return None

    return {
        "entity_type": TASK_ENTITY_TYPE,
        "action": action,
        "entity_id": task_id,
        # label does not go into notification text (important=False); it is only a readable identifier for logs/debugging.
        # label 不进通知文案（important=False），仅作日志/调试可读标识。
        "label": task_id,
        "focus": None,
        "important": False,
        # The frontend uses this to decide which canvas needs refetching (currently only reference_video uses it).
        # 前端据此判定哪类画布需要重拉（当前只有 reference_video 用到）。
        "task_type": str(event.get("task_type") or ""),
    }


def emit_task_terminal_events(events: Iterable[dict[str, Any]]) -> None:
    """Publish task terminal events grouped by project.

    Publish failures are not raised: the task status itself is already persisted, events are only a real-time
    optimisation, and frontend polling is the fallback.

    按项目分组发布任务终态事件。

    发布失败不向上抛：任务状态本身已落库，事件只是实时性优化，前端轮询兜底。
    """
    by_project: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        project_name = str(event.get("project_name") or "")
        if not project_name:
            continue
        change = build_task_terminal_change(event)
        if change is None:
            continue
        by_project.setdefault(project_name, []).append(change)

    for project_name, changes in by_project.items():
        try:
            emit_project_change_batch(project_name, changes)
        except Exception:
            logger.exception("发送任务终态项目事件失败 project=%s count=%d", project_name, len(changes))
