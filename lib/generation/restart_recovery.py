"""Restart self-healing of the generation Worker: reclaim orphan tasks, settle interrupted calls, dispatch resumes by bucket.

Orphans only come from process restarts (deployment and decision premises: see ``lib.generation.generation_worker``
and ``docs/adr/0007``). The triage principle is **never actively cause extra charges**: when the worker cannot confirm
it can resume a job the provider has already accepted, the orphan is marked failed and dropped, never resubmitted.
"Retry download" reuses the same resume dispatch.

生成 Worker 的重启自愈：回收孤儿任务、结算被打断的调用、分桶派发续跑。

孤儿只来自进程重启（部署与判定前提见 ``lib.generation.generation_worker`` 与
``docs/adr/0007``）。分流原则是**不主动产生额外扣费**：worker 不能确认能接续供应商已收单的
job 时，把孤儿标记为失败丢弃，绝不重新提交。「重试下载」复用同一条续跑派发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from lib.artifacts.rendered_artifact import cleanup_interrupted_render_staging
from lib.backends.providers import PROVIDER_GROK, PROVIDER_VIDU
from lib.config.service import read_video_poll_timeout_seconds
from lib.generation.render_lane import RENDER_MEDIA_TYPE
from lib.generation.task_failure import encode_failure
from lib.generation.video_resume import VideoResumeRunner, cleanup_video_staging
from lib.infra.async_thread import run_sync_transaction
from lib.project.project_manager import get_project_manager
from lib.script.reference_video.execution_checkpoint import VideoResumeState, classify_video_resume_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from lib.generation.generation_queue import GenerationQueue
    from lib.generation.generation_worker import CapacityTable, SlotTable

    ProviderProjection = Callable[[dict[str, Any]], Awaitable[str]]

logger = logging.getLogger(__name__)

# Video providers that don't implement VideoBackend.resume_video: the orphan scan marks their running orphans failed with
# [resume_unsupported] rather than actively requeuing them -- to avoid double charges for requests already submitted to
# the provider (Grok is synchronous with no job_id; Vidu's generate polls inline, with no separate resume).
# When adding a backend without resume support, register it here as well.
# 不实现 VideoBackend.resume_video 的视频 provider：孤儿扫描把它们的 running 孤儿标记为
# [resume_unsupported] 失败，而非主动 requeue 重跑——避免对已经提交给供应商的请求二次扣费
# （Grok 同步型无 job_id；Vidu 的 generate 内联 poll，没有独立 resume）。
# 新增不支持 resume 的 backend 时同步在这里登记。
NON_RESUMABLE_VIDEO_PROVIDERS = frozenset({PROVIDER_GROK, PROVIDER_VIDU})


def _cleanup_orphan_render_staging(project_name: str) -> None:
    """Locate the project directory in a worker thread and clean up temporary files left by an interrupted render.

    在工作线程里定位项目目录并清理中断渲染留下的临时文件。"""

    cleanup_interrupted_render_staging(get_project_manager().get_project_path(project_name))


class InterruptedCallSettler(Protocol):
    """Startup settlement entry point: returns the number of pending call rows turned terminal. Pending rows without a task
    identity are only settled if started before ``taskless_started_before``; passing ``None`` settles only rows bound to a task.

    启动收口入口：返回已翻成终态的 pending 调用行数。无任务身份的 pending 行只收口在
    ``taskless_started_before`` 之前发起的，传 ``None`` 则只收口绑定了任务的行。"""

    def __call__(self, *, taskless_started_before: datetime | None) -> Awaitable[int]: ...


class RestartRecovery:
    """Restart self-healing collaborator, held by ``GenerationWorker``.

    Shares the same capacity table, occupancy ledger and stop signal with the worker (references constant, never rebuilt):
    resume sub-tasks are registered in the same occupancy ledger, drained by the worker main loop and awaited on shutdown.

    重启自愈协作者，由 ``GenerationWorker`` 持有。

    与 worker 共用同一张容量表、占用台账与停止信号（引用恒定，不被重建）：续跑 sub-task 登记
    进同一份占用台账，由 worker 主循环 drain、关停时一并等待。
    """

    def __init__(
        self,
        *,
        queue: GenerationQueue,
        capacity: CapacityTable,
        slots: SlotTable,
        stop_event: asyncio.Event,
        reload_limits: Callable[[], Awaitable[None]],
        provider_projection: ProviderProjection,
        resume: VideoResumeRunner,
        settle_interrupted_calls: InterruptedCallSettler,
    ) -> None:
        self._queue = queue
        self._capacity = capacity
        self._slots = slots
        self._stop_event = stop_event
        self._reload_limits = reload_limits
        self._provider_projection = provider_projection
        self._resume = resume
        self._settle_interrupted_calls = settle_interrupted_calls
        # Orphan dispatcher handle is persisted: shutdown awaits it to finish; when the lease is regained after a switch,
        # handle_orphans is entered a second time, and the old handle cannot simply be overwritten if not done.
        # Orphan dispatcher 句柄持久化：shutdown 时 await 它跑完；lease 切换重夺时
        # 第二次进 handle_orphans，旧句柄未 done 不能直接覆盖。
        self.orphan_dispatcher_task: asyncio.Task | None = None
        # "Retry download" dispatch is fire-and-forget: without a strong reference nobody outside the event loop references the
        # task, and it may be silently garbage-collected at a suspension point. Removed from the set on completion.
        # 「重试下载」的派发是即发即忘的：不持强引用的话，事件循环之外无人引用该 task，
        # 它可能在挂起点被 GC 静默回收。完成即从集合摘除。
        self._retry_dispatch_tasks: set[asyncio.Task] = set()

    async def wait_dispatchers(self) -> None:
        """shutdown: wait for the dispatcher to dispatch the last batch of sub-tasks (otherwise sub-tasks might be created after
        the dispatcher exits). A dispatcher exception must not break the shutdown chain.

        shutdown：等 dispatcher 派完最后一批 sub-task（否则 sub-task 可能在 dispatcher 退出后
        才创建）。dispatcher 异常不能断 shutdown 链。"""
        if self.orphan_dispatcher_task is not None and not self.orphan_dispatcher_task.done():
            try:
                await self.orphan_dispatcher_task
            except Exception:
                logger.exception("orphan dispatcher 在 shutdown 等待时异常")

        # Same for the "retry download" dispatcher: the task is already turned running before dispatch, and if the dispatcher
        # exits before registering the sub-task, this job would only be picked up by the orphan scan at the next startup.
        # 「重试下载」的 dispatcher 同理：任务在派发之前就已经被翻成 running，dispatcher 还没
        # 登记 sub-task 就退出的话，这一笔要等到下次启动的孤儿扫描才被接手。
        if retry_dispatchers := [t for t in self._retry_dispatch_tasks if not t.done()]:
            await asyncio.gather(*retry_dispatchers, return_exceptions=True)

    async def settle_interrupted_calls(self, *, taskless_started_before: datetime | None) -> None:
        """After orphan task handling, settle pending call rows with no live task (sharing the one-shot switch with the orphan scan).

        The order cannot be reversed: orphan handling first turns tasks that cannot be resumed terminal, so settlement can
        judge their call rows by the task's outcome; the other way round those tasks would still be running and their call rows
        would be skipped as "task still alive". Settlement failures are only logged and don't block the claim loop -- finishing
        the accounts matters less than the worker failing to start.

        孤儿任务处理之后收口没有存活任务的 pending 调用行（与孤儿扫描共用一次性开关）。

        顺序不能反：孤儿处理先把无法接续的任务翻成终态，收口才能按任务的结局判定它的调用行；
        反过来那些任务还是 running，它们的调用行会被当成「任务还活着」跳过。收口失败只记日志，
        不阻断认领循环——账目收尾比不上 worker 起不来。
        """
        try:
            settled = await self._settle_interrupted_calls(taskless_started_before=taskless_started_before)
        except Exception:
            logger.warning("启动收口 pending 调用行失败", exc_info=True)
            return
        if settled:
            logger.info(
                "启动收口：%d 条无存活任务的 pending 调用行已结算（%s）",
                settled,
                "仅绑定任务的行" if taskless_started_before is None else "含启动前发起的无任务行",
            )

    async def handle_orphans(self) -> None:
        """Restart self-healing: scan running orphans and triage by "can it be safely resumed".

        Principle -- **never actively cause extra charges**: whenever the worker cannot confirm it can resume a job the
        provider has already accepted, mark the orphan failed and drop it, never resubmit.

        - image running -> [restart_lost] (image tasks don't persist job_id and cannot be resumed;
          and the image submission is already billed, rerunning would double-charge)
        - video running, provider ∈ NON_RESUMABLE_VIDEO_PROVIDERS (Grok/Vidu)
          -> [resume_unsupported] (the backend doesn't implement resume_video, no means to resume the original job)
        - video running, resumable backend (ark/gemini/openai/newapi):
          - no provider_job_id -> [restart_lost]
          - has job_id -> collected into the `resumable_by_provider` bucket, dispatched in batches by the background
            dispatcher under video capacity limits

        The startup fast path (this function) **only does terminating handling** and returns immediately; resumable video
        orphans are handed to the background dispatcher, to avoid N Sora orphans × 5 min poll each blocking startup for tens
        of minutes. The dispatcher doesn't drain the occupancy ledger and relies entirely on the worker main loop cleaning up
        each cycle; when the stop signal fires the dispatcher exits naturally.

        重启自愈：扫 running 孤儿，按"是否可安全 resume"分流。

        原则——**不主动产生额外扣费**：只要 worker 不能确认能接续供应商已收单的 job，
        就把孤儿标记为失败丢弃，绝不重新提交。

        - image running → [restart_lost]（image 任务不持久化 job_id，无法接续；
          且 image 提交本身已计费，重跑等于双重扣费）
        - video running，provider ∈ NON_RESUMABLE_VIDEO_PROVIDERS（Grok/Vidu）
          → [resume_unsupported]（backend 不实现 resume_video，原 job 无接续手段）
        - video running，可 resume backend (ark/gemini/openai/newapi)：
          - 无 provider_job_id → [restart_lost]
          - 有 job_id → 收集到 `resumable_by_provider` 桶，后台 dispatcher 受
            video 容量约束分批 dispatch

        启动期 fast path（本函数）**只做终结类处理**，立刻返回；可 resume 的视频孤儿
        派发给后台 dispatcher 处理，避免 N 个 Sora orphan × 每个 5min poll 把启动期
        阻塞数十分钟。Dispatcher 不 drain 占用台账，完全依赖 worker 主循环每 cycle
        清理；停止信号触发时 dispatcher 自然退出。
        """
        orphans = await self._queue.list_orphan_tasks_on_start()
        if not orphans:
            return
        logger.info("等待 lease 获取后开始扫孤儿（待处理 %d 个）", len(orphans))

        # self-active guards against self-preemption: after lease flap > 3×TTL the one-shot scan switch resets, and rescanning
        # DB running would include tasks this process still has inflight. Without excluding them:
        # - image tasks -> wrongly marked [restart_lost] (marked failed while still running)
        # - video tasks -> start a duplicate resume flow, the same provider job is polled/finalised concurrently,
        #   and a crash window could cause double charges on the provider side (violating the ADR 0007 red line)
        # active_task_ids contains all pending+inflight occupancy; a task with the same id found in the DB is treated as this process's live work.
        # self-active 防 self-preemption：lease flap > 3×TTL 后一次性扫描开关
        # 重置，再扫 DB running 会包含本进程仍 inflight 的 task。若不排除：
        # - image 任务 → 错误标 [restart_lost]（任务还在跑就被标失败）
        # - video 任务 → 启动重复 resume 流，同一 provider job 被并发 poll/finalize，
        #   崩溃窗口可能导致 provider 端双重扣费（违反 ADR 0007 红线）
        # active_task_ids 含 pending+inflight 全部占用，DB 扫到的同 id task 视为本进程的活。
        self_active_task_ids = self._slots.active_task_ids()

        resumable_by_provider: dict[str, list[dict[str, Any]]] = {}

        for task in orphans:
            task_id = task["task_id"]
            if task_id in self_active_task_ids:
                logger.info("孤儿扫到本进程仍 active 的 task %s，跳过避免 self-preemption", task_id)
                continue
            task_type = task.get("task_type")
            if task.get("media_type"):
                media_type = task["media_type"]
            elif task_type in ("video", "reference_video"):
                media_type = "video"
            elif task_type == "tts":
                media_type = "audio"
            else:
                media_type = "image"

            # image tasks neither persist job_id nor have a resume entry -- a request already submitted to the provider cannot be
            # recovered, and actively requeuing would double-charge. Drop it and let the user decide whether to retry manually.
            # image 任务不持久化 job_id 也无 resume 入口——已提交给供应商的请求无法回收，
            # 主动 requeue 会双重扣费。直接丢弃，等用户决定是否手动重试。
            if media_type == "image":
                logger.warning("孤儿 image running → [restart_lost]: %s", task_id)
                await self._queue.mark_task_failed(
                    task_id,
                    encode_failure("restart_lost_image"),
                )
                continue

            # audio (TTS) is synchronous, doesn't persist job_id and has no resume entry -- degraded to [restart_lost] like image,
            # not resubmitted to avoid duplicate charges.
            # audio（TTS）同步、不持久化 job_id、无 resume 入口——与 image 同样降级为
            # [restart_lost]，不重新提交以免重复计费。
            if media_type == "audio":
                logger.warning("孤儿 audio running → [restart_lost]: %s", task_id)
                await self._queue.mark_task_failed(
                    task_id,
                    encode_failure("restart_lost_audio"),
                )
                continue

            # A synchronous text call may keep running in a thread, but after a process restart there is no job identity to resume;
            # like image/audio, not resubmitted automatically, to avoid duplicate charges.
            # text 同步调用可能在线程中继续运行，但进程重启后没有可接续的 job identity；
            # 与 image/audio 一样不自动重交，避免重复计费。
            if media_type == "text":
                logger.warning("孤儿 text running → [restart_lost]: %s", task_id)
                await self._queue.mark_task_failed(task_id, encode_failure("restart_lost_text"))
                continue

            # Local renders don't resume; on restart clean up temporary files, and when the lease is regained keep renders this process is still running.
            # Not requeued automatically; the user decides whether to render again.
            # 本地渲染不续跑；重启时清理临时文件，lease 重夺时保留本进程仍在执行的渲染。
            # 不自动重排，由用户决定是否重新渲染。
            if media_type == RENDER_MEDIA_TYPE:
                logger.warning("孤儿 render running → [restart_lost]: %s", task_id)
                if not self._slots.occupied_providers(RENDER_MEDIA_TYPE):
                    try:
                        await run_sync_transaction(_cleanup_orphan_render_staging, task["project_name"])
                    except Exception:
                        logger.warning("清理孤儿 render 临时文件失败: %s", task_id, exc_info=True)
                await self._queue.mark_task_failed(task_id, encode_failure("restart_lost_render"))
                continue

            checkpoint = None
            if task_type in ("video", "reference_video"):
                resume_state, checkpoint = classify_video_resume_state(task)
                if resume_state is not VideoResumeState.READY:
                    if resume_state is VideoResumeState.NO_CHECKPOINT_NO_JOB:
                        failure = encode_failure("restart_lost_no_job_id")
                    elif resume_state is VideoResumeState.CHECKPOINT_WITHOUT_JOB:
                        failure = encode_failure("restart_lost_checkpoint_no_job_id")
                    else:
                        failure = encode_failure(
                            "execution_identity_unrecoverable",
                            detail="missing, malformed, or mismatched video submission checkpoint",
                        )
                    await self._queue.mark_task_failed(task_id, failure)
                    await cleanup_video_staging(task)
                    continue

            # video path: decide whether the provider supports resume. Both generation modes only use the immutable checkpoint;
            # otherwise, when the project config switches across a restart, the provider projection would be re-resolved by the
            # current project, possibly misjudging an original Grok/Vidu orphan as resumable, or routing a resumable task to the wrong pool.
            # video 路径：判断 provider 是否支持 resume。两种生成模式均只用不可变 checkpoint；
            # 否则项目配置在重启前后切换时，provider 投影会按当前项目重新解析，可能把原本
            # Grok/Vidu 孤儿误判成可 resume，或把可 resume 任务路由到错池。
            provider_id = (
                checkpoint.provider_id
                if checkpoint is not None
                else task.get("provider_id") or await self._provider_projection(task)
            )
            if provider_id in NON_RESUMABLE_VIDEO_PROVIDERS:
                # Grok/Vidu currently don't implement resume_video -- the original job was already sent to the provider with no means to
                # resume, and rerunning would charge twice. Drop it and let the user decide manually whether to retry.
                # Grok/Vidu 当前不实现 resume_video——原 job 已发给供应商无接续手段，
                # 重跑会重复扣费。丢弃，由用户手动决定是否重试。
                logger.warning(
                    "孤儿 video running (provider=%s 不支持 resume) → [resume_unsupported]: %s",
                    provider_id,
                    task_id,
                )
                await self._queue.mark_task_failed(
                    task_id,
                    encode_failure("resume_unsupported_provider", provider_id=provider_id),
                )
                await cleanup_video_staging(task)
                continue

            job_id = task.get("provider_job_id")
            if not job_id:
                logger.warning("孤儿 running 无 job_id → [restart_lost]: %s", task_id)
                await self._queue.mark_task_failed(task_id, encode_failure("restart_lost_no_job_id"))
                await cleanup_video_staging(task)
                continue

            # Collect into the provider bucket for the background dispatcher to process in batches under pool capacity limits.
            # Also write the resolved provider_id back into the task dict, for dispatcher routing.
            # 收集到 provider 桶，交给后台 dispatcher 受 pool 容量约束分批处理。
            # 顺便把 resolve 出的 provider_id 写回 task dict，dispatcher 路由用。
            task["provider_id"] = provider_id
            resumable_by_provider.setdefault(provider_id, []).append(task)

        if resumable_by_provider:
            total = sum(len(v) for v in resumable_by_provider.values())
            poll_timeout_seconds = await read_video_poll_timeout_seconds()
            for tasks in resumable_by_provider.values():
                for task in tasks:
                    task["video_poll_timeout_seconds"] = poll_timeout_seconds
            logger.info(
                "孤儿扫描 fast path 完成：%d 个可 resume video 任务交后台分批 dispatch",
                total,
            )
            # When the lease is regained the old dispatcher may still be running (typical case: polling the provider inside
            # resume_video takes minutes to 10+ minutes). This round overwrites the handle directly, **without await or cancel**:
            # - no await: avoids blocking the main loop -> liveness problems (cannot renew the lease heartbeat / cannot respond to the cancel API)
            # - no cancel: cancel would make the old dispatcher's _run_one raise CancelledError and enter the
            #   process-level interruption fallback path, wrongly marking in-flight resumes still in progress as cancelled,
            #   and leaving the already charged job on the provider side without an owner
            # - overwrite directly: the old dispatcher_task's sub-tasks are still referenced by the occupancy ledger (SlotTable)
            #   + held by asyncio.gather's internal callback chain, so the old task is not GC-detached
            # - shutdown still sees them: worker shutdown waits for the old sub-tasks via _slots.all_active_tasks()
            # lease 重夺时旧 dispatcher 可能还在跑（典型场景：resume_video 内 poll provider
            # 需要几分钟到 10+ 分钟）。本轮**不 await 不 cancel** 直接覆盖句柄：
            # - 不 await：避免阻塞主循环 → liveness 问题（无法续 lease 心跳/无法响应 cancel API）
            # - 不 cancel：cancel 会让旧 dispatcher 的 _run_one 抛 CancelledError，进入
            #   进程级打断的兜底路径，把仍在接续的 in-flight resume 错误标为 cancelled，
            #   且让 provider 端已扣费 job 失去归属
            # - 直接覆盖：旧 dispatcher_task 的 sub-task 仍由占用台账（SlotTable）持有引用
            #   + asyncio.gather 内部 callback 链持有，旧 task 不会被 GC detached
            # - shutdown 仍能感知：worker 关停经 _slots.all_active_tasks() 等到旧 sub-task
            if self.orphan_dispatcher_task is not None and not self.orphan_dispatcher_task.done():
                logger.warning(
                    "旧 orphan dispatcher 仍在运行，本轮直接覆盖句柄不等待——"
                    "旧 sub-task 由占用台账跟踪，shutdown 时经 _slots.all_active_tasks 兜底"
                )
            self.orphan_dispatcher_task = asyncio.create_task(
                self.dispatch_resumable(resumable_by_provider),
                name="orphan-dispatcher",
            )
        else:
            logger.info("孤儿扫描完成，无可 resume 任务")

    async def dispatch_resumable(
        self,
        resumable_by_provider: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Background dispatcher: concurrent per provider bucket, entering inflight in batches under video capacity limits.

        - No capacity coupling between different providers -> run independent sub-tasks concurrently;
        - Sequential enqueue within the same provider: when full, `asyncio.wait(inflight, FIRST_COMPLETED)` waits for any one
          to finish (precise detection, no sleep polling);
        - The worker main loop drains done tasks each cycle -> the dispatcher's next has_room check finds a free slot (the key
          decoupling assumption);
        - When the stop signal fires the dispatcher exits naturally, holding no lease resources.

        后台 dispatcher：按 provider 分桶并发，受 video 容量约束分批入 inflight。

        - 不同 provider 之间无容量耦合 → 并发跑独立 sub-task；
        - 同 provider 内顺序入队：满则 `asyncio.wait(inflight, FIRST_COMPLETED)` 等任一
          完成（精确感知，不 sleep 轮询）；
        - worker 主循环每 cycle drain 已 done 的 task → dispatcher 下次 has_room 判定就有
          空位（解耦关键假设）；
        - 停止信号触发时 dispatcher 自然退出，不持有 lease 资源。
        """
        if self._stop_event.is_set():
            return
        sub_tasks = [
            asyncio.create_task(
                self.dispatch_provider_bucket(provider_id, tasks),
                name=f"orphan-dispatch-{provider_id}",
            )
            for provider_id, tasks in resumable_by_provider.items()
        ]
        await asyncio.gather(*sub_tasks, return_exceptions=True)
        logger.info("孤儿后台 dispatcher 完成")

    async def retry_artifact_download(self, task: dict[str, Any], *, poll_timeout_seconds: int) -> None:
        """Dispatch one download recovery for the original provider job, without resubmitting the provider task.

        按原 provider job 派发一次下载恢复，不重新提交供应商任务。"""
        provider_id = task.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("retry-download task has no provider_id")
        task["video_poll_timeout_seconds"] = poll_timeout_seconds
        dispatch = asyncio.create_task(
            self.dispatch_resumable({provider_id: [task]}),
            name=f"retry-download-{task['task_id']}",
        )
        self._retry_dispatch_tasks.add(dispatch)
        dispatch.add_done_callback(self._retry_dispatch_tasks.discard)

    async def dispatch_provider_bucket(
        self,
        provider_id: str,
        tasks: list[dict[str, Any]],
    ) -> None:
        """Run resume tasks concurrently within the same provider bucket, using phase flags pending/inflight for precise capacity and cancel tracking.

        - ``cap <= 0``: fail-fast mark_failed[resume_unsupported], without entering a ``Semaphore(0)`` deadlock; reload once
          as a fallback, to avoid misjudging due to capability jitter at startup.
        - Sub-tasks are pre-registered synchronously by the parent coroutine as PENDING in the occupancy ledger -- avoiding the
          main loop's ``has_room`` seeing occupancy=0 and wrongly assuming capacity before the async ``create_task`` scheduling fires.
        - After a successful sem acquire, ``promote`` flips that occupancy to INFLIGHT (same sub-task, only the flag flips);
          ``release`` in finally.
        - The sem capacity is read once from CapacityTable at the top of dispatch and fixed: capacity table changes during
          reload don't affect this batch's concurrency limit. This is a known design choice, not a bug.

        同 provider 桶并发跑 resume task，pending/inflight 用 phase 标志精确容量与 cancel 跟踪。

        - ``cap <= 0``：fail-fast mark_failed[resume_unsupported]，不进 ``Semaphore(0)``
          死锁；reload 一次兜底，避免启动期 capability 抖动误判。
        - sub-task 由父协程同步预先以 PENDING 登记到占用台账——避免 ``create_task``
          异步调度还未触发时主循环 ``has_room`` 看占用=0 误判可有容量。
        - sem acquire 成功后 ``promote`` 把该占用翻成 INFLIGHT（同一 sub-task，只翻标志）；
          finally ``release``。
        - sem 容量在 dispatch 顶部从 CapacityTable 读一次定型：reload 期间改的容量表
          不影响本批 dispatch 的并发上限。这是已知设计选择，非 bug。
        """
        cap = self._capacity.get(provider_id, "video")
        if cap <= 0:
            # Startup reload fallback: DB loading may come later than the first orphan scan. / 启动期 reload 兜底：DB 加载可能晚于第一次 orphan 扫描。
            try:
                await self._reload_limits()
            except Exception:
                logger.warning("reload_limits 兜底失败", exc_info=True)
            cap = self._capacity.get(provider_id, "video")
        if cap <= 0:
            # Judged dead before dispatch, with no exception object in hand: this sentence is the failure text of these call rows,
            # in the same registry as the expired/switch-endpoint messages raised by backends (agent-facing text, not in i18n, with no classifiable machine code).
            # 派发前就判死，手上没有异常对象：这一句就是这些调用行的失败原文，与 backend 抛出的
            # 过期/换端点消息同一登记册（agent-facing 原文，不进 i18n，也没有可分类的机器码）。
            no_capacity = f"resume unsupported: provider {provider_id} has no video capacity"
            for t in tasks:
                await self._queue.mark_task_failed(
                    t["task_id"],
                    encode_failure("resume_unsupported_capacity_zero", provider_id=provider_id),
                )
                await self._resume.settle_unresumable_call(t, failure=no_capacity)
                await cleanup_video_staging(t)
            return

        sem = asyncio.Semaphore(cap)

        async def _run_one(t: dict[str, Any]) -> None:
            task_id = t["task_id"]
            acquired = False
            try:
                await sem.acquire()
                acquired = True
                if self._stop_event.is_set():
                    return
                # The occupancy object is constant (the SlotTable reference is not rebuilt by reload), so promote flips PENDING->INFLIGHT directly
                # 占用对象恒定（SlotTable 引用不被 reload 重建），promote 直接翻 PENDING→INFLIGHT
                self._slots.promote(provider_id, "video", task_id)
                logger.info("已派发 resume video orphan: task_id=%s provider=%s", task_id, provider_id)
                await self._resume.run(t)
            except asyncio.CancelledError:
                # Process-level interruption landing in any of three places is caught here -- the SQL guard only lets queued / running through, guaranteeing idempotency:
                # 1) while waiting on sem.acquire -> resume hasn't started, the terminal state must be set here
                # 2) after acquired=True but outside the try block inside resume -> the internal terminal path won't fire, the terminal state must be set here
                # 3) inside resume -> the terminal state was already set internally; calling again here hits a terminal row and returns 0 rows, no side effects
                # 进程级打断落在三处都在这里兜底——SQL 守卫只放行 queued / running，保证幂等：
                # 1) sem.acquire 等待期 → 续跑还没开始，必须由此落终态
                # 2) acquired=True 后但续跑内 try 块外 → 内部落终态路径不会触发，必须由此落终态
                # 3) 续跑内部 → 内部已落终态，此处再调命中终态行返回 0 rows，无副作用
                try:
                    await asyncio.shield(self._queue.mark_task_interrupted(task_id))
                    await asyncio.shield(self._resume.settle_unresumable_call(t, cancelled=True))
                    await asyncio.shield(cleanup_video_staging(t))
                except Exception:
                    logger.exception("resume dispatch 打断后落终态失败 task_id=%s", task_id)
                raise
            finally:
                if acquired:
                    sem.release()
                self._slots.release(provider_id, "video", task_id)

        sub: list[asyncio.Task] = []
        for t in tasks:
            if self._stop_event.is_set():
                break
            # Parent coroutine synchronisation: create_task first, then immediately register as PENDING -- avoiding the transient race
            # where "create_task is scheduled asynchronously, and has_room sees occupancy=0 before scheduling happens and wrongly assumes capacity".
            # 父协程同步：先 create_task、再立即以 PENDING 登记——避免「create_task
            # 是异步调度，has_room 在调度未发生前看占用=0 误判可有容量」的瞬时 race。
            sub_task = asyncio.create_task(_run_one(t), name=f"resume-video-{t['task_id']}")
            self._slots.register(provider_id, "video", t["task_id"], sub_task, pending=True)
            sub.append(sub_task)
        if sub:
            await asyncio.gather(*sub, return_exceptions=True)
