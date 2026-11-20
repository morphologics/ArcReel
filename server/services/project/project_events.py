"""
Project data change detection and SSE fanout for workspace realtime updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib import PROJECT_ROOT
from lib.infra.content_digest import FileIdentity, file_identity
from lib.project.project_change_hints import (
    ProjectChangeBatch,
    ProjectChangeSource,
    register_project_change_batch_listener,
    register_project_change_listener,
)
from lib.project.project_manager import ProjectManager
from lib.script.draft_quarantine import DOC_TYPE_TO_QUARANTINE_KIND, quarantine_filename
from server.services.project.project_state_projection import (
    ProjectSnapshot,
    ProjectState,
    build_snapshot,
    diff_snapshots,
    draft_state_key,
)
from server.sse_channel import IDLE, DropSubscriber, SseChannel

logger = logging.getLogger(__name__)

PROJECT_EVENTS_POLL_SECONDS = 0.5

# Name of the terminal event broadcast to subscribers after the project directory is deleted; the stream ends normally
# after it (see stream_events._iter).
# 项目目录被删除后向订阅者广播的终止事件名——流在其后正常结束（见 stream_events._iter）。
PROJECT_DELETED_EVENT = "project_deleted"
# An Agent session under the project returns to running because the CLI starts a new turn on its own (woken after a
# background task completes); an open session panel receives this turn through its resident entry stream; other views
# such as the session list learn of the state change from this.
# 项目下的 Agent 会话因 CLI 自主开启新一轮（后台任务完成后唤醒）回到 running；
# 打开着的会话面板经常驻的 entry 流收到这一轮；会话列表等其他视图凭此得知状态变化。
ASSISTANT_SESSION_RESUMED_EVENT = "assistant_session_resumed"

#: Disk-reading entry point for a project's current state; called in the thread pool. Raises ``FileNotFoundError`` when
#: the project directory does not exist.
#: 读取一个项目当前状态的读盘入口；在线程池中调用。项目目录不存在时抛 ``FileNotFoundError``。
ProjectStateReader = Callable[[str], ProjectState]


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _draft_paths(project_path: Path) -> list[tuple[str, Path]]:
    """Draft files that may be present (state key -> path); every episode and every origin is listed, and whether the
    file exists is decided by the caller.

    Only handles directories under ``drafts/`` named ``episode_<digits>``; paths keep the real directory name on
    disk (``episode_007`` is not normalized to ``episode_7``), and only the episode number in the state key is taken
    as an integer.

    可能在场的草稿文件（状态键 → 路径）；各集各来源都列出，文件是否存在由调用方判定。

    只处理 ``drafts/`` 下名为 ``episode_<数字>`` 的目录；路径沿用磁盘真实目录名（``episode_007`` 不规范化成
    ``episode_7``），状态键里的集号才取整数值。
    """
    drafts_root = project_path / "drafts"
    paths: list[tuple[str, Path]] = []
    if not drafts_root.is_dir():
        return paths
    for episode_dir in sorted(drafts_root.glob("episode_*")):
        suffix = episode_dir.name.removeprefix("episode_")
        if not suffix.isdigit() or not episode_dir.is_dir():
            continue
        episode = int(suffix)
        paths.extend(
            (draft_state_key(episode, kind), episode_dir / quarantine_filename(kind))
            for kind in DOC_TYPE_TO_QUARANTINE_KIND.values()
        )
    return paths


def _read_draft_digests(project_path: Path) -> dict[str, str]:
    """Content digest of a present draft; a draft with a corrupt envelope is still present and is compared by byte
    digest.

    在场草稿的文件内容摘要；信封损坏的草稿同样在场，按字节摘要比对。"""
    digests: dict[str, str] = {}
    for key, path in _draft_paths(project_path):
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            continue
        digests[key] = hashlib.sha256(payload).hexdigest()
    return digests


def read_project_state(pm: ProjectManager, project_name: str) -> ProjectState:
    """Load project state read-only: no write-back of script migration, no episode index sync; scripts that cannot be
    parsed are skipped.

    只读加载项目状态：不回写剧本迁移、不同步集索引，无法解析的剧本跳过。"""
    project_path = pm.get_project_path(project_name)
    scripts_dir = project_path / "scripts"
    project = pm.load_project(project_name)
    scripts: dict[str, dict[str, Any]] = {}
    if scripts_dir.exists():
        for script_path in sorted(scripts_dir.glob("*.json")):
            try:
                scripts[script_path.name] = pm.load_script_readonly(project_name, script_path.name)
            except Exception:
                logger.warning("跳过无法解析的剧本快照 project=%s file=%s", project_name, script_path.name)
    return ProjectState(project=project, scripts=scripts, drafts=_read_draft_digests(project_path))


#: Stat identity of all input files of the project state: (path relative to the project directory, (device, inode,
#: size, mtime in ns)) sorted by path; absent files are not listed.
#: 项目状态全部输入文件的 stat 标识：(相对项目目录的路径, (设备, inode, 大小, 纳秒 mtime)) 按路径排序；
#: 不在场的文件不入列。
ProjectInputStamp = tuple[tuple[str, FileIdentity], ...]


def _file_stat(path: Path) -> FileIdentity | None:
    try:
        return file_identity(path.stat())
    except FileNotFoundError:
        return None


def project_input_stamp(pm: ProjectManager, project_name: str) -> ProjectInputStamp:
    """Stat identity of all input files read by :func:`read_project_state`: ``project.json``, ``scripts/*.json`` and
    present drafts. Only stats, does not read content; raises ``FileNotFoundError`` when the project directory does
    not exist.

    An unchanged identity means the inputs did not change, and polling uses it to skip re-parsing and rebuilding.
    When the disk-reading entry point gains a new input file it must be added here too, otherwise changes of that
    kind will not be found by polling.

    :func:`read_project_state` 读取的全部输入文件的 stat 标识：``project.json``、``scripts/*.json``
    与在场草稿。只做 stat，不读内容；项目目录不存在时抛 ``FileNotFoundError``。

    标识不变就说明输入没变，轮询据此跳过重新解析与重建。读盘入口新增输入文件时须同步纳入这里，
    否则那类变更不会被轮询发现。
    """
    project_path = pm.get_project_path(project_name)
    candidates = [(ProjectManager.PROJECT_FILE, project_path / ProjectManager.PROJECT_FILE)]
    scripts_dir = project_path / "scripts"
    if scripts_dir.exists():
        candidates.extend((f"scripts/{path.name}", path) for path in sorted(scripts_dir.glob("*.json")))
    candidates.extend((f"drafts:{key}", path) for key, path in _draft_paths(project_path))
    stamp: list[tuple[str, FileIdentity]] = []
    for label, path in candidates:
        identity = _file_stat(path)
        if identity is not None:
            stamp.append((label, identity))
    return tuple(stamp)


# The same event is named differently by the publisher and by the snapshot diff: when a reference-to-video task
# completes, the publisher maps it by task_type to ``reference_video_ready`` (see
# generation_tasks._SKELETON_DRIVEN_TASK_ACTIONS), while the snapshot diff represents it as ``video_ready``.
# entity_type/entity_id are the same on both sides and only action differs; without normalization the same completion
# would be broadcast twice.
# 同一件事在发布方与快照差分两侧的 action 命名差异：参考生视频任务完成时，发布方按 task_type
# 映射为 ``reference_video_ready``（见 generation_tasks._SKELETON_DRIVEN_TASK_ACTIONS），而快照
# 差分表示为 ``video_ready``。两侧 entity_type/entity_id 相同，
# 只有 action 不同，不归一会让同一次完成广播成两条。
_EQUIVALENT_ACTIONS: dict[str, str] = {"reference_video_ready": "video_ready"}


def _change_identity(change: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Dedup identity of a change.

    Only entity_type/action/entity_id are taken: script_file and episode are attached selectively by the publisher
    depending on task_type, and including them in the identity would count the same event twice when one side is
    None. action is normalized via ``_EQUIVALENT_ACTIONS`` to smooth out naming differences between the publisher
    and the snapshot diff for the same event.

    变更去重身份。

    只取 entity_type/action/entity_id：script_file 与 episode 由发布方按 task_type 选择性
    附带，纳入身份会让同一件事因一侧为 None 而判成两条。action 经 ``_EQUIVALENT_ACTIONS``
    归一，抹平发布方与快照差分对同一件事的命名差异。
    """
    action = change.get("action")
    if isinstance(action, str):
        action = _EQUIVALENT_ACTIONS.get(action, action)
    return (change.get("entity_type"), action, change.get("entity_id"))


@dataclass
class _ProjectChannel:
    sse: SseChannel
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    scan_now: asyncio.Event = field(default_factory=asyncio.Event)
    # Serialize the read-modify-write of "read disk -> settle baseline -> broadcast": when a polling scan and an
    # explicit rebuild run concurrently, different read times can make the side that read the older disk state settle
    # later, writing a stale snapshot back as the baseline and diffing out reverse changes (entities that actually
    # exist broadcast as deleted). This lock is not reset: a rebuild task may still hold it across a watch task
    # restart.
    # 串行化「读盘 → 结算基线 → 广播」这段读-改-写：轮询扫描与显式重建并发时，读盘耗时
    # 不同会让先读到旧盘的一方后结算，把陈旧快照写回基线并 diff 出反向变更（实际存在的
    # 实体被广播成 deleted）。不重置该锁——重建 task 可能跨 watch task 重启仍持有它。
    rebuild_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Change identities described by each explicit batch that has been published but not yet broadcast. A rebuild read
    # picks up artifacts other batches have already written, and the catch-up scan must leave those to their own batch
    # to broadcast; otherwise the same event is sent once by the catch-up and again by its batch.
    # 已发布、尚未广播的显式批次各自描述的变更身份。重建读盘会捎带其它批次已落盘的产物，
    # 补扫需把它们让给自己那一批去广播，否则同一件事先被补扫发一次、再被本批发一次。
    inflight_batch_identities: Counter[tuple[Any, Any, Any]] = field(default_factory=Counter)
    pending_sources: set[ProjectChangeSource] = field(default_factory=set)
    task: asyncio.Task | None = None
    snapshot: ProjectSnapshot | None = None
    # Input stat identity before the ``snapshot`` disk read, updated together with ``snapshot``; polling uses it to
    # skip rebuilding when inputs have not changed.
    # ``snapshot`` 读盘前的输入 stat 标识，与 ``snapshot`` 同时更新；轮询据此在输入未变时跳过重建。
    input_stamp: ProjectInputStamp | None = None


class ProjectEventService:
    def __init__(
        self,
        project_root: Path | None = None,
        *,
        data_root: Path | None = None,
        poll_interval: float = PROJECT_EVENTS_POLL_SECONDS,
        read_state: ProjectStateReader | None = None,
    ):
        """``read_state`` defaults to :func:`read_project_state` (read-only load through this service's ``pm``).

        ``read_state`` 缺省为 :func:`read_project_state`（经本服务的 ``pm`` 只读加载）。"""
        self.project_root = Path(project_root or PROJECT_ROOT)
        # An explicitly passed ``data_root`` takes precedence (production entry points pass the configured data root);
        # otherwise use the default data root (``projects/`` under the repo root) for compatibility with test
        # fixtures.
        # 显式传入 ``data_root`` 时优先使用（生产入口传配置的数据根），
        # 否则取默认数据根（仓库根下的 ``projects/``）兼容测试 fixture。
        resolved_data_root = (
            Path(data_root).resolve(strict=False) if data_root is not None else self.project_root / "projects"
        )
        self.pm = ProjectManager(resolved_data_root)
        self._read_state: ProjectStateReader = read_state or (lambda name: read_project_state(self.pm, name))
        self.poll_interval = max(0.1, float(poll_interval))
        self._channels: dict[str, _ProjectChannel] = {}
        self._listener_unregister = None
        self._batch_listener_unregister = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_batch_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._listener_unregister is not None or self._batch_listener_unregister is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._listener_unregister = register_project_change_listener(self._on_hint)
        self._batch_listener_unregister = register_project_change_batch_listener(self._on_batch_hint)

    async def shutdown(self) -> None:
        unregister = self._listener_unregister
        self._listener_unregister = None
        if unregister is not None:
            unregister()
        batch_unregister = self._batch_listener_unregister
        self._batch_listener_unregister = None
        if batch_unregister is not None:
            batch_unregister()

        tasks = [channel.task for channel in self._channels.values() if channel.task is not None]
        tasks.extend(self._pending_batch_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending_batch_tasks.clear()
        self._channels.clear()
        self._loop = None

    def _create_channel(self, project_name: str) -> _ProjectChannel:
        """Build the project channel: overflow policy "remove subscriber"; first/last subscriber hooks start and stop
        the background scan.

        构造项目通道：溢出策略「移除订阅者」，首/末订阅者钩子启停后台扫描。"""
        sse = SseChannel(
            overflow=DropSubscriber(
                on_removed=lambda count: logger.warning(
                    "项目事件订阅队列溢出，移除 %s 个订阅者 project=%s",
                    count,
                    project_name,
                ),
            ),
            on_first_subscriber=lambda: self._start_watch(project_name),
            on_last_subscriber=lambda: self._stop_watch(project_name),
        )
        return _ProjectChannel(sse=sse)

    def _start_watch(self, project_name: str) -> None:
        """First-subscriber hook: start (or restart one that has exited on its own) the background scan task.

        When overflow removes the last subscriber, the watch task exits on its own via ``while has_subscribers``
        while the channel stays in the registry, so the restart condition is "task not running" rather than only
        "first subscription".

        首订阅者钩子：启动（或重启已自行退出的）后台扫描任务。

        溢出移除掉最后一个订阅者时 watch task 经 ``while has_subscribers`` 自行
        退出而通道仍留在注册表，故重启条件是「任务不在跑」而非仅「首次订阅」。
        """
        channel = self._channels.get(project_name)
        if channel is None:
            return
        if channel.task is not None and not channel.task.done():
            return
        channel.ready_event = asyncio.Event()
        channel.scan_now = asyncio.Event()
        channel.pending_sources.clear()
        channel.task = asyncio.create_task(
            self._watch_project(project_name, channel),
            name=f"project-events-{project_name}",
        )

    async def _stop_watch(self, project_name: str) -> None:
        """Last-subscriber hook: stop the background scan task and unregister the channel.

        Remove the channel from the registry first, then await the watch task exit: there is no yield point between
        removing and fetching, so the channel removed is exactly the current one. When the event loop is yielded
        during teardown, a new subscriber coming in concurrently cannot get this dying channel; it creates and
        registers a separate channel and is not removed along with this teardown's deletion.

        末订阅者钩子：停止后台扫描任务并注销通道。

        先从注册表摘除通道再 await watch task 退出——摘除与取回之间无让出点，
        摘的正是当前通道。收尾期间让出事件循环时，并发进入的新订阅者取不到这个
        将死通道，会新建独立通道注册入表，不会被本次收尾的删除连带摘掉。
        """
        channel = self._channels.pop(project_name, None)
        if channel is None:
            return
        task = channel.task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _subscribe(self, project_name: str) -> tuple[SseChannel, asyncio.Queue, dict[str, Any]]:
        """Register a queue for *project_name* and return it with the initial snapshot.

        Private: the only consumer is :meth:`stream_events`, which owns the
        deterministic unsubscribe via its context-manager ``__aexit__``.
        """
        await asyncio.to_thread(self.pm.get_project_path, project_name)
        channel = self._channels.get(project_name)
        if channel is None:
            channel = self._create_channel(project_name)
            self._channels[project_name] = channel

        # The queue is registered before the first scan starts (the first-subscriber hook fires after registration);
        # otherwise events broadcast between scan completion and registration would be missed.
        # 队列在首次扫描启动前注册(首订阅者钩子在注册后触发)，否则会漏掉
        # 扫描完成到注册之间广播的事件。
        queue = channel.sse.subscribe()

        try:
            await channel.ready_event.wait()
        except BaseException:
            # A client disconnecting during the first scan cancels here: at that point _subscribe has not returned the
            # queue yet, so the try/finally of stream_events is never entered. Synchronously clean up the subscriber
            # just registered (on an idle project the watch task does not self-heal), without awaiting to avoid
            # cancellation re-entry: bypass the async last-subscriber hook and handle teardown ourselves.
            # 客户端在首次扫描期间断开会取消这里:此时 _subscribe 尚未返回 queue,
            # stream_events 的 try/finally 进不去。同步清理掉刚注册的订阅者(空闲项目
            # 下 watch task 不会自愈),不 await 以免取消重入——绕过异步末位钩子，
            # 收尾自理。
            if channel.sse.unsubscribe_nowait(queue) and channel.task is not None:
                channel.task.cancel()
                self._channels.pop(project_name, None)
            raise
        return channel.sse, queue, self._build_snapshot_payload(project_name, channel)

    async def _unsubscribe(self, project_name: str, queue: asyncio.Queue) -> None:
        """Remove a queue; the last-subscriber hook stops the watch task."""
        channel = self._channels.get(project_name)
        if channel is None:
            return
        await channel.sse.unsubscribe(queue)

    @contextlib.asynccontextmanager
    async def stream_events(
        self, project_name: str, *, idle_timeout: float = 1.0
    ) -> AsyncGenerator[AsyncIterator[tuple[str, Any] | dict[str, Any]]]:
        """Subscribe to a project's events as a self-cleaning async iterator.

        Yields an async iterator producing, in order:

        - a ``("snapshot", payload)`` tuple as the first event (initial state),
        - live ``(event_name, payload)`` tuples as changes are broadcast,
        - a ``{"type": "_idle"}`` sentinel whenever *idle_timeout* elapses with no
          event (consumers poll disconnect on it).

        When the subscriber's queue fills, :class:`DropSubscriber` removes it and
        the iterator ends; the client reconnects and the fresh snapshot realigns
        it. Subscription and unsubscribe live behind this seam; cleanup is
        carried by ``__aexit__`` (see ADR-0005). Consume as
        ``async with stream_events(...) as stream: async for item in stream``.
        """
        sse, queue, snapshot = await self._subscribe(project_name)

        async def _iter() -> AsyncIterator[tuple[str, Any] | dict[str, Any]]:
            # NOTE: intentionally NO ``finally: _unsubscribe`` here — cleanup is owned
            # by the enclosing context manager's __aexit__ (ADR-0005). Do not add one.
            yield ("snapshot", snapshot)
            async for item in sse.iterate(queue, idle_timeout=idle_timeout):
                yield {"type": "_idle"} if item is IDLE else item
                # The project was deleted: the stream ends normally after the terminal event, no longer waiting for
                # the next broadcast or idle heartbeat.
                # 项目已被删除：终止事件之后流正常结束，不再等待下一条广播或空闲心跳。
                if isinstance(item, tuple) and item[0] == PROJECT_DELETED_EVENT:
                    return

        try:
            yield _iter()
        finally:
            await self._unsubscribe(project_name, queue)

    def publish_assistant_session_resumed(self, project_name: str, session_id: str) -> None:
        """Broadcast to project subscribers that a session returned to running; dropped when there are no subscribers.
        Must be called on the event loop thread.

        向项目订阅者广播会话回到 running；没有订阅者时丢弃。须在事件循环线程调用。"""
        channel = self._channels.get(project_name)
        if channel is None:
            return
        payload = {"project_name": project_name, "session_id": session_id, "status": "running"}
        channel.sse.broadcast((ASSISTANT_SESSION_RESUMED_EVENT, payload))

    def _on_hint(
        self,
        project_name: str,
        source: ProjectChangeSource,
        changed_paths: tuple[str, ...],
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            self._apply_hint,
            project_name,
            source,
            changed_paths,
        )

    def _on_batch_hint(
        self,
        project_name: str,
        source: ProjectChangeSource,
        changes: tuple[ProjectChangeBatch, ...],
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            self._apply_emitted_batch,
            project_name,
            source,
            changes,
        )

    def _apply_hint(
        self,
        project_name: str,
        source: ProjectChangeSource,
        changed_paths: tuple[str, ...],
    ) -> None:
        channel = self._channels.get(project_name)
        if channel is None:
            return
        channel.pending_sources.add(source)
        channel.scan_now.set()
        logger.debug(
            "项目变更 hint project=%s source=%s paths=%s",
            project_name,
            source,
            changed_paths,
        )

    def _apply_emitted_batch(
        self,
        project_name: str,
        source: ProjectChangeSource,
        changes: tuple[ProjectChangeBatch, ...],
    ) -> None:
        channel = self._channels.get(project_name)
        if channel is None or not changes:
            return

        channel.scan_now.clear()

        # Registration must happen before this batch's rebuild read, so other batches' catch-up scans can leave these
        # identities alone
        # 在册时机必须早于本批重建读盘，其它批次的补扫才让得掉这些身份
        identities = Counter(_change_identity(change) for change in changes)
        channel.inflight_batch_identities.update(identities)

        # File I/O goes down to the thread pool; state updates and broadcasting stay on the event loop / 文件 I/O 下沉到线程池，状态更新和广播留在事件循环
        task = asyncio.create_task(
            self._async_rebuild_and_broadcast(project_name, channel, source, changes, identities),
            name=f"batch-rebuild-{project_name}",
        )
        self._pending_batch_tasks.add(task)
        task.add_done_callback(self._pending_batch_tasks.discard)

    async def _async_rebuild_and_broadcast(
        self,
        project_name: str,
        channel: _ProjectChannel,
        source: ProjectChangeSource,
        changes: tuple[ProjectChangeBatch, ...],
        identities: Counter[tuple[Any, Any, Any]],
    ) -> None:
        """File I/O runs in a thread; state updates and broadcasting run on the event loop thread.

        文件 I/O 在线程中执行，状态更新和广播在事件循环线程中执行。"""
        try:
            async with channel.rebuild_lock:
                # Swap out the whole set of origin markers registered before the read: hints added during the read go
                # into the new set, take part in this round's settlement, and are also kept for the next scan. If
                # instead the old set were subtracted after the read, repeated arrivals of the same origin within the
                # window would be subtracted too, and that new change would be mislabeled filesystem by
                # _resolve_batch_source in the next scan.
                # 读盘前在册的来源标记整体换出：读盘期间新增的 hint 落进新集合，参与本轮结算，
                # 同时留给下一轮扫描。若改为读盘后减去旧集合，同一来源在窗口内重复到达会被一并
                # 减掉，那条新变更会在下一轮扫描被 _resolve_batch_source 误标成 filesystem。
                covered_sources = channel.pending_sources
                channel.pending_sources = set()
                try:
                    stamp, snapshot = await asyncio.to_thread(self._rebuild_snapshot, project_name)
                except FileNotFoundError:
                    channel.pending_sources |= covered_sources
                    await self._handle_scan_file_not_found(
                        project_name, channel, log_message="Failed to build explicit project event snapshot project=%s"
                    )
                    return
                except Exception:
                    channel.pending_sources |= covered_sources
                    logger.exception("构建显式项目事件快照失败 project=%s", project_name)
                    return

                # The following runs on the event loop thread and is thread-safe / 以下在事件循环线程中执行，线程安全
                previous = channel.snapshot
                channel.snapshot = snapshot
                channel.input_stamp = stamp

                self._broadcast_changes(
                    project_name,
                    channel,
                    fingerprint=snapshot.fingerprint,
                    source=source,
                    changes=[dict(change) for change in changes],
                )

                swept = self._sweep_uncovered_changes(previous, snapshot, channel.inflight_batch_identities)
                if swept:
                    # Changes from the catch-up scan do not belong to this batch; their origin is resolved from their
                    # own hint markers, the same convention as the polling scan: reusing this batch's source would
                    # label the WebUI's own edits as worker (the frontend then pops a notification and navigates
                    # automatically), or conversely miss notifications for worker artifacts.
                    # 补扫的变更不属于本批，来源按自己的 hint 标记解析，与轮询扫描同一口径：
                    # 沿用本批 source 会让 WebUI 自身的编辑被标成 worker（前端据此弹通知并自动
                    # 导航），或反之漏掉 worker 产物的通知。
                    self._broadcast_changes(
                        project_name,
                        channel,
                        fingerprint=snapshot.fingerprint,
                        source=self._settled_source(channel, covered_sources),
                        changes=swept,
                    )
        finally:
            channel.inflight_batch_identities -= identities

    def _broadcast_changes(
        self,
        project_name: str,
        channel: _ProjectChannel,
        *,
        fingerprint: str,
        source: ProjectChangeSource,
        changes: list[dict[str, Any]],
    ) -> None:
        """Broadcast a batch of changes to subscribers (the polling scan and explicit rebuild paths share the same
        payload shape).

        向订阅者广播一批变更（轮询扫描与显式重建两条路径共用同一 payload 形状）。"""
        payload = {
            "project_name": project_name,
            "batch_id": uuid.uuid4().hex,
            "fingerprint": fingerprint,
            "generated_at": _utc_now_iso(),
            "source": source,
            "changes": changes,
        }
        channel.sse.broadcast(("changes", payload))

    def _sweep_uncovered_changes(
        self,
        previous: ProjectSnapshot | None,
        snapshot: ProjectSnapshot,
        covered: Counter[tuple[Any, Any, Any]],
    ) -> list[dict[str, Any]]:
        """Changes the new snapshot has beyond the baseline that no in-flight explicit batch describes.

        A rebuild read picks up any change already on disk, while this batch's changes only describe the publisher's
        own event. The new snapshot is written back as the baseline unconditionally; if a picked-up change is not in
        this broadcast, later scans see no difference from the baseline and no mechanism would ever send it. So diff
        against the baseline here once and send the uncovered part.

        ``covered`` is the registry of in-flight batch identities (including this batch); these identities are left
        for their own batch to broadcast, so the same event is not sent once by the catch-up and again by its own
        batch. Remaining window: if a batch's artifact is already on disk but its emit call comes after this read
        returns, the identity is not registered yet and will still be sent twice.

        新快照相对基线多出、而任何在途显式批次都未描述的变更。

        重建读盘会捎带任何已落盘的变更，而本批 changes 只描述发布方自己那一件事。
        新快照被无条件写回基线，捎带进来的变更若不在本次广播里，后续扫描与基线已无
        差异，就再没有任何机制为它补发——故在此对基线做一次 diff 补发未覆盖的部分。

        ``covered`` 是在途批次身份登记表（含本批自己），这些身份让给各自那一批广播，
        免得同一件事被补扫先发一次、再被它自己的批次发一次。残留窗口：某批次的产物已
        落盘、而它的 emit 调用晚于本次读盘返回，此时该身份尚未在册，仍会重复一次。
        """
        if previous is None:
            return []
        return [change for change in diff_snapshots(previous, snapshot) if _change_identity(change) not in covered]

    def _rebuild_snapshot(self, project_name: str) -> tuple[ProjectInputStamp, ProjectSnapshot]:
        """Synchronous method (runs in the thread pool): load the project state read-only and normalize it into a
        snapshot, together with the input identity from before the read.

        The identity is taken before the read: changes written during the read only make the next round's identity
        comparison count as changed and rebuild once more; they never record a newer disk state as the snapshot
        under the old identity.

        同步方法（在线程池中执行）：只读加载项目状态并归一成快照，连同读盘前的输入标识。

        标识取在读盘之前：读盘期间落盘的变更只会让下一轮标识比对判为已变，多重建一次，
        不会把更新的盘面误记成旧标识下的快照。
        """
        stamp = project_input_stamp(self.pm, project_name)
        return stamp, build_snapshot(self._read_state(project_name))

    def _poll_snapshot(
        self, project_name: str, known_stamp: ProjectInputStamp | None
    ) -> tuple[ProjectInputStamp, ProjectSnapshot | None]:
        """Synchronous method (runs in the thread pool): the rebuild used by polling; skipped when the input identity
        equals ``known_stamp``, in which case the snapshot is ``None``.

        同步方法（在线程池中执行）：轮询用的重建——输入标识与 ``known_stamp`` 一致时跳过，快照返回 ``None``。"""
        stamp = project_input_stamp(self.pm, project_name)
        if stamp == known_stamp:
            return stamp, None
        return stamp, build_snapshot(self._read_state(project_name))

    def _project_directory_gone(self, project_name: str) -> bool:
        """Decide whether the project directory really no longer exists right now (``get_project_path`` semantics).

        Used by the scan / hint rebuild paths after catching ``FileNotFoundError`` for an independent check of the
        current state, distinguishing it from "a deep file such as project.json is missing but the directory is
        still there"; the latter keeps the status quo, is logged as ERROR by the generic exception fallback, and
        does not trigger termination. A recheck is used rather than a one-time check at scan start because directory
        deletion (e.g. ``shutil.rmtree``) is itself not atomic: the scan may hit ``FileNotFoundError`` at any
        intermediate state of the deletion (e.g. project.json removed before the directory itself), and a start-time
        check would misjudge it as "not deleted"; the recheck reflects the actual state after the exception.

        判定项目目录当前是否确已不存在（``get_project_path`` 语义）。

        供扫描 / hint 重建路径捕获 ``FileNotFoundError`` 后做一次独立的现状复核，
        与「project.json 等深层文件缺失但目录仍在」区分——后者维持现状，按通用
        异常兜底记 ERROR，不触发终止流程。用复核而非扫描起点的一次性判断，是因为
        目录删除（如 ``shutil.rmtree``）本身非原子：扫描可能在删除过程中的任意
        中间状态命中 ``FileNotFoundError``（如 project.json 先于目录本身被移除），
        起点检查会误判为「未删除」；复核反映的是异常发生后的当前实况。
        """
        try:
            self.pm.get_project_path(project_name)
        except FileNotFoundError:
            return True
        return False

    def _handle_project_deleted(self, project_name: str, channel: _ProjectChannel) -> None:
        """The project directory has been deleted: terminate the channel: broadcast the terminal event, remove it from
        the registry, cancel the watch task.

        Both the polling scan and the hint rebuild paths may independently detect the same deletion and land in this
        method; whether this is the first termination is decided by "is this channel still the current channel in
        the registry", avoiding duplicate broadcasts/logs and also avoiding killing a new channel registered after a
        project with the same name was recreated.

        项目目录已被删除：终止该通道——广播终止事件、移出注册表、取消 watch task。

        轮询扫描与 hint 重建两条路径都可能独立探测到同一次删除并落到本方法；
        按「本通道是否仍是注册表现行通道」判定是否为首次终止，避免重复广播/
        重复日志，也避免误杀同名项目重建后已注册的新通道。
        """
        if self._channels.get(project_name) is not channel:
            return
        self._channels.pop(project_name, None)
        channel.sse.broadcast((PROJECT_DELETED_EVENT, {"project_name": project_name}))
        logger.info("项目已被删除，终止事件流 project=%s", project_name)
        task = channel.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _handle_scan_file_not_found(
        self, project_name: str, channel: _ProjectChannel, *, log_message: str
    ) -> bool:
        """Common handling after the scan / hint rebuild paths catch ``FileNotFoundError``: recheck whether the
        directory really is gone; if so, terminate the channel and return ``True``; otherwise keep the status quo,
        log ERROR as the fallback and return ``False``.

        Shared by the two independent paths :meth:`_async_rebuild_and_broadcast` and :meth:`_watch_project`, so they
        do not each keep a copy of the same decision logic and miss one when the condition changes later.

        扫描 / hint 重建路径捕获 ``FileNotFoundError`` 后的统一处理：复核目录是否确已
        消失，是则终止通道并返回 ``True``；否则维持现状按 ERROR 兜底并返回 ``False``。

        供 :meth:`_async_rebuild_and_broadcast` 与 :meth:`_watch_project` 两条独立路径
        共用，避免各自维护一份相同判定逻辑、日后修改判定条件时漏改其中一处。
        """
        if await asyncio.to_thread(self._project_directory_gone, project_name):
            self._handle_project_deleted(project_name, channel)
            return True
        logger.exception(log_message, project_name)
        return False

    async def _watch_project(self, project_name: str, channel: _ProjectChannel) -> None:
        # The first round and hint-woken scans read and rebuild as usual; only timed polling skips when the input
        # identity is unchanged.
        # 首轮与 hint 唤醒的扫描照常读盘重建；只有定时轮询才凭输入标识不变而跳过。
        forced = True
        try:
            while channel.sse.has_subscribers:
                try:
                    async with channel.rebuild_lock:
                        known_stamp = None if forced or channel.snapshot is None else channel.input_stamp
                        # Same convention as the explicit rebuild: origin markers registered before the read are
                        # swapped out and consumed by this round's settlement; hints arriving during the read go into
                        # the new set, take part in this round and are kept for the next. The read may come before
                        # that write; if they were cleared with this round, the next round reading that write would
                        # have no marker left and label it filesystem.
                        # 与显式重建同一口径：读盘前在册的来源标记换出，由本轮结算消费；读盘期间
                        # 到达的 hint 落进新集合，参与本轮结算并留给下一轮。读盘可能早于那次写入，
                        # 随本轮清掉的话，下一轮读到这次写入时已没有标记，会被标成 filesystem。
                        covered_sources = channel.pending_sources
                        channel.pending_sources = set()
                        try:
                            # Only file I/O runs in a thread / 仅文件 I/O 在线程中执行
                            stamp, snapshot = await asyncio.to_thread(self._poll_snapshot, project_name, known_stamp)
                        except BaseException:
                            channel.pending_sources |= covered_sources
                            raise
                        # When inputs are unchanged the snapshot is reused, and the swapped-out origin markers are
                        # settled with it (same convention as a scan with no differences)
                        # 输入未变时快照沿用，换出的来源标记随之结算（与无差异的扫描同一口径）
                        if snapshot is not None:
                            # State updates and broadcasting run on the event loop thread (thread-safe) / 状态更新和广播在事件循环线程中执行（线程安全）
                            channel.input_stamp = stamp
                            self._apply_scan_result(project_name, channel, snapshot, covered_sources)
                except asyncio.CancelledError:
                    raise
                except FileNotFoundError:
                    if await self._handle_scan_file_not_found(
                        project_name, channel, log_message="Project event scan failed project=%s"
                    ):
                        return
                except Exception:
                    logger.exception("项目事件扫描失败 project=%s", project_name)
                finally:
                    channel.ready_event.set()

                try:
                    await asyncio.wait_for(channel.scan_now.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    # A hint that arrives after the timeout cancels the wait but before this coroutine resumes only
                    # leaves the event set, and is likewise rebuilt as a hint.
                    # 超时取消等待之后、本协程恢复之前到达的 hint 只留下置位的事件，同样按 hint 重建。
                    forced = channel.scan_now.is_set()
                else:
                    forced = True
                finally:
                    channel.scan_now.clear()
        except asyncio.CancelledError:
            raise

    def _apply_scan_result(
        self,
        project_name: str,
        channel: _ProjectChannel,
        snapshot: ProjectSnapshot,
        covered_sources: set[ProjectChangeSource],
    ) -> None:
        """Update channel state and broadcast changes on the event loop thread.

        在事件循环线程中更新 channel 状态并广播变更。"""
        previous = channel.snapshot
        channel.snapshot = snapshot
        source = self._settled_source(channel, covered_sources)
        if previous is None or previous.fingerprint == snapshot.fingerprint:
            return

        changes = diff_snapshots(previous, snapshot)
        if not changes:
            return

        self._broadcast_changes(
            project_name,
            channel,
            fingerprint=snapshot.fingerprint,
            source=source,
            changes=changes,
        )

    def _build_snapshot_payload(
        self,
        project_name: str,
        channel: _ProjectChannel,
    ) -> dict[str, Any]:
        return {
            "project_name": project_name,
            "fingerprint": channel.snapshot.fingerprint if channel.snapshot is not None else "",
            "generated_at": _utc_now_iso(),
        }

    @classmethod
    def _settled_source(
        cls, channel: _ProjectChannel, covered_sources: set[ProjectChangeSource]
    ) -> ProjectChangeSource:
        """Origins of one read round: markers swapped out before the read, plus markers delivered during the read that
        are still kept for the next round.

        一轮读盘的来源：读盘前换出的标记，加上读盘期间已送达、仍留给下一轮的标记。"""
        return cls._resolve_batch_source(covered_sources | channel.pending_sources)

    @staticmethod
    def _resolve_batch_source(
        pending_sources: set[ProjectChangeSource],
    ) -> ProjectChangeSource:
        if "worker" in pending_sources:
            return "worker"
        if "webui" in pending_sources:
            return "webui"
        return "filesystem"
