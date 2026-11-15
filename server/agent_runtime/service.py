"""
Assistant service orchestration using ClaudeSDKClient.
"""

import asyncio
import copy
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from claude_agent_sdk import (
    delete_session as sdk_delete_session,
)
from claude_agent_sdk import (
    delete_session_via_store,
    list_sessions_from_store,
)
from claude_agent_sdk import (
    list_sessions as sdk_list_sessions,
)

if TYPE_CHECKING:
    from server.routers.assistant import ImageAttachment

logger = logging.getLogger(__name__)

from fastapi import Request
from fastapi.sse import ServerSentEvent

from lib.agent.agent_profile import agent_profile_dir
from lib.agent.profile_frontmatter import FrontmatterError, parse_profile_metadata
from lib.agent.profile_manifest import VALID_CONTENT_MODES
from lib.i18n import DEFAULT_LOCALE
from lib.infra.async_thread import run_sync_transaction
from lib.infra.data_root_layout import DataRootLayout
from lib.project.project_manager import ProjectManager
from server.agent_runtime.event_log import (
    EventLogService,
    EventLogStore,
    build_failure_entry,
    build_user_entry,
)
from server.agent_runtime.keyed_locks import KeyedLocks
from server.agent_runtime.models import (
    Heartbeat,
    LiveMessage,
    SessionMeta,
    SessionStatus,
    SubscriptionReady,
    WithdrawalIntent,
)
from server.agent_runtime.sdk_transcript_adapter import SdkTranscriptAdapter
from server.agent_runtime.session_branch import (
    BranchAnchorError,
    BranchedSession,
    SessionBranchError,
    SessionBranchService,
)
from server.agent_runtime.session_manager import SessionManager
from server.agent_runtime.session_store import SessionMetaStore


class MessageRewriteError(RuntimeError):
    """The message rewrite cannot proceed.

    消息改写无法进行。"""


class RewriteAnchorError(MessageRewriteError):
    """The anchor is not a user message in this session.

    锚点不是该会话内的一条用户消息。"""


class PendingQuestionError(MessageRewriteError):
    """The session has a pending question card; questions take priority.

    会话有未决问答卡片，问答优先。"""


class QueuedMessagesPendingError(MessageRewriteError):
    """The session still has queued messages that have not entered the conversation: if forked now, they would keep
    running in the superseded original session.

    会话还有排队消息没进入对话：此刻分叉，它们会在被取代的原会话里继续执行。"""


class SessionSupersededError(MessageRewriteError):
    """The session has been superseded by a rewrite; the rewrite entry point is only open to the current branch.

    会话已被一次改写取代，改写入口只对当前分支开放。"""


class RewriteUnavailableError(MessageRewriteError):
    """Rewriting is not possible in the current deployment (transcript mirroring is not enabled).

    当前部署形态下无法改写（transcript 镜像未开启）。"""


class InterruptSettleTimeoutError(MessageRewriteError):
    """Timed out waiting for the running turn to be interrupted to a terminal state.

    等待运行中轮次中断到终态超时。"""


class AssistantService:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.layout = DataRootLayout.current()
        self.data_root = self.layout.root

        self.pm = ProjectManager(self.data_root)
        self.meta_store = SessionMetaStore()
        # Session event log: the single read source of the UI timeline. The store shares the same instance with
        # SessionManager, so the live write point (entry pipeline) and the read side (REST / SSE / lazy generation)
        # use the same table.
        # 会话事件日志：UI 时间线唯一读源。store 与 SessionManager 共享同一实例，
        # live 写入点（entry pipeline）与读取端（REST / SSE / 懒生成）落同一张表。
        self.event_log_store = EventLogStore()
        self.session_manager = SessionManager(
            project_root=self.project_root,
            meta_store=self.meta_store,
            data_root=self.data_root,
            event_log_store=self.event_log_store,
        )
        # Shared with SessionManager (lazy-cached there) so reads via the
        # adapter and writes via SDK options use the same per-user namespace.
        # None when ARCREEL_SDK_SESSION_STORE=off.
        self._session_store = self.session_manager._build_session_store()
        self.transcript_adapter = SdkTranscriptAdapter(store=self._session_store)
        self.event_log = EventLogService(self.event_log_store, self.transcript_adapter)
        self.session_branch = SessionBranchService(
            store=self._session_store,
            meta_store=self.meta_store,
            event_log=self.event_log,
            resolve_project_cwd=self._resolve_project_cwd_safe,
        )
        self._startup_lock = asyncio.Lock()
        self._startup_done = False
        # Idempotency mapping for new sessions: the client_key unique index is partitioned by (session_id, client_key)
        # and does not cover acceptance of a new session whose session_id does not exist yet -- a retry after a lost
        # response that goes down the new-session branch again would create a duplicate session and execute the same
        # prompt twice. The in-process LRU is the fast path; after a restart / eviction the event log's cross-session
        # query is the fallback (_find_accepted_new_session).
        # 新会话幂等映射：client_key 唯一索引按 (session_id, client_key) 分区，
        # 覆盖不到 session_id 尚不存在的新会话受理——响应丢失后的重试若再走
        # 新会话分支会重复建会话、重复执行同一 prompt。进程内 LRU 为快路径，
        # 重启 / 淘汰后由事件日志的跨会话查询兜底（_find_accepted_new_session）。
        self._new_session_client_keys: OrderedDict[str, str] = OrderedDict()
        self._new_session_client_keys_max = 256
        # Concurrent new-session requests with the same client_key are serialized here, to avoid creating duplicate
        # sessions in the in-flight window
        # 同一 client_key 的并发新建请求在此串行化，避免在途窗口内重复建会话
        self._new_session_locks = KeyedLocks()
        # Concurrent rewrite requests for the same (original session, client_key) are serialized here: the fork's
        # idempotency pre-check reads the original session's superseded pointer, and without serializing the in-flight
        # window would let two requests each fork once.
        # 同一（原会话, client_key）的并发改写请求在此串行化：分叉的幂等预检读的是
        # 原会话的 superseded 指针，在途窗口内不串行会让两个请求各自分叉一次。
        self._rewrite_locks = KeyedLocks()
        # Message acceptance lock for existing sessions: send and rewrite are mutually exclusive. A rewrite holds it
        # from the queue check until the branch is published; a send arriving meanwhile waits until it ends, sees the
        # original session has been superseded, and does not get queued into a session about to be superseded.
        # 已有会话的消息受理锁：发送与改写互斥。改写从排队检查持有到分支发布，期间到达的发送
        # 等它结束后看到原会话已被取代，不会排进一个即将被取代的会话。
        self._admission_locks = KeyedLocks()
        self.stream_heartbeat_seconds = int(os.environ.get("ASSISTANT_STREAM_HEARTBEAT_SECONDS", "20"))

    async def startup(self, *, in_docker: bool = False, sandbox_enabled: bool = True) -> None:
        """Run async initialization (must be called from event loop).

        With ``sandbox_enabled=False`` the SDK SandboxSettings are turned off and Bash tool calls switch to the code
        allowlist path (see ``SessionManager.configure_sandbox_runtime``). The default ``True`` keeps macOS / Linux
        behavior unchanged.

        Run async initialization (must be called from event loop).

        ``sandbox_enabled=False`` 时关闭 SDK SandboxSettings 并把 Bash 工具调用
        切到代码白名单路径（详见 ``SessionManager.configure_sandbox_runtime``）。
        默认 ``True`` 保持 macOS / Linux 现状不变。
        """
        if self._startup_done:
            return
        async with self._startup_lock:
            if self._startup_done:
                return
            self.session_manager.configure_sandbox_runtime(
                in_docker=bool(in_docker),
                sandbox_enabled=bool(sandbox_enabled),
            )
            await self._interrupt_stale_running_sessions()
            self._startup_done = True

    # ==================== Session CRUD ====================

    async def _interrupt_stale_running_sessions(self) -> None:
        """On service restart, stale running sessions cannot safely resume."""
        interrupted_count = await self.meta_store.interrupt_running_sessions()
        if interrupted_count > 0:
            logger.warning(
                "服务启动时中断遗留运行中会话 count=%s",
                interrupted_count,
            )

    async def list_sessions(
        self,
        project_name: str | None = None,
        status: SessionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionMeta]:
        """List sessions, injecting SDK summary as title when available."""
        sessions = await self.meta_store.list(project_name=project_name, status=status, limit=limit, offset=offset)
        if not sessions or not project_name:
            return sessions

        project_cwd = str(self.layout.projects_dir / project_name)
        sdk_sessions: list[Any] = []

        if self._session_store is not None and list_sessions_from_store is not None:
            try:
                sdk_sessions = await list_sessions_from_store(self._session_store, directory=project_cwd)
            except Exception:
                logger.warning(
                    "SDK list_sessions_from_store failed, titles will be empty",
                    exc_info=True,
                )
                return sessions
        elif sdk_list_sessions is not None:
            try:
                sdk_sessions = await asyncio.to_thread(
                    sdk_list_sessions, directory=project_cwd, include_worktrees=False
                )
            except Exception:
                logger.warning("SDK list_sessions failed, titles will be empty", exc_info=True)
                return sessions
        else:
            return sessions

        summary_map = {s.session_id: s.summary for s in sdk_sessions}
        return [SessionMeta(**{**s.model_dump(), "title": summary_map.get(s.id, s.title)}) for s in sessions]

    async def get_session(self, session_id: str) -> SessionMeta | None:
        """Get session by ID."""
        meta = await self.meta_store.get(session_id)
        if meta and session_id in self.session_manager.sessions:
            # Update status from live session
            managed = self.session_manager.sessions[session_id]
            meta = SessionMeta(**{**meta.model_dump(), "status": managed.status})
        return meta

    async def delete_session(self, session_id: str) -> bool:
        """Delete session and cleanup."""
        if session_id in self.session_manager.sessions:
            await self.session_manager.close_session(
                session_id,
                reason="session deleted",
            )

        if self._session_store is not None and delete_session_via_store is not None:
            # SDK derives project_key from `directory`; without it the key is
            # computed from server cwd and never matches inserted rows, so the
            # delete becomes a silent no-op. Resolve project cwd from meta.
            meta = await self.meta_store.get(session_id)
            project_cwd = str(self.layout.projects_dir / meta.project_name) if meta else None
            try:
                await delete_session_via_store(self._session_store, session_id, directory=project_cwd)
            except Exception:
                logger.warning(
                    "delete_session_via_store failed for %s",
                    session_id,
                    exc_info=True,
                )
        elif sdk_delete_session is not None:
            try:
                await run_sync_transaction(sdk_delete_session, session_id)
            except Exception:
                logger.warning("sdk delete_session failed for %s", session_id, exc_info=True)

        try:
            await self.event_log_store.delete_session(session_id)
        except Exception:
            logger.warning("删除会话事件日志失败 session_id=%s", session_id, exc_info=True)

        return await self.meta_store.delete(session_id)

    # ==================== Messages ====================

    def _prepare_content(
        self,
        content: str,
        images: list["ImageAttachment"] | None = None,
    ) -> str | list[dict[str, Any]]:
        """Message content sent to the CLI: a string for plain text; with images, a list with image blocks first and
        the text block after.

        送入 CLI 的消息内容：纯文本为字符串，带图片时为图片块在前、文本块在后的列表。"""
        text = content.strip()
        if not text and not images:
            raise ValueError("消息内容不能为空")
        if not images:
            return text
        blocks: list[dict[str, Any]] = [self._image_block(img) for img in images]
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    @staticmethod
    def _build_user_log_entry(content: str | list[dict[str, Any]]) -> dict[str, Any]:
        """Build the authoritative entry of a user message: the identity is assigned at send time, and it is written to
        the log when the Agent accepts it.

        构造用户消息的权威条目：身份在发送时分配，被 Agent 接纳时写入日志。"""
        return build_user_entry([{"type": "text", "text": content}] if isinstance(content, str) else content)

    async def send_or_create(
        self,
        project_name: str,
        content: str,
        *,
        session_id: str | None = None,
        images: list["ImageAttachment"] | None = None,
        locale: str = DEFAULT_LOCALE,
        client_key: str | None = None,
    ) -> dict[str, Any]:
        """Unified send: create new session or send to existing one.

        Existing session: accepted even while a turn is running; the response carries the queued message
        (``queued_message``), which is only sent down the entry stream as an entry once the Agent accepts it; for a
        retry with the same ``client_key`` whose message is already in the log, the response carries the
        authoritative entry (``entry``). A session superseded by a rewrite no longer accepts messages
        (``SessionSupersededError``). New session: returns only after the first message is written to the log, and
        the response carries the authoritative entry. The frontend renders no locally synthesized messages.

        Unified send: create new session or send to existing one.

        已有会话：有轮次在跑时同样受理，响应携带排队消息（``queued_message``），被 Agent 接纳时
        才作为条目经 entry 流下发；同一 ``client_key`` 的重试若消息已入日志，响应携带权威条目
        （``entry``）。已被改写取代的会话不再受理消息（``SessionSupersededError``）。新会话：首条消息
        写入日志后才返回，响应携带权威条目。前端不渲染任何本地合成消息。
        """
        await asyncio.to_thread(self.pm.get_project_path, project_name)  # Validate project

        if session_id:
            # Existing session
            async with self._admission_locks.lock_for(session_id):
                meta = await self.meta_store.get(session_id)
                if meta is None:
                    raise FileNotFoundError(f"session not found: {session_id}")
                if meta.project_name != project_name:
                    raise FileNotFoundError(f"session not found: {session_id}")
                if meta.superseded_by is not None:
                    raise SessionSupersededError(
                        f"session {session_id} has already been superseded by {meta.superseded_by}"
                    )
                prompt = self._prepare_content(content, images)
                # Lazy generation of an old session comes first: ensures this message comes after the history rebuilt
                # by replay.
                # 旧会话懒生成先行：保证本条消息排在重放重建的历史之后。
                await self.event_log.ensure_backfilled(
                    session_id, await self._aresolve_project_cwd_safe(meta.project_name)
                )
                accepted = await self.session_manager.send_message(
                    session_id,
                    prompt,
                    meta=meta,
                    locale=locale,
                    user_entry=self._build_user_log_entry(prompt),
                    client_key=client_key,
                )
            return self._accepted_response(session_id, accepted)
        # New session
        if not client_key:
            return await self._create_new_session(project_name, content, images, locale, client_key)

        existing = await self._find_accepted_new_session(client_key, project_name)
        if existing is not None:
            return existing

        # Concurrent requests with the same client_key are serialized here: while send_new_session is in flight,
        # latecomers wait for the lock instead of each creating a session, to avoid executing the same prompt twice.
        # 同一 client_key 的并发请求在此串行化：send_new_session 在途期间
        # 后来者等锁而非各自建会话，避免重复执行同一 prompt。
        lock = self._new_session_locks.lock_for(client_key)
        async with lock:
            # Double check: while waiting for the lock, the earlier request may already have created the session for
            # the same client_key.
            # 双重检查：等锁期间先行者可能已完成同一 client_key 的建会话。
            existing = await self._find_accepted_new_session(client_key, project_name)
            if existing is not None:
                return existing
            result = await self._create_new_session(project_name, content, images, locale, client_key)
            self._record_new_session_client_key(client_key, result["session_id"])
            return result

    async def _find_accepted_new_session(self, client_key: str, project_name: str) -> dict[str, Any] | None:
        """Locate an accepted new session by idempotency key: the in-process mapping is the fast path, with a
        cross-session event log query as the fallback -- after a process restart / LRU eviction the mapping is lost,
        and a retry whose acceptance is already persisted must still hit the existing session instead of creating a
        duplicate one (executing the same prompt twice, billing twice).

        The project of the hit session must match the caller's ``project_name``: if not, it counts as a miss
        (returns None and the caller creates a new session in the current project), without raising -- the user did
        not name any session, so when the same client_key is reused across projects the new-session intent should
        land in the current project. Both lookup paths use the same rule and check ownership before returning.

        按幂等键定位已受理的新会话：进程内映射为快路径，事件日志跨会话
        查询兜底——进程重启 / LRU 淘汰后映射丢失，受理已落库的重试仍须命中
        既有会话而非重复建会话（重复执行同一 prompt、重复计费）。

        命中会话的项目归属须与调用方 ``project_name`` 一致：不一致则视为未命中
        （返回 None，由调用方在当前项目新建会话），不抛错——用户未指名任何
        会话，跨项目复用同一 client_key 时新会话意图应落在当前项目。两条查找
        路径口径一致，均在返回前做归属校验。"""
        mapped_session_id = self._new_session_client_keys.get(client_key)
        if mapped_session_id is not None:
            # Idempotent retry: the first acceptance already created the session and delivered the message; return the
            # authoritative entry of that same session.
            # 幂等重试：首次受理已建会话并投递，返回同一会话的权威条目。
            entry = await self.event_log_store.find_by_client_key(mapped_session_id, client_key)
            if entry is None:
                # The session entry the mapping points to no longer exists (e.g. the session was deleted): the mapping
                # is invalid, so clear it and keep probing below, to avoid returning a ghost "accepted" response
                # pointing to a deleted session -- the caller would connect to a non-existent session based on it and
                # the message would be silently lost. During the await on the previous line, another concurrent
                # request may have written a newer mapping for this key; only clear it if the current value is still
                # the old value read here, to avoid clearing a concurrently written new mapping (the DB fallback query
                # locates by client_key and always hits the same authoritative session, so a wrong delete would only
                # waste a query, but exact conditions still avoid this needless churn).
                # 映射指向的会话条目已不存在（如会话已被删除）：映射已失效，
                # 清掉后继续向下探测，避免返回指向已删除会话的幽灵 "accepted"
                # 响应——调用方会据此连接一个不存在的会话，消息静默丢失。上一行
                # await 期间该 key 可能已被其他并发请求写入更新的映射；仅当当前
                # 值仍是本次读到的旧值时才清，避免清掉并发写入的新映射（DB 兜底
                # 查询本身按 client_key 定位一定命中同一权威会话，误删只是白跑
                # 一次查询，但仍以精确条件避免这层不必要的抖动）。
                if self._new_session_client_keys.get(client_key) == mapped_session_id:
                    self._new_session_client_keys.pop(client_key, None)
            elif await self._new_session_matches_project(mapped_session_id, project_name):
                # Refresh the LRU position on a hit: otherwise a key hit by frequent retries is still evicted by
                # insertion order (not access order), degrading to FIFO. During the await on the previous line, the
                # key may have been removed by another concurrent request's eviction logic, and calling move_to_end
                # directly on a missing key raises KeyError; reuse the assignment semantics of
                # _record_new_session_client_key (insert if missing, update in place if present) and then explicitly
                # move it to the most-recently-used end, which is safe in both cases.
                # 命中即刷新 LRU 位置：否则被频繁重试命中的 key 仍按插入
                # 顺序（而非访问顺序）淘汰，退化成 FIFO。上一行 await 期间
                # 该 key 可能已被其他并发请求的淘汰逻辑移除，直接
                # move_to_end 对不存在的键会抛 KeyError；复用
                # _record_new_session_client_key 的赋值语义（不存在则插入，
                # 存在则原地更新）再显式挪到最近使用端，两种情形都安全。
                self._record_new_session_client_key(client_key, mapped_session_id)
                return {"status": "accepted", "session_id": mapped_session_id, "entry": entry, "queued_message": None}
            # else: the session hit by the mapping belongs to another project -> treat as a miss and fall through to
            #       the DB fallback / create path. Do not clear the mapping: it is still valid for the original
            #       project; when a new session is later created in the current project,
            #       _record_new_session_client_key overwrites the same client_key with this project's session.
            # else：映射命中的会话属于其他项目 → 视为未命中，落到 DB 兜底 / 新建
            # 路径。不清映射：它对原项目仍有效；后续在当前项目新建会话时由
            # _record_new_session_client_key 以本项目 session 覆盖同一 client_key。
        recovered = await self.event_log_store.find_new_session_by_client_key(client_key)
        if recovered is None:
            return None
        session_id, entry = recovered
        if not await self._new_session_matches_project(session_id, project_name):
            # The session hit by the fallback belongs to another project -> treat as a miss and take the current
            # project's create path.
            # 兜底命中的会话属于其他项目 → 视为未命中，走当前项目新建路径。
            return None
        # During the await on the previous line, another concurrent request may have recorded a new mapping for this
        # key; only write when there is currently no mapping or it is already the same session_id, to avoid
        # overwriting a concurrently written mapping with the (older) session_id found here.
        # 上一行 await 期间该 key 可能已被其他并发请求记入新映射；仅当当前
        # 无映射或已是同一 session_id 时才写入，避免用本次查到的（较旧）
        # session_id 覆盖并发写入的映射。
        if self._new_session_client_keys.get(client_key) in (None, session_id):
            self._record_new_session_client_key(client_key, session_id)
        return {"status": "accepted", "session_id": session_id, "entry": entry, "queued_message": None}

    async def _new_session_matches_project(self, session_id: str, project_name: str) -> bool:
        """Whether the new session hit idempotently belongs to the calling project. The check uses the ``project_name``
        of the session meta; when the meta does not exist (error / deleted) the hit is not blocked, keeping the
        existing idempotency semantics -- a cross-project mix-up requires that the hit session's meta exists and the
        project differs.

        幂等命中的新会话是否属于当前调用项目。校验依据为会话 meta 的
        ``project_name``；meta 不存在（异常 / 已删）时不阻断命中，保持既有幂等
        语义——跨项目串号的前提是命中会话 meta 存在且项目不同。"""
        meta = await self.meta_store.get(session_id)
        return meta is None or meta.project_name == project_name

    def _record_new_session_client_key(self, client_key: str, session_id: str) -> None:
        self._new_session_client_keys[client_key] = session_id
        self._new_session_client_keys.move_to_end(client_key)
        while len(self._new_session_client_keys) > self._new_session_client_keys_max:
            self._new_session_client_keys.popitem(last=False)

    async def _create_new_session(
        self,
        project_name: str,
        content: str,
        images: list["ImageAttachment"] | None,
        locale: str,
        client_key: str | None,
    ) -> dict[str, Any]:
        """Actually create the new session and deliver the first message, without the client_key idempotency mapping
        bookkeeping.

        实际创建新会话并投递首条消息，不涉及 client_key 幂等映射记账。"""
        prompt = self._prepare_content(content, images)
        new_sdk_session_id = await self.session_manager.send_new_session(
            project_name,
            prompt,
            locale=locale,
            user_entry=self._build_user_log_entry(prompt),
            client_key=client_key,
        )
        managed = self.session_manager.sessions.get(new_sdk_session_id)
        entry = managed.initial_user_log_entry if managed is not None else None
        return {"status": "accepted", "session_id": new_sdk_session_id, "entry": entry, "queued_message": None}

    @staticmethod
    def _accepted_response(session_id: str, accepted: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """Acceptance response for an existing session: exactly one of the queued message and the authoritative entry
        (the entry is given when an idempotent retry hits a message already in the log).

        已有会话的受理响应：排队消息与权威条目二者恰有其一（条目在幂等重试命中已入日志的消息时给出）。"""
        return {
            "status": "accepted",
            "session_id": session_id,
            **extra,
            "entry": accepted.get("entry"),
            "queued_message": accepted.get("queued_message"),
        }

    # ==================== Message rewrite (branch session orchestration) ====================
    # ==================== 消息改写（分支会话编排） ====================

    # Upper limit and polling interval for waiting for the running turn to be interrupted to a terminal state: the
    # terminal state is derived by the inbox task after it receives the SDK's result message; there is no event to
    # wait on, so the status can only be observed.
    # 等待运行中轮次中断到终态的上限与轮询间隔：终态由 inbox 任务收到 SDK 的
    # result 消息后推导，没有可等的 event，只能观察状态。
    _INTERRUPT_SETTLE_TIMEOUT = 30.0
    _INTERRUPT_SETTLE_POLL = 0.05

    async def rewrite_message(
        self,
        project_name: str,
        session_id: str,
        *,
        anchor_entry_uuid: str,
        content: str,
        images: list["ImageAttachment"] | None = None,
        locale: str = DEFAULT_LOCALE,
        client_key: str | None = None,
    ) -> dict[str, Any]:
        """Rewrite the user message at the anchor in ``session_id`` and return the new session that takes over the
        rewrite.

        The orchestration order follows the cost of rejection: reject what can be rejected first (anchor, pending
        questions), do the costly steps later (interrupting the running turn, forking, dispatching) -- a rejected
        request should not already have interrupted the user's turn. Any step failing after the fork means the whole
        rewrite fails; the branch is withdrawn as a whole and the original session returns to a rewritable state.

        The response has the same shape as the send endpoint: ``client_key`` is the request-side idempotency key,
        and retries do not create a second branch session; the rewritten message is first a queued message
        (``queued_message``) in the new session and lands in the new session's log once accepted.

        改写 ``session_id`` 中锚点处的那条用户消息，返回承接改写的新会话。

        编排顺序即拒绝的代价顺序：能拒的先拒（锚点、未决问答），代价大的后做
        （中断运行中的轮次、分叉、派发）——被拒的请求不该已经打断用户的轮次。
        分叉之后任何一步失败都是整次改写失败，分支整体撤回，原会话回到可再改写
        的状态。

        响应与发送端点同构：``client_key`` 为请求侧幂等键，重试不产生第二个分支
        会话；改写后的消息在新会话里先是排队消息（``queued_message``），被接纳后落进新会话的日志。
        """
        await asyncio.to_thread(self.pm.get_project_path, project_name)  # Validate project
        if not client_key:
            return await self._rewrite_message_once(
                project_name,
                session_id,
                anchor_entry_uuid=anchor_entry_uuid,
                content=content,
                images=images,
                locale=locale,
                client_key=None,
            )
        async with self._rewrite_locks.lock_for(f"{session_id}:{client_key}"):
            return await self._rewrite_message_once(
                project_name,
                session_id,
                anchor_entry_uuid=anchor_entry_uuid,
                content=content,
                images=images,
                locale=locale,
                client_key=client_key,
            )

    async def _rewrite_message_once(
        self,
        project_name: str,
        session_id: str,
        *,
        anchor_entry_uuid: str,
        content: str,
        images: list["ImageAttachment"] | None,
        locale: str,
        client_key: str | None,
    ) -> dict[str, Any]:
        meta = await self.meta_store.get(session_id)
        if meta is None or meta.project_name != project_name:
            raise FileNotFoundError(f"session not found: {session_id}")

        if self._session_store is None:
            raise RewriteUnavailableError(
                "message rewrite requires the DB transcript store (ARCREEL_SDK_SESSION_STORE=db)"
            )

        # The original session has been superseded: a retry with the same client_key claims its own authoritative
        # entry in the new branch; any other case is a rewrite against a voided branch and is rejected explicitly
        # rather than forking again.
        # 原会话已被取代：同一 client_key 的重试在新分支里认领自己的权威条目，
        # 其余情形是对一个已作废分支发起的改写，明确拒绝而非再分叉一次。
        if meta.superseded_by is not None:
            replay = await self._replay_rewrite(meta.superseded_by, client_key)
            if replay is not None:
                return replay
            raise SessionSupersededError(f"session {session_id} has already been superseded by {meta.superseded_by}")

        # Content validation comes before any step with side effects: an empty message should not interrupt the
        # running turn.
        # 内容校验先于任何有副作用的步骤：空消息不该中断运行中的轮次。
        prompt = self._prepare_content(content, images)

        project_cwd = await self._aresolve_project_cwd_safe(meta.project_name)
        anchor = await self.event_log.resolve_user_message_anchor(session_id, anchor_entry_uuid, project_cwd)
        if anchor is None:
            raise RewriteAnchorError(f"anchor {anchor_entry_uuid} is not a user message of session {session_id}")

        # Questions take priority: do not rewrite while a pending question card exists. Cards live only in memory
        # (answers are handed back to the running turn via a future); after the session is evicted or the process
        # restarts the card has been cancelled with it, a cold session has no answerable question, so reading memory
        # is reading the truth.
        # 问答优先：未决问答卡片存在时不改写。卡片只活在内存里（回答经 future
        # 交回运行中的轮次），会话被驱逐或进程重启后卡片已随之取消，冷会话没有
        # 可回答的问答，读内存即读真相。
        if await self.session_manager.get_pending_questions_snapshot(session_id):
            raise PendingQuestionError(f"session {session_id} has pending questions")
        # No new messages are accepted between the queue check and branch publication: an interrupt only stops the
        # current turn, queued messages would go on running, and the branch would not have them either
        # 排队检查到分支发布之间不受理新消息：中断只停当前轮，排队消息会接着执行，分支里也不会有它们
        async with self._admission_locks.lock_for(session_id):
            if self.session_manager.get_queued_messages_snapshot(session_id):
                raise QueuedMessagesPendingError(f"session {session_id} has queued messages")

            await self._settle_running_session(session_id)

            branched = await self._branch_or_reject(session_id, anchor_entry_uuid)
        new_session_id = branched.session_id
        # Once the branch is published (the superseded pointer already points to the new session), every later step is
        # within compensation scope: if a mid-way failure were not rolled back, the original session would be hidden
        # while the new session never received the rewritten message, and a retry would hit "already superseded". When
        # send_message raises it leaves no queued message and has written no entry (entries are only written when the
        # Agent accepts), so rolling back as a whole loses no data.
        # 分支一旦发布（superseded 指针已指向新会话），其后每一步都在补偿范围内：
        # 中途失败若不撤回，原会话被隐藏、新会话又没收到改写后的消息，重试还会
        # 撞上「已被取代」。send_message 抛出时不留下排队消息，也没有写入任何条目
        # （条目在 Agent 接纳时才写），因此整体撤回不丢数据。
        try:
            new_meta = await self.meta_store.get(new_session_id)
            if branched.resumable:
                # Lazy generation comes first: the rewritten message must come after the copied prefix history. / 懒生成先行：改写后的消息要排在复制来的前缀历史之后。
                await self.event_log.ensure_backfilled(new_session_id, project_cwd)
            accepted = await self.session_manager.send_message(
                new_session_id,
                prompt,
                meta=new_meta,
                locale=locale,
                user_entry=self._build_user_log_entry(prompt),
                client_key=client_key,
                resumable=branched.resumable,
            )
        except BaseException:
            await self._discard_branch(session_id, new_session_id)
            raise

        return self._accepted_response(new_session_id, accepted, origin_session_id=session_id)

    async def _replay_rewrite(self, new_session_id: str, client_key: str | None) -> dict[str, Any] | None:
        """Idempotent replay: whether the rewrite for the given client_key has already been taken over by
        ``new_session_id``.

        幂等重放：给定 client_key 的改写是否已由 ``new_session_id`` 承接。"""
        if not client_key:
            return None
        accepted = self.session_manager.find_queued_message_by_client_key(new_session_id, client_key)
        if accepted is None:
            entry = await self.event_log_store.find_by_client_key(new_session_id, client_key)
            if entry is None:
                return None
            accepted = {"entry": entry}
        meta = await self.meta_store.get(new_session_id)
        return self._accepted_response(
            new_session_id,
            accepted,
            origin_session_id=meta.fork_parent_session_id if meta is not None else None,
        )

    async def _settle_running_session(self, session_id: str) -> None:
        """Interrupt the running turn and wait for it to reach a terminal state -- a running session cannot fork a
        clean prefix.

        For the user it is a single action: the rewrite request includes the interrupt, there is no need to click
        stop first. The terminal state is derived by the inbox task after receiving the SDK's result message and can
        only be observed; messages at the tail of the interrupted turn come after the anchor anyway and are voided
        with the original branch, so there is no need to wait for them to be persisted.

        中断运行中的轮次并等它落到终态——运行中的会话分叉不出干净的前缀。

        对用户是一步操作：改写请求自带中断，不需要先点停止。终态由 inbox 任务
        在收到 SDK 的 result 消息后推导，只能观察状态；被中断轮次尾巴上的消息
        本就排在锚点之后、要随原分支作废，无需等它们落库。
        """
        status = await self.session_manager.interrupt_session(session_id)
        if status != "running":
            return
        deadline = asyncio.get_running_loop().time() + self._INTERRUPT_SETTLE_TIMEOUT
        while status == "running":
            if asyncio.get_running_loop().time() >= deadline:
                raise InterruptSettleTimeoutError(f"session {session_id} did not settle after interrupt")
            await asyncio.sleep(self._INTERRUPT_SETTLE_POLL)
            status = await self.session_manager.get_status(session_id) or "idle"

    async def _branch_or_reject(self, session_id: str, anchor_entry_uuid: str) -> BranchedSession:
        """Fork, and translate exceptions from the branch service back into rejection reasons the orchestration layer
        can tell apart.

        分叉，并把分支服务的异常翻译回编排层能分辨的拒绝理由。"""
        try:
            return await self.session_branch.branch(session_id, anchor_entry_uuid)
        except BranchAnchorError as exc:
            # The orchestration layer's pre-check passed but slicing refused: the resolved uuid does not exist in the
            # transcript (the anchor is one just sent by the running turn that the SDK has not replayed yet), or that
            # entry carries a tool_result. Both are bad requests the caller can correct, distinct from "fork failed".
            # 编排层的预检放行了、切片却拒绝：解析出的 uuid 在 transcript 里查无
            # 此条（锚点是运行中轮次刚发出、SDK 尚未回放的那条），或该条目载有
            # tool_result。都是调用方能改正的坏请求，与「分叉失败」区分开。
            raise RewriteAnchorError(
                f"anchor {anchor_entry_uuid} is not a forkable user message of session {session_id}"
            ) from exc
        except SessionBranchError as exc:
            meta = await self.meta_store.get(session_id)
            if meta is not None and meta.superseded_by is not None:
                # Lost to another rewrite between the pre-check and the fork (the conditional update of the pointer
                # lets only one win).
                # 预检与分叉之间输给了另一次改写（指针的条件更新只让一个赢）。
                raise SessionSupersededError(
                    f"session {session_id} has already been superseded by {meta.superseded_by}"
                ) from exc
            raise

    async def _discard_branch(self, origin_session_id: str, new_session_id: str) -> None:
        """Withdraw a branch that failed to take over the rewrite: disconnect its runtime first, then clear the data.

        撤回一个没能承接住改写的分支：先断开它的运行时，再清数据。"""
        try:
            if new_session_id in self.session_manager.sessions:
                await self.session_manager.close_session(new_session_id, reason="message rewrite dispatch failed")
        except Exception:
            logger.exception("关闭未完成分支会话失败 session_id=%s", new_session_id)
        try:
            # The fork itself does not write the event log, but the dispatch path may already have; clear the log
            # along with it, the same way delete_session does, to avoid leaving orphan entries after the rollback.
            # 分叉本身不写事件日志，但派发路径可能已经写过；与 delete_session
            # 同口径连日志一起清，避免撤回后留下无主条目。
            await self.event_log_store.delete_session(new_session_id)
        except Exception:
            logger.exception("删除未完成分支会话事件日志失败 session_id=%s", new_session_id)
        try:
            await self.session_branch.discard(origin_session_id, new_session_id)
        except Exception:
            logger.exception("撤回未完成分支失败 origin=%s new=%s", origin_session_id, new_session_id)

    @staticmethod
    def _image_block(img: "ImageAttachment") -> dict[str, Any]:
        """Build a single image content block dict."""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.media_type,
                "data": img.data,
            },
        }

    async def answer_user_question(
        self,
        session_id: str,
        question_id: str,
        answers: dict[str, str],
        *,
        meta: SessionMeta | None = None,
    ) -> dict[str, Any]:
        """Submit answers for a pending AskUserQuestion."""
        if meta is None:
            meta = await self.meta_store.get(session_id)
            if meta is None:
                raise FileNotFoundError(f"session not found: {session_id}")
        await self.session_manager.answer_user_question(session_id, question_id, answers)
        return {"status": "accepted", "session_id": session_id, "question_id": question_id}

    async def withdraw_queued_message(
        self, project_name: str, session_id: str, message_id: str, *, intent: WithdrawalIntent
    ) -> dict[str, Any]:
        """Edit or delete a queued message: withdraw it from the CLI first, and only remove it from the queue if the
        withdrawal succeeds; when editing, the response returns the message content.

        When ``outcome`` is ``withdrawn`` the message has been removed from the queue (when editing, ``message`` is
        the content to put back into the input box); when it is ``accepted`` the Agent has already received this
        message and it enters the conversation as usual.

        编辑或删除一条排队消息：先向 CLI 撤回，撤回成功才移出排队，编辑时响应带回消息内容。

        ``outcome`` 为 ``withdrawn`` 时消息已移出排队（编辑时 ``message`` 是要退回输入框的内容）；
        为 ``accepted`` 时 Agent 已接收这条消息，它照常进入对话。
        """
        meta = await self.meta_store.get(session_id)
        if meta is None or meta.project_name != project_name:
            raise FileNotFoundError(f"session not found: {session_id}")
        # Shares the acceptance lock with sending: while a send is rebuilding the connection, "unsent" messages are
        # temporarily away from the session, so the withdrawal looks for it after the connection is set up
        # 与发送共用受理锁：发送重建连接期间「未发送」消息暂离会话，撤回等连接建好后再找它
        async with self._admission_locks.lock_for(session_id):
            outcome, queued = await self.session_manager.withdraw_queued_message(session_id, message_id, intent)
        return {
            "session_id": session_id,
            "id": message_id,
            "outcome": outcome,
            "message": queued.to_payload() if queued is not None and intent == "edit" else None,
        }

    async def resend_queued_message(
        self, project_name: str, session_id: str, message_id: str, *, locale: str = DEFAULT_LOCALE
    ) -> dict[str, Any]:
        """Hand an "unsent" message to the CLI again, reviving the session first if needed; the response returns this
        queued message.

        把一条「未发送」消息重新交给 CLI，必要时先复活会话；响应带回这条排队消息。"""
        # Shares the acceptance lock with send and rewrite: reviving the session and sending to the CLI are not
        # interleaved with them
        # 与发送、改写共用受理锁：复活会话与送入 CLI 不与它们交错
        async with self._admission_locks.lock_for(session_id):
            meta = await self.meta_store.get(session_id)
            if meta is None or meta.project_name != project_name:
                raise FileNotFoundError(f"session not found: {session_id}")
            queued = await self.session_manager.resend_queued_message(session_id, message_id, meta=meta, locale=locale)
        return {"session_id": session_id, "id": message_id, "queued_message": queued.to_payload()}

    async def send_queued_message_now(self, project_name: str, session_id: str, message_id: str) -> dict[str, Any]:
        """Send a queued message immediately: the Agent interrupts the current turn and handles it first.

        When ``outcome`` is ``sent`` it has been sent again with ``now`` priority and is still the same message in
        the tray; when it is ``accepted`` the Agent has already received this message, it is not resent and enters
        the conversation as usual.

        立即发送一条排队消息：Agent 打断当前轮先处理它。

        ``outcome`` 为 ``sent`` 时已以 ``now`` 优先级重新送入，它仍是托盘里的同一条消息；为
        ``accepted`` 时 Agent 已接收这条消息，不再重发，它照常进入对话。
        """
        meta = await self.meta_store.get(session_id)
        if meta is None or meta.project_name != project_name:
            raise FileNotFoundError(f"session not found: {session_id}")
        # Same as withdrawal: not interleaved with a send rebuilding the connection; for an "unsent" message it
        # rebuilds the connection itself, serialized just like a resend
        # 同撤回：不与发送重建连接交错；对「未发送」消息还会自己重建连接，与重新发送一样串行
        async with self._admission_locks.lock_for(session_id):
            outcome = await self.session_manager.send_queued_message_now(session_id, message_id)
        return {"session_id": session_id, "id": message_id, "outcome": outcome}

    async def interrupt_session(self, session_id: str, *, meta: SessionMeta | None = None) -> dict[str, Any]:
        """Interrupt a running session."""
        if meta is None:
            meta = await self.meta_store.get(session_id)
            if meta is None:
                raise FileNotFoundError(f"session not found: {session_id}")
        session_status = await self.session_manager.interrupt_session(session_id)
        return {
            "status": "accepted",
            "session_id": session_id,
            "session_status": session_status,
        }

    # ==================== Session event log (single read source of the UI timeline) ====================
    # ==================== 会话事件日志（UI 时间线唯一读源） ====================

    async def list_session_entries(
        self,
        session_id: str,
        *,
        meta: SessionMeta | None = None,
        after_seq: int = -1,
    ) -> dict[str, Any]:
        """Cold read of the event log (history replay / initial load of a non-running session).

        冷读事件日志（历史回放 / 非 running 会话初始加载）。"""
        if meta is None:
            meta = await self.meta_store.get(session_id)
            if meta is None:
                raise FileNotFoundError(f"session not found: {session_id}")
        status = await self.session_manager.get_status(session_id) or meta.status
        project_cwd = await self._aresolve_project_cwd_safe(meta.project_name)
        entries = await self.event_log.list_entries(session_id, project_cwd, after_seq=after_seq)
        draft_state = (
            self.session_manager.get_draft_state(session_id) if status == "running" else {"draft": None, "rev": 0}
        )
        return {
            "session_id": session_id,
            "status": status,
            "entries": entries,
            "draft": draft_state["draft"],
            "draft_rev": draft_state["rev"],
        }

    async def stream_entry_events(
        self,
        session_id: str,
        *,
        meta: SessionMeta | None = None,
        request: Request | None = None,
        after_seq: int = -1,
    ) -> AsyncIterator[ServerSentEvent]:
        """SSE entry stream: the event ``id`` is the seq; on reconnect it resumes from the cursor without recomputing
        whole frames.

        The lifetime follows the session panel: at open it sends in order ``entry``xN (stored entries after the
        cursor), ``draft`` (streaming accumulated state + rev filter threshold), ``queue`` (queued message
        snapshot), ``question``xN (pending questions) and the current ``status``, then live-streams entry / delta /
        queued message changes (``queue_upsert`` / ``queue_remove``, plus another ``queue`` when a send-immediately
        reorders the tray) / question / status. ``status`` only updates the status and does not close the stream;
        the stream only ends when the client leaves (or the subscriber is removed by overflow, which is the
        reconnect signal). When the session is not resident the stream is still set up and waits, and a session
        later revived by a send pushes as usual.

        SSE entry 流：事件 ``id`` 即 seq，断线重连按 cursor 续传、不整帧重算。

        生命周期跟随会话面板：开场依次下发 ``entry``×N（cursor 之后的存量）、
        ``draft``（流式累积态 + rev 过滤门槛）、``queue``（排队消息快照）、``question``×N
        （未决问题）和当前 ``status``，之后直播 entry / delta / 排队消息变化（``queue_upsert`` /
        ``queue_remove``，立即发送重排托盘时再发一次 ``queue``）/ question / status。``status`` 只更新
        状态、不关流；流只在客户端离开（或订阅者被溢出移除，即重连信号）时结束。
        会话不常驻时同样建流等待，之后由发送复活的会话照常推送。
        """
        if meta is None:
            meta = await self.meta_store.get(session_id)
            if meta is None:
                raise FileNotFoundError(f"session not found: {session_id}")

        project_cwd = await self._aresolve_project_cwd_safe(meta.project_name)
        last_seq = after_seq
        async with self.session_manager.stream_messages(
            session_id, idle_timeout=self.stream_heartbeat_seconds
        ) as stream:
            ready = await anext(stream, None)
            if not isinstance(ready, SubscriptionReady):
                return
            # The subscription is set up first (no gap); entries delivered twice between subscription and the DB read
            # are filtered by the seq threshold -- an identity comparison, not a content comparison.
            # 订阅已先行建立（无缝隙）；订阅与库读之间重复投递的条目由 seq
            # 门槛过滤——身份比对，非内容比对。
            for entry in await self.event_log.list_entries(session_id, project_cwd, after_seq=last_seq):
                last_seq = max(last_seq, self._entry_seq(entry))
                yield self._entry_sse_event(entry)

            yield self._draft_sse_event(session_id)

            # Deltas delivered twice between the snapshot and the subscription are applied idempotently by queued
            # message id; subscribing first guarantees eventual consistency
            # 快照与订阅之间重复投递的增量按排队消息 id 幂等应用，订阅先行保证最终一致
            yield self._sse_event(
                "queue",
                {"session_id": session_id, "messages": self.session_manager.get_queued_messages_snapshot(session_id)},
            )

            for question in await self.session_manager.get_pending_questions_snapshot(session_id):
                yield self._sse_event("question", {**question, "session_id": session_id})

            status: SessionStatus = await self.session_manager.get_status(session_id) or meta.status
            yield self._sse_event("status", self._build_status_event_payload(status=status, session_id=session_id))

            # A result only means one turn ended and does not push a status: the session leaves running when the CLI
            # reports idle, and the inbox sends runtime_status after all of this turn's entries are broadcast.
            # result 只代表一轮结束，不推状态：会话离开 running 以 CLI 报 idle 为准，由 inbox
            # 在本轮条目全部广播之后发出 runtime_status。
            async for stream_event in stream:
                if request is not None and await request.is_disconnected():
                    break

                if isinstance(stream_event, Heartbeat):
                    # Status changes not broadcast, such as eviction, are aligned by the heartbeat. / 驱逐等不经广播的状态变化由心跳对齐。
                    live_status = await self.session_manager.get_status(session_id) or status
                    if live_status != status:
                        status = live_status
                        for event in self._status_change_events(status, session_id):
                            yield event
                    continue

                if not isinstance(stream_event, LiveMessage):
                    continue

                message = stream_event.message
                msg_type = message.get("type", "")

                if (
                    msg_type in ("log_entry", "log_delta", "ask_user_question", "queued_message")
                    and status != "running"
                ):
                    # Both sends and autonomous turns switch to running before producing these messages (a send
                    # registers the queued message and switches to running together): push running first, so the new
                    # turn's content and queued messages do not land under the old terminal state.
                    # 发送、自主轮次都先切 running 再产出这些消息（发送登记排队消息与切 running 同步完成）：
                    # 先推 running，新一轮的内容与排队消息不落在旧终态之下。
                    live_status = await self.session_manager.get_status(session_id) or status
                    if live_status == "running":
                        status = live_status
                        for event in self._status_change_events(status, session_id):
                            yield event

                if msg_type == "log_entry":
                    entry = message.get("entry")
                    if isinstance(entry, dict):
                        seq = self._entry_seq(entry)
                        if seq > last_seq:
                            last_seq = seq
                            yield self._entry_sse_event(entry)
                    continue

                if msg_type == "log_delta":
                    yield self._sse_event("delta", {k: v for k, v in message.items() if k != "type"})
                    continue

                if msg_type == "queued_message":
                    if message.get("op") == "remove":
                        # When removed because the user withdrew it, include the intent (``withdrawn``) and, when
                        # editing, the content to put back into the input box (``message``)
                        # 按用户撤回移出时带上意图（``withdrawn``）与编辑时退回输入框的内容（``message``）
                        removed = {k: v for k, v in message.items() if k in ("id", "withdrawn", "message")}
                        yield self._sse_event("queue_remove", {"session_id": session_id, **removed})
                    elif message.get("op") == "reorder":
                        # A send-immediately moves the message to the front: rebroadcast the whole snapshot, and the
                        # client reorders the tray by it
                        # 立即发送把消息排到最前：重播整份快照，客户端按它重排托盘
                        yield self._sse_event("queue", {"session_id": session_id, "messages": message.get("messages")})
                    else:
                        yield self._sse_event(
                            "queue_upsert", {"session_id": session_id, "message": message.get("message")}
                        )
                    continue

                if msg_type == "ask_user_question":
                    yield self._sse_event(
                        "question",
                        await self._with_session_metadata(copy.deepcopy(message), session_id=session_id),
                    )
                    continue

                if msg_type == "runtime_status":
                    terminal = self._check_runtime_status_terminal(message, session_id)
                    if terminal is not None:
                        status = terminal.data["status"]
                        yield terminal
                    continue

    def _draft_sse_event(self, session_id: str) -> ServerSentEvent:
        return self._sse_event("draft", {"session_id": session_id, **self.session_manager.get_draft_state(session_id)})

    def _status_change_events(self, status: SessionStatus, session_id: str) -> list[ServerSentEvent]:
        """Status changes not broadcast via runtime_status. When going back to running, add a draft snapshot frame: if
        the session was revived after eviction, the new process counts delta rev from the start, so the client must
        switch to the new filter threshold.

        不经 runtime_status 广播的状态变化。回到 running 时补一帧 draft 快照：会话若经驱逐后
        复活，新进程的 delta rev 从头计数，客户端要换用新的过滤门槛。"""
        events = [self._sse_event("status", self._build_status_event_payload(status=status, session_id=session_id))]
        if status == "running":
            events.append(self._draft_sse_event(session_id))
        return events

    async def stream_startup_failure_events(
        self,
        session_id: str,
        failure: dict[str, Any],
    ) -> AsyncIterator[ServerSentEvent]:
        """Cold-resume startup for an immediate send failed; only record the terminal state, do not write the failure
        details into history.

        即时发送冷恢复启动失败；只落终态，不把故障详情写入历史。"""
        entry = build_failure_entry(failure)
        await self.meta_store.update_status(session_id, "error")
        yield self._sse_event("entry", entry)
        yield self._sse_event(
            "status",
            self._build_status_event_payload(status="error", session_id=session_id),
        )

    @staticmethod
    def _entry_seq(entry: dict[str, Any]) -> int:
        seq = entry.get("seq")
        return seq if isinstance(seq, int) else -1

    @staticmethod
    def _entry_sse_event(entry: dict[str, Any]) -> ServerSentEvent:
        """entry event: the SSE ``id`` field is the seq; the frontend streaming client resumes with Last-Event-ID on
        reconnect.

        entry 事件：SSE ``id`` 字段即 seq，前端流式客户端重连时以 Last-Event-ID 续传。"""
        return ServerSentEvent(event="entry", data=entry, id=str(entry.get("seq")))

    _TERMINAL_STATUSES: ClassVar[frozenset[SessionStatus]] = frozenset(
        {"idle", "running", "completed", "error", "interrupted"}
    )

    def _check_runtime_status_terminal(self, message: dict[str, Any], session_id: str) -> ServerSentEvent | None:
        """Return a status SSE event if *message* carries a terminal runtime status."""
        runtime_status = str(message.get("status") or "").strip()
        if runtime_status in self._TERMINAL_STATUSES:
            return self._sse_event(
                "status",
                self._build_status_event_payload(
                    status=runtime_status,
                    session_id=session_id,
                    result_message=message,
                ),
            )
        return None

    @staticmethod
    def _sse_event(event: str, data: dict[str, Any]) -> ServerSentEvent:
        """Build an SSE event for FastAPI's EventSourceResponse."""
        return ServerSentEvent(event=event, data=data)

    def _resolve_project_cwd_safe(self, project_name: str) -> Path | None:
        """Resolve the project's working directory, returning None on failure.

        ``SdkTranscriptAdapter`` needs ``project_cwd`` to derive the
        per-project key when reading from the SessionStore. If the project
        directory is missing (deleted, never materialized in tests, etc.)
        we fall back to None — the store helper / SDK defaults handle that.
        """
        try:
            return self.pm.get_project_path(project_name)
        except (FileNotFoundError, ValueError):
            return None

    async def _aresolve_project_cwd_safe(self, project_name: str) -> Path | None:
        """Async entry for ``_resolve_project_cwd_safe``: probing the project directory does a stat, so offload it to a
        thread.

        ``_resolve_project_cwd_safe`` 的异步入口：项目目录探测会 stat，卸到线程。"""
        return await asyncio.to_thread(self._resolve_project_cwd_safe, project_name)

    @staticmethod
    def _build_status_event_payload(
        status: SessionStatus,
        session_id: str,
        result_message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build normalized status event payload."""
        message = result_message if isinstance(result_message, dict) else {}
        subtype = message.get("subtype")
        stop_reason = message.get("stop_reason")
        is_error = bool(message.get("is_error"))

        if status == "error" and subtype is None:
            subtype = "error"
        if status == "error":
            is_error = True

        payload: dict[str, Any] = {
            "status": status,
            "subtype": subtype,
            "stop_reason": stop_reason,
            "is_error": is_error,
            "session_id": session_id,
        }
        api_error_status = message.get("api_error_status")  # SDK 0.1.76+
        if api_error_status is not None:
            payload["api_error_status"] = api_error_status
        return payload

    async def _with_session_metadata(
        self,
        payload: dict[str, Any],
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Normalize outward-facing event payloads."""
        normalized = dict(payload)
        normalized["session_id"] = session_id
        normalized.pop("sdk_session_id", None)
        return normalized

    # ==================== Lifecycle ====================

    async def shutdown(self) -> None:
        """Shutdown service gracefully."""
        await self.session_manager.shutdown_gracefully()

    # ==================== Skills ====================

    # Lucide icon hint for each user-invocable skill. The display name is
    # **not** stored here — the frontend resolves it from i18n
    # ``dashboard:skill_name_<id>`` (single source of truth for skill labels
    # lives in ``frontend/src/i18n/{zh,en,vi}/dashboard.ts``).
    # ``tests/unit/test_frontend_skill_i18n.py`` cross-checks SKILL.md against
    # those keys so adding a user-invocable skill without translations fails CI.
    _SKILL_ICONS: ClassVar[dict[str, str]] = {
        "video-workflow": "clapperboard",
        "generate-storyboard": "images",
        "generate-grid": "grid-2x2",
        "generate-video": "film",
        "generate-narration-audio": "audio-lines",
        "generate-assets": "users",
        "edit-video": "scissors",
    }

    def list_available_skills(self, project_name: str | None = None) -> list[dict[str, str]]:
        """List available skills."""
        if project_name:
            self.pm.get_project_path(project_name)

        source_roots = {
            "agent": agent_profile_dir() / ".claude" / "skills",
        }

        skills: list[dict[str, str]] = []
        seen_keys: set[str] = set()

        for scope, root in source_roots.items():
            if not root.exists() or not root.is_dir():
                continue
            try:
                directories = sorted(root.iterdir())
            except OSError:
                continue

            for skill_dir in directories:
                if not skill_dir.is_dir():
                    continue
                skill_file = self._resolve_skill_entry_file(skill_dir)
                if skill_file is None:
                    continue

                try:
                    metadata = self._load_skill_metadata(skill_file, skill_dir.name)
                except OSError:
                    continue

                if metadata is None:
                    continue

                if not metadata["user_invocable"]:
                    continue

                key = f"{scope}:{metadata['name']}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                skill_entry: dict[str, Any] = {
                    "name": metadata["name"],
                    "description": metadata["description"],
                    "scope": scope,
                    "path": str(skill_file),
                }
                icon = self._SKILL_ICONS.get(metadata["name"])
                if icon:
                    skill_entry["icon"] = icon
                skills.append(skill_entry)

        return skills

    @staticmethod
    def _resolve_skill_entry_file(skill_dir: Path) -> Path | None:
        # content_mode variants on the profile side (SKILL.narration.md / SKILL.drama.md) are only materialized as
        # SKILL.md when synced into the project directory; the list endpoint scans the profile directly and must
        # recognize variants itself, otherwise variant-only skills like video-workflow would never be found.
        #
        # The lookup contract matches tests/unit/test_frontend_skill_i18n.py:_find_skill_md: filter files strictly
        # with is_file, enumerate valid modes explicitly via sorted(VALID_CONTENT_MODES), check that common/variant
        # are mutually exclusive and the variants are complete, and that all variants have the same
        # name/user-invocable. Invalid shapes log a warning and return None, so the list does not randomly expose a
        # broken config of some mode.
        # profile 端的 content_mode 变体（SKILL.narration.md / SKILL.drama.md）只在 sync
        # 进项目目录时才会被物化为 SKILL.md；列表接口直接扫 profile 时必须自己识别变体，
        # 否则 video-workflow 这类 variant-only skill 永远拿不到。
        #
        # 查找契约与 tests/unit/test_frontend_skill_i18n.py:_find_skill_md 保持一致：
        # 用 is_file 严格筛文件、按 sorted(VALID_CONTENT_MODES) 显式枚举有效模式、
        # 校验 common/variant 互斥、变体完整，且所有变体的 name/user-invocable 一致。
        # 非法形态 warning 后返回 None，避免列表随机暴露某个 mode 的破损配置。
        variants = [skill_dir / f"SKILL.{mode}.md" for mode in sorted(VALID_CONTENT_MODES)]
        existing = [v for v in variants if v.is_file()]
        common = skill_dir / "SKILL.md"
        if common.is_file():
            if existing:
                logger.warning("skill %s 同时存在 common 与 content_mode 变体，跳过", skill_dir.name)
                return None
            return common
        if not existing:
            return None
        if len(existing) != len(variants):
            missing = [path.name for path in variants if path not in existing]
            logger.warning("skill %s 的 content_mode 变体不完整，缺少 %s，跳过", skill_dir.name, missing)
            return None
        try:
            metadata = [AssistantService._load_skill_metadata(v, skill_dir.name) for v in existing]
        except OSError:
            return None
        if any(item is None for item in metadata):
            return None
        identities = {(item["name"], item["user_invocable"]) for item in metadata if item is not None}
        if len(identities) > 1:
            logger.warning(
                "skill %s 各 content_mode 变体的 name 或 user-invocable 不一致，跳过；"
                "请保证所有 SKILL.<mode>.md frontmatter 身份一致",
                skill_dir.name,
            )
            return None
        return existing[0]

    @staticmethod
    def _load_skill_metadata(skill_file: Path, fallback_name: str) -> dict[str, Any] | None:
        """Load skill metadata from SKILL.md frontmatter.

        Parsed fields: name, description, user-invocable.
        """
        try:
            content = skill_file.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            logger.warning("invalid skill encoding in %s: %s; skipping", skill_file, exc)
            return None
        if content.lstrip().startswith("---"):
            try:
                metadata = parse_profile_metadata(skill_file)
            except FrontmatterError as exc:
                logger.warning("invalid skill frontmatter in %s: %s; skipping", skill_file, exc)
                return None
            return {
                "name": metadata.name,
                "description": metadata.description,
                "user_invocable": metadata.user_invocable,
            }

        # Keep legacy body-only Skills readable; shipped profile files are required
        # to have YAML frontmatter by the static profile lint.
        name = fallback_name
        description = ""
        user_invocable = True
        for line in content.splitlines():
            text = line.strip()
            if text and not text.startswith("#"):
                description = text
                break

        return {
            "name": name,
            "description": description,
            "user_invocable": user_invocable,
        }
