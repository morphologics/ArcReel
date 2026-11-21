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
from collections.abc import Iterable, Mapping
from typing import Any

from lib.generation.task_lifecycle_events import build_task_change
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

    Besides the identity fields the change carries the task's facts (``batch_id``, ``resource_id``, ``status``,
    ``source``, timestamps; ``error_code`` when failed, ``cancelled_by`` when cancelled), see
    :func:`lib.generation.task_lifecycle_events.build_task_change`.

    把一条终态记录转成项目变更 dict；状态非终态时返回 None。

    除身份字段外，变更还带着任务的事实（``batch_id``、``resource_id``、``status``、``source``、时间戳；失败时
    ``error_code``，取消时 ``cancelled_by``），见 :func:`lib.generation.task_lifecycle_events.build_task_change`。
    """
    action = _STATUS_ACTIONS.get(str(event.get("status") or ""))
    if action is None:
        return None
    # task_type: the frontend uses it to decide which canvas needs refetching (currently only reference_video uses it).
    # task_type：前端据此判定哪类画布需要重拉（当前只有 reference_video 用到）。
    return build_task_change(event, action)


def emit_task_terminal_events(
    events: Iterable[dict[str, Any]],
    extra_changes: Mapping[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Publish task terminal events grouped by project.

    Publish failures are not raised: the task status itself is already persisted, events are only a real-time
    optimisation, and frontend polling is the fallback. ``extra_changes`` (by project) are appended to the same
    project's batch: the ``batch_done`` of batches these terminal states completed, so they arrive after them.

    按项目分组发布任务终态事件。

    发布失败不向上抛：任务状态本身已落库，事件只是实时性优化，前端轮询兜底。``extra_changes``（按项目）
    追加到同一项目的批次末尾：这些终态完成的批次的 ``batch_done``，因此在它们之后到达。
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
    for project_name, changes in (extra_changes or {}).items():
        by_project.setdefault(project_name, []).extend(changes)

    for project_name, changes in by_project.items():
        try:
            emit_project_change_batch(project_name, changes)
        except Exception:
            logger.exception("发送任务终态项目事件失败 project=%s count=%d", project_name, len(changes))
