"""Task lifecycle -> project events: queued, started, and a generation batch's completion.

Terminal states (succeeded / failed / cancelled) have been on the project event stream for a while
(:mod:`lib.generation.task_terminal_events`). This module adds the steps before them and the batch level, so a
client can follow the queue without polling ``/tasks``:

- ``task_queued``: a new task row was created (a deduplicated enqueue creates nothing and sends nothing).
- ``task_started``: the worker dispatched the task to its provider slot (a claim that is put straight back because
  the slot is full is not a start), or a failed download was retried.
- ``batch_queued``: the coalesced form of ``task_queued`` for members of one generation batch, see
  :func:`coalesce_task_signals`.
- ``batch_done``: the last member of a generation batch reached a terminal state; carries the batch's counts.

All of them are refresh signals like the terminal changes: ``entity_type`` is ``task``, ``important=False``,
``focus=None``. ``task_queued`` / ``task_started`` / ``batch_queued`` change no project file, so they travel on the
signal bus of :mod:`lib.project.project_change_hints` (broadcast without re-reading the project); ``batch_done`` is
sent in the same change batch as the terminal change that completed the batch, so it always arrives after it.

Publishing never raises: the task rows are the truth, events only make them visible sooner.

任务生命周期 → 项目事件：入队、开始执行，以及生成批次的完成。

终态（succeeded / failed / cancelled）早已经项目事件流推送（:mod:`lib.generation.task_terminal_events`）。本模块
补上终态之前的步骤与批次层级，客户端不必轮询 ``/tasks`` 就能跟上队列：

- ``task_queued``：新建了一条任务（去重命中的入队不新建，也不发）。
- ``task_started``：worker 把任务派发到供应商槽位（因槽位已满立刻回队的认领不算开始），或重试了一次失败的下载。
- ``batch_queued``：同一生成批次成员的 ``task_queued`` 合并后的形态，见 :func:`coalesce_task_signals`。
- ``batch_done``：生成批次的最后一个成员落终态；携带该批次的计数。

它们与终态变更一样是刷新信号：``entity_type`` 为 ``task``、``important=False``、``focus=None``。
``task_queued`` / ``task_started`` / ``batch_queued`` 不改任何项目文件，走 :mod:`lib.project.project_change_hints`
的信号总线（直接广播，不重读项目）；``batch_done`` 与完成该批次的那条终态变更同批发送，因此总在它之后到达。

发布从不抛错：任务行才是真相，事件只让它更早可见。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from lib.project.project_change_hints import emit_project_signal_batch

logger = logging.getLogger(__name__)

#: entity_type shared by every task and batch signal. / 任务与批次信号共用的 entity_type。
TASK_ENTITY_TYPE = "task"

TASK_QUEUED_ACTION = "task_queued"
TASK_STARTED_ACTION = "task_started"
BATCH_QUEUED_ACTION = "batch_queued"
BATCH_DONE_ACTION = "batch_done"

_LIFECYCLE_ACTIONS: dict[str, str] = {
    "queued": TASK_QUEUED_ACTION,
    "running": TASK_STARTED_ACTION,
}

#: Task fields copied onto every task change, beside the identity fields. ``source`` is the task's caller label (G3:
#: ``webui``, ``mcp``, an ``X-Client`` name), not the change origin carried at the top of the ``changes`` payload.
#: 复制到每条任务变更上的任务字段（身份字段之外）。``source`` 是任务的调用方标签（G3：``webui``、``mcp``、
#: ``X-Client`` 名），不是 ``changes`` 负载顶层的变更来源。
TASK_CHANGE_FIELDS: tuple[str, ...] = (
    "batch_id",
    "resource_id",
    "resource_type",
    "script_file",
    "media_type",
    "source",
    "queued_at",
    "started_at",
    "finished_at",
)


def task_failure_code(error_message: object) -> str | None:
    """Machine code of a stored task failure (the same ``error_code`` ``GET /tasks`` exposes); ``None`` for raw text.

    存储的任务失败的机器码（与 ``GET /tasks`` 给出的 ``error_code`` 相同）；原始文本为 ``None``。"""
    if not isinstance(error_message, str) or not error_message:
        return None
    # Imported here, not at module level: the generation result model pulls in the artifact layer, and this module
    # loads early (the task queue imports it).
    # 在此导入而非模块顶层：生成结果模型会带入产物层，而本模块加载得早（任务队列引用它）。
    from lib.generation.generation_result import decode_generation_problem
    from lib.generation.task_failure import parse_failure

    problem = decode_generation_problem(error_message)
    if problem is not None:
        return problem.code
    parsed = parse_failure(error_message)
    return parsed[0] if parsed is not None else None


def build_task_change(event: Mapping[str, Any], action: str) -> dict[str, Any] | None:
    """One task record (the dict shape of a task row) -> a ``task`` project change with ``action``; ``None`` without id.

    一条任务记录（任务行的 dict 形态）→ 动作为 ``action`` 的 ``task`` 项目变更；没有 id 时为 ``None``。"""
    task_id = str(event.get("task_id") or "")
    if not task_id:
        return None
    status = str(event.get("status") or "")
    change: dict[str, Any] = {
        "entity_type": TASK_ENTITY_TYPE,
        "action": action,
        "entity_id": task_id,
        # Not notification text (important=False); a readable identifier for logs. / 不进通知文案（important=False），仅作日志可读标识。
        "label": task_id,
        "focus": None,
        "important": False,
        "task_type": str(event.get("task_type") or ""),
        "task_id": task_id,
        "status": status,
    }
    for field in TASK_CHANGE_FIELDS:
        change[field] = event.get(field)
    if status == "failed":
        change["error_code"] = task_failure_code(event.get("error_message"))
    if status == "cancelled":
        change["cancelled_by"] = event.get("cancelled_by")
    if event.get("requeued"):
        change["requeued"] = True
    return change


def build_task_lifecycle_change(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """A queued or running task record -> ``task_queued`` / ``task_started``; other statuses -> ``None``.

    排队中或执行中的任务记录 → ``task_queued`` / ``task_started``；其余状态 → ``None``。"""
    action = _LIFECYCLE_ACTIONS.get(str(event.get("status") or ""))
    return build_task_change(event, action) if action is not None else None


def emit_task_lifecycle_events(events: Iterable[Mapping[str, Any]]) -> None:
    """Publish queued / started records on the signal bus, grouped by project; never raises.

    把排队 / 开始记录按项目分组发上信号总线；从不抛错。"""
    by_project: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        project_name = str(event.get("project_name") or "")
        change = build_task_lifecycle_change(event) if project_name else None
        if change is not None:
            by_project.setdefault(project_name, []).append(change)
    for project_name, changes in by_project.items():
        try:
            emit_project_signal_batch(project_name, changes)
        except Exception:
            logger.exception("发送任务生命周期事件失败 project=%s count=%d", project_name, len(changes))


def coalesce_task_signals(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fold the ``task_queued`` changes of generation batch members into one ``batch_queued`` per batch.

    A batch route enqueues its members one by one; without folding, a 60-shot batch would be 60 events. The fold
    keeps the position of the batch's first member; ``task_ids`` lists the members in enqueue order. Tasks without a
    batch, requeued tasks and every other action pass through unchanged. Applied by the event stream to the signals it
    collects within one short window, so a slow batch may show up as several ``batch_queued`` with the same id.

    把生成批次成员的 ``task_queued`` 变更合并成每批一条 ``batch_queued``。

    批次路由逐个入队成员；不合并的话，60 个分镜的批次就是 60 条事件。合并后的位置取该批第一个成员的位置；
    ``task_ids`` 按入队顺序列出成员。不属于批次的任务、回队的任务与其余动作原样通过。由事件流对一个短窗口内
    收集到的信号使用，因此慢的批次可能出现多条同 id 的 ``batch_queued``。
    """
    result: list[dict[str, Any]] = []
    folded: dict[str, dict[str, Any]] = {}
    for change in changes:
        batch_id = change.get("batch_id")
        if (
            change.get("entity_type") != TASK_ENTITY_TYPE
            or change.get("action") != TASK_QUEUED_ACTION
            or not isinstance(batch_id, str)
            or not batch_id
            or change.get("requeued")
        ):
            result.append(dict(change))
            continue
        entry = folded.get(batch_id)
        if entry is None:
            entry = {
                "entity_type": TASK_ENTITY_TYPE,
                "action": BATCH_QUEUED_ACTION,
                "entity_id": batch_id,
                "label": batch_id,
                "focus": None,
                "important": False,
                "batch_id": batch_id,
                "source": change.get("source"),
                "count": 0,
                "task_ids": [],
                "task_types": [],
                "queued_at": change.get("queued_at"),
            }
            folded[batch_id] = entry
            result.append(entry)
        entry["count"] += 1
        entry["task_ids"].append(change.get("task_id") or change.get("entity_id"))
        task_type = change.get("task_type")
        if task_type and task_type not in entry["task_types"]:
            entry["task_types"].append(task_type)
    return result


#: How many completed batches the process remembers to send ``batch_done`` once. / 进程记住多少个已完成批次以只发一次 ``batch_done``。
_ANNOUNCED_BATCH_LIMIT = 4096
_announced_batches: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
_announced_lock = threading.Lock()


def claim_batch_done(batch_id: str, outcome: tuple[Any, ...]) -> bool:
    """Whether this process should announce ``batch_done`` for ``batch_id`` with this final ``outcome`` (its counts).

    Two members finishing at the same moment both see the batch complete; only the first claim wins. A batch
    completes again with a different outcome only after a member was retried (a failed download back to running),
    and that is announced again. Remembered per process (bounded), so a restart forgets: then a later terminal change
    of an already complete batch, which does not happen without a retry, would announce it once more.

    本进程是否应为 ``batch_id`` 以这个最终 ``outcome``（其计数）通告 ``batch_done``。

    两个成员同时完成时都会看到批次已完成，只有第一个认领生效。批次只有在某个成员被重试（失败的下载回到执行中）
    之后才会以不同结果再次完成，届时会再通告一次。按进程（有上限）记忆，重启即忘：届时已完成批次若再出现终态
    变更（不重试就不会发生），会再通告一次。
    """
    with _announced_lock:
        if _announced_batches.get(batch_id) == outcome:
            return False
        _announced_batches[batch_id] = outcome
        _announced_batches.move_to_end(batch_id)
        while len(_announced_batches) > _ANNOUNCED_BATCH_LIMIT:
            _announced_batches.popitem(last=False)
        return True


_submitting_batches: dict[str, int] = {}


@contextmanager
def batch_submission(batch_id: str) -> Iterator[None]:
    """Hold back ``batch_done`` for ``batch_id`` while its members are still being enqueued.

    A requested unit without a task counts as failed (the batch read model's rule: its enqueue failed), so a member
    finishing before the next one is enqueued would otherwise make the batch look complete. The submitter checks the
    batch once more after the enqueue phase (``GenerationQueue.announce_batch_if_done``).

    批次成员仍在入队时压住该批次的 ``batch_done``。

    没有任务的请求单元计为失败（批次读模型的规则：它入队失败），否则某个成员在下一个入队之前就完成时，批次会看起来
    已完成。提交方在入队阶段之后再检查一次批次（``GenerationQueue.announce_batch_if_done``）。
    """
    with _announced_lock:
        _submitting_batches[batch_id] = _submitting_batches.get(batch_id, 0) + 1
    try:
        yield
    finally:
        with _announced_lock:
            remaining = _submitting_batches.get(batch_id, 1) - 1
            if remaining > 0:
                _submitting_batches[batch_id] = remaining
            else:
                _submitting_batches.pop(batch_id, None)


def _is_submitting(batch_id: str) -> bool:
    with _announced_lock:
        return batch_id in _submitting_batches


def build_batch_done_change(progress: Mapping[str, Any]) -> dict[str, Any]:
    """A completed batch's progress (see ``TaskRepository.batch_progress_for_tasks``) -> its ``batch_done`` change.

    已完成批次的进度（见 ``TaskRepository.batch_progress_for_tasks``）→ 它的 ``batch_done`` 变更。"""
    batch_id = str(progress["batch_id"])
    return {
        "entity_type": TASK_ENTITY_TYPE,
        "action": BATCH_DONE_ACTION,
        "entity_id": batch_id,
        "label": batch_id,
        "focus": None,
        "important": False,
        "batch_id": batch_id,
        "operation": progress.get("operation"),
        "source": progress.get("source"),
        "created_at": progress.get("created_at"),
        "counts": dict(progress.get("counts") or {}),
    }


def batch_done_changes(progress: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``batch_done`` changes, by project, for the batches that are complete now and not announced yet.

    此刻已完成且尚未通告的批次的 ``batch_done`` 变更，按项目分组。"""
    by_project: dict[str, list[dict[str, Any]]] = {}
    for item in progress:
        if not item.get("done") or _is_submitting(str(item["batch_id"])):
            continue
        counts = item.get("counts") or {}
        outcome = tuple(sorted(counts.items()))
        if not claim_batch_done(str(item["batch_id"]), outcome):
            continue
        by_project.setdefault(str(item["project_name"]), []).append(build_batch_done_change(item))
    return by_project


__all__ = [
    "BATCH_DONE_ACTION",
    "BATCH_QUEUED_ACTION",
    "TASK_CHANGE_FIELDS",
    "TASK_ENTITY_TYPE",
    "TASK_QUEUED_ACTION",
    "TASK_STARTED_ACTION",
    "batch_done_changes",
    "batch_submission",
    "build_batch_done_change",
    "build_task_change",
    "build_task_lifecycle_change",
    "claim_batch_done",
    "coalesce_task_signals",
    "emit_task_lifecycle_events",
    "task_failure_code",
]
