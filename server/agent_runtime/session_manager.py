"""
Manages ClaudeSDKClient instances with background execution and reconnection support.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional
from uuid import uuid4

from lib.agent.agent_memory_paths import project_memory_dir
from lib.agent.credential_tiers import AgentPricing
from lib.db.base import DEFAULT_USER_ID
from lib.i18n import DEFAULT_LOCALE
from lib.infra.data_root_layout import DataRootLayout
from lib.infra.logging_utils import redact_diagnostic_text
from lib.infra.path_safety import PathTraversalError, safe_join
from server.agent_runtime.agent_access_policy import AgentAccessPolicy
from server.agent_runtime.entry_pipeline import SessionEntryPipeline
from server.agent_runtime.event_log import (
    REPLAYED_USER_ECHO_ENTRY_UUID_KEY,
    REPLAYED_USER_ECHO_KEY,
    EventLogStore,
    build_user_entry,
)
from server.agent_runtime.failure_observation import (
    build_startup_failure_observation,
    failure_observation_json,
)
from server.agent_runtime.message_serialization import (
    is_main_turn_activity,
    message_to_dict,
    utc_now_iso,
)
from server.agent_runtime.models import (
    Heartbeat,
    LiveMessage,
    QueuedMessage,
    QueuedMessageState,
    SessionMeta,
    SessionStatus,
    SessionStreamEvent,
    SubscriptionReady,
    WithdrawalIntent,
)
from server.agent_runtime.options_assembler import OptionsAssembler, agent_pricing_of
from server.agent_runtime.result_status import resolve_result_status
from server.agent_runtime.sdk_frames import CommandLifecycle
from server.agent_runtime.session_actor import SessionActor, SessionCommand
from server.agent_runtime.session_store import SessionMetaStore
from server.agent_runtime.usage_extraction import ModelTurnUsage, TurnUsageMeter
from server.sse_channel import IDLE, EvictNonCriticalAndSignal, SseChannel

logger = logging.getLogger(__name__)

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    SettingSource,
)

from lib.backends.providers import PROVIDER_ANTHROPIC, CallStatus
from lib.billing.ledger import Ledger
from lib.billing.metering import Usage, token_usage
from lib.config.service import ConfigService
from lib.db import async_session_factory
from lib.db.repositories.usage_repo import AgentModelCall
from lib.i18n.display_names import agent_provider_id

SDK_AVAILABLE = True


# Inbox backlog warning threshold: 100 stream_event within ~1s (the typical upper bound of streaming frequency);
# staying above it means _process_inbox is blocked or downstream I/O is very slow.
# inbox 积压告警阈值：~1s 内 100 条 stream_event（典型流式频率上限）；
# 持续高于此值说明 _process_inbox 被阻塞或下游 I/O 超慢。
_INBOX_BACKLOG_WARN_THRESHOLD = 100
# Reset the warning state only after dropping below this level, to avoid flapping log spam.
_INBOX_BACKLOG_RESET_THRESHOLD = 50  # 降至此水位以下才重置告警状态，避免抖动刷屏


class _StartupStderrCollector:
    """Collect stderr losslessly until sdk_session_id is ready, then stop and release it.

    在 sdk_session_id 就绪前无损收集 stderr，随后停止并释放。"""

    def __init__(self) -> None:
        self._active = True
        self._lines: list[str] = []

    def __call__(self, line: str) -> None:
        logger.warning("claude_agent_sdk stderr: %s", redact_diagnostic_text(line))
        if self._active:
            self._lines.append(line)

    def render(self) -> str:
        return "\n".join(self._lines)

    def stop(self) -> None:
        self._active = False
        self._lines.clear()


class SessionCapacityError(Exception):
    """All concurrency slots are taken by running sessions; no new connection can be created.

    所有并发槽位已被 running 会话占满，无法创建新连接。"""


class UnrecordedMessageError(Exception):
    """The message hit by a same-key retry was accepted by the Agent but could not be written to the event log: do not
    send it to the CLI again, so the same message is not executed twice.

    同键重试命中的消息已被 Agent 接纳，但没能写入事件日志：不再送入 CLI，避免同一消息执行两次。"""


class QueuedMessageNotFoundError(Exception):
    """The message to withdraw is not one this session queued (or the session is no longer in memory and its queued
    messages were cleared with it).

    要撤回的消息不是本会话排队过的消息（或会话已不在内存中，排队消息随之清理）。"""


class QueuedMessageWithdrawalPendingError(Exception):
    """A withdrawal of the same queued message is still waiting for the CLI's reply.

    同一条排队消息的撤回仍在等待 CLI 答复。"""


WithdrawalOutcome = Literal["withdrawn", "accepted"]
"""撤回排队消息的结果：``withdrawn`` 已撤回并移出排队，``accepted`` 已被 Agent 接收、照常进入对话。"""

SendNowOutcome = Literal["sent", "accepted"]
"""立即发送的结果：``sent`` 已以 now 优先级重新送入，``accepted`` 已被 Agent 接收、不再重发。"""


class AgentStartupError(RuntimeError):
    """Exception carrying the SDK stderr when ClaudeSDKClient fails to start.

    Inside the SDK, ``ProcessError`` is raised for a non-zero subprocess exit, but its ``stderr`` field is
    hard-coded to ``"Check stderr output for details"`` -- the real stderr is only available through the
    ``ClaudeAgentOptions.stderr`` callback. This exception packs the stderr lines collected by the callback and
    passes them through to the router/frontend, so the user can see the installation guidance the SDK gives (for
    example the download link when bash.exe / pwsh.exe is missing on Windows).

    ``__str__`` returns the full concatenation of message + stderr, so the router's generic ``except Exception:
    str(exc)`` branch passes it through automatically without a dedicated catch on every path.

    ClaudeSDKClient 启动失败时携带 SDK stderr 的异常。

    SDK 内部用 ``ProcessError`` 抛子进程非 0 退出，但其 ``stderr`` 字段写死为
    ``"Check stderr output for details"`` —— 真实 stderr 只能通过
    ``ClaudeAgentOptions.stderr`` 回调拿到。本异常把回调收集的 stderr 行打包
    透传给 router/前端，让用户能看到 SDK 给出的安装指引（例如 Windows 缺
    bash.exe / pwsh.exe 时的下载链接）。

    ``__str__`` 直接返回 message + stderr 的完整拼接，让 router 的通用
    ``except Exception: str(exc)`` 分支也能自动透传，不需要每条路径都加专门
    捕获。
    """

    def __init__(
        self,
        message: str,
        sdk_stderr: str = "",
        *,
        failure_observation: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.sdk_stderr = sdk_stderr
        self.failure_observation = failure_observation
        super().__init__(self._compose())

    def _compose(self) -> str:
        if self.sdk_stderr:
            return f"{self.message}\n\n{self.sdk_stderr}"
        return self.message


def _make_agent_startup_error(
    exc: BaseException,
    *,
    project_name: str,
    session_id: str | None,
    sdk_stderr: str = "",
) -> AgentStartupError:
    """At the runtime boundary, uniformly keep, redact and wrap what was actually observed during startup.

    在 runtime 边界统一保留、脱敏并包装启动阶段实际观测。"""
    failure = build_startup_failure_observation(
        exc,
        project_name=project_name,
        session_id=session_id,
        sdk_stderr=sdk_stderr,
    )
    logger.error(
        "Agent 启动失败 project_name=%s session_id=%s failure_observation=%s",
        project_name,
        session_id,
        failure_observation_json(failure),
    )
    return AgentStartupError(
        redact_diagnostic_text(exc),
        sdk_stderr=redact_diagnostic_text(sdk_stderr),
        failure_observation=failure,
    )


@dataclass
class PendingQuestion:
    """Tracks a pending AskUserQuestion request."""

    question_id: str
    payload: dict[str, Any]
    answer_future: asyncio.Future[dict[str, str]]


@dataclass(frozen=True)
class _ActorExitNotice:
    """Completion event of the Actor; the exception is handed to the inbox processor in queue order.

    Actor 的完成事件；异常随队列顺序交给 inbox processor 处理。"""

    error: BaseException | None = None


@dataclass(frozen=True)
class _CliIdleNotice:
    """The CLI reports idle; ``epoch`` is the enter-running count at the moment this frame was read, and the inbox
    handles it after this turn's entries.

    CLI 报告空闲；``epoch`` 是读到这一帧时的进入 running 计数，由 inbox 在本轮条目之后处理。"""

    epoch: int


@dataclass(frozen=True)
class _CliBusyNotice:
    """The CLI reports it started working, so the session switches in from non-running: the inbox persists it and sends
    the autonomous-turn notification.

    CLI 报告开始工作，会话因此从非 running 切入：由 inbox 持久化并发出自主轮次通知。"""


@dataclass(frozen=True)
class _QueuedMessageSettled:
    """The CLI reports what happened to a queued message (merged into a turn, or dropped); the inbox handles it after
    the preceding output.

    CLI 报告一条排队消息的去向（被并入一轮，或被丢弃），由 inbox 在此前的输出之后处理。"""

    lifecycle: CommandLifecycle
    preempted: bool = False
    """CLI 报告时有立即发送的消息尚未被接纳：这次丢弃来自被它打断的那一轮。"""


def _user_message_frame(
    content: str | list[dict[str, Any]], cli_uuid: str, *, priority: Literal["now"] | None = None
) -> dict[str, Any]:
    """User message frame sent to the CLI. Only messages with a uuid get ``command_lifecycle`` frames, and replay keeps
    this uuid too.

    ``priority="now"`` makes the CLI interrupt the current turn and handle this message first (send immediately).

    送入 CLI 的用户消息帧。带 uuid 的消息才会有 ``command_lifecycle`` 帧，回放也保留这个 uuid。

    ``priority="now"`` 让 CLI 打断当前轮先处理这条消息（立即发送）。
    """
    frame: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "uuid": cli_uuid,
    }
    if priority is not None:
        frame["priority"] = priority
    return frame


def _agent_model_call(usage: ModelTurnUsage) -> AgentModelCall:
    """Turn one model's usage for a turn into billing input: each request yields one metering record under the usage
    contract, and the token columns hold the sum over all requests.

    一个模型一轮的用量转成记账输入：各次请求按用量契约各出一份计量，token 列记各次之和。"""
    total = usage.total
    return AgentModelCall(
        model=usage.model,
        requests=tuple(
            Usage(
                quantities=token_usage(
                    input_tokens=request.total_input,
                    output_tokens=request.output,
                    cache_read_tokens=request.cache_read,
                    cache_write_5m_tokens=request.cache_write_5m,
                    cache_write_1h_tokens=request.cache_write_1h,
                )
            )
            for request in usage.requests
        ),
        input_tokens=total.total_input,
        output_tokens=total.output,
        cache_read_tokens=total.cache_read,
        cache_write_5m_tokens=total.cache_write_5m,
        cache_write_1h_tokens=total.cache_write_1h,
    )


def _content_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text")


def _make_session_channel() -> SseChannel:
    """Session subscription broadcast channel: the overflow policy is "evict non-critical messages + overflow signal".

    Critical messages (result/runtime_status/log_entry/log_turn_complete/queued_message) must not be dropped
    silently; when a subscriber's queue cannot keep up at all, its stream is ended, and the end of the stream is the
    reconnect signal (see docs/adr/0046).

    会话订阅广播通道：溢出策略为「逐出非关键消息 + 溢出信号」。

    关键消息（result/runtime_status/log_entry/log_turn_complete/queued_message）不得静默丢弃；订阅者
    队列彻底跟不上时其流被结束，流结束即重连信号（见 docs/adr/0046）。
    """
    return SseChannel(
        overflow=EvictNonCriticalAndSignal(
            is_critical=lambda message: message.get("type") in ManagedSession._CRITICAL_MESSAGE_TYPES,
        ),
    )


@dataclass
class _DetachedQueue:
    """Queued messages and idempotency records taken by ``ManagedSession.detach_queued_messages``.

    ``ManagedSession.detach_queued_messages`` 取走的排队消息与幂等记录。"""

    messages: list[QueuedMessage]
    sent_message_entries: dict[str, str]
    sent_client_keys: dict[str, str]


@dataclass
class ManagedSession:
    """A managed ClaudeSDKClient session."""

    # sdk_session_id (existing session) or a temporary UUID (new session still waiting)
    session_id: str  # sdk_session_id（已有会话）或临时 UUID（新会话等待中）
    actor: "SessionActor"  # per-session actor owning the SDK client
    status: SessionStatus = "idle"
    project_name: str = ""  # Used by _register_new_session / 用于 _register_new_session
    sdk_id_event: asyncio.Event = field(default_factory=asyncio.Event)
    resolved_sdk_id: str | None = None  # Set by the consumer, read by send_new_session / consumer 设置，send_new_session 读取
    channel: SseChannel = field(default_factory=_make_session_channel)
    pending_questions: dict[str, PendingQuestion] = field(default_factory=dict)
    # Queued messages, in send order: already handed to the CLI but not yet accepted into the conversation. When the
    # CLI reports it started handling one (started), it is written to the log and removed.
    # 排队消息，按发送顺序：已交给 CLI、尚未被接纳进对话。CLI 报告开始处理（started）时写入日志并移出。
    queued_messages: list[QueuedMessage] = field(default_factory=list)
    # uuid of the message sent to the CLI -> uuid of the user entry: kept until session cleanup, so late duplicate
    # replays are also ignored idempotently.
    # 送入 CLI 的消息 uuid → 用户条目 uuid：保留到会话清理，迟到的重复回放同样幂等忽略。
    sent_message_entries: dict[str, str] = field(default_factory=dict)
    # Idempotency key -> user entry uuid: retries while queued use it to return the same queued message; after
    # acceptance the event log takes over.
    # 幂等键 → 用户条目 uuid：排队期间的重试凭此返回同一条排队消息，接纳后由事件日志承担。
    sent_client_keys: dict[str, str] = field(default_factory=dict)
    # uuids of messages sent with now priority that the CLI has not accepted yet. While it is non-empty, a turn ending
    # with aborted_* was interrupted by a send-immediately and is treated as an interruption; queued messages the CLI
    # dropped during that time, which have not entered the conversation yet, are sent again.
    # 以 now 优先级送入、CLI 尚未接纳的消息 uuid。非空期间以 aborted_* 结束的轮次是被立即发送打断的，
    # 按中断处理；这期间被 CLI 丢弃、还没进入对话的排队消息重新送入。
    now_messages_in_flight: set[str] = field(default_factory=set)
    # Serial lock for the send path: waiters get the lock in arrival order, so messages enter the CLI in the same
    # order as they were queued.
    # 发送路径的串行锁：等待者按到达顺序获锁，消息进入 CLI 的顺序与排队顺序一致。
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Event log write-point pipeline (the live write side of the UI timeline's single read source). / 事件日志写入点管道（UI 时间线唯一读源的 live 写侧）。
    entry_pipeline: SessionEntryPipeline | None = None
    # First user message of a new session: the inbox task writes it to the log (seq 0) once sdk_session_id is ready,
    # so the user entry gets its identity before any assistant entry.
    # 新会话首条用户消息：sdk_session_id 就绪后由 inbox 任务写入日志（seq 0），
    # 保证用户条目先于任何 assistant 条目分配身份。
    pending_initial_user_entry: dict[str, Any] | None = None
    initial_user_log_entry: dict[str, Any] | None = None
    # Exception from failing to persist the first user message: the inbox task records it, and send_new_session
    # reports the failure explicitly when it wakes up (the event log is the timeline's single read source; a missing
    # seq 0 is not acceptable).
    # 首条用户消息落库失败的异常：inbox 任务记录，send_new_session 醒来后
    # 据此显式回报失败（事件日志是时间线唯一读源，seq 0 缺失不可接受）。
    initial_user_entry_error: Exception | None = None
    # Prompt for usage billing: messages accepted before this turn's result are merged into the same turn and appended
    # in order; messages accepted after the result start a new turn.
    # 用量记账的 prompt：本轮 result 之前被接纳的消息并入同一轮，依次追加；result 之后被接纳的消息开新一轮。
    last_user_prompt: str = ""
    turn_usage_recorded: bool = True
    # The Agent credential injected into environment variables when the session connected, with its multiplier and
    # tier binding at that time: this connection's turns are priced by it (docs/adr/0017, 0102). When falling back to
    # system settings these are defaults (no credential, multiplier 1, no binding).
    # 会话连接时注入环境变量的 Agent 凭证及其当时的倍率与档位绑定：本连接的轮次按它计价（docs/adr/0017、0102）。
    # 回退到系统设置时为默认值（无凭证、倍率 1、没有绑定）。
    agent_pricing: AgentPricing = field(default_factory=AgentPricing)
    # Usage metering for this connection: the diff baseline between in-stream requests and model_usage
    # (docs/adr/0102).
    # 本连接的用量计量：流内请求与 model_usage 的差分基线（docs/adr/0102）。
    usage_meter: TurnUsageMeter = field(default_factory=lambda: TurnUsageMeter(resumed=False))
    interrupt_requested: bool = False
    last_activity: float | None = None  # updated on every send/receive
    # Outcome of the latest turn: recorded when a turn's result closes it, used when the session leaves running;
    # cleared when a new turn starts.
    # 最近一轮的结局：一轮的 result 收尾时记下，会话离开 running 时取用；开启新一轮时清空。
    turn_outcome: SessionStatus | None = None
    # Result of the latest turn (with session_status); when leaving running, its subtype and other fields are sent
    # along with the status.
    # 最近一轮的 result（带 session_status），离开 running 时随状态下发其 subtype 等字段。
    last_turn_result: dict[str, Any] | None = None
    # Count of enter-running signals: incremented when a message is delivered or when the CLI is read reporting
    # non-idle. When the CLI reports idle, the value at that moment is recorded; if the count has changed by the time
    # the inbox handles it, another message was delivered or the CLI started working again afterwards, so that idle is
    # stale.
    # 进入 running 的信号计数：送达消息、读到 CLI 报告非 idle 时递增。CLI 报 idle 时记下当时的值，
    # inbox 处理到它时若计数已变，说明其后又有消息送达或 CLI 又开始工作，这个 idle 已过时。
    _running_epoch: int = 0
    # The enter-running count at the last time the CLI was read reporting idle; equal to the current count means there
    # was no enter-running signal since.
    # 最近一次读到 CLI 报 idle 时的进入 running 计数；与当前计数相同说明此后没有进入 running 的信号。
    _cli_idle_epoch: int | None = None
    # The inbox has stopped processing: the actor still reads frames, but no longer switches to running when it reads
    # that the CLI started working, otherwise nobody would close the turn.
    # inbox 已停止处理：actor 仍在读帧，但读到 CLI 开始工作也不再切入 running，否则无人收尾。
    _inbox_stopped: bool = False
    _cleanup_task: asyncio.Task | None = None  # current cleanup timer (idle TTL or terminal delay)
    _inbox: asyncio.Queue = field(default_factory=asyncio.Queue)  # async post-processing queue
    _inbox_warned: bool = False  # edge-triggered backlog warning state
    _process_task: asyncio.Task | None = None  # per-session async inbox processor
    _interrupting: bool = False  # send_interrupt re-entry guard (distinct from interrupt_requested)

    # Message types that must never be silently dropped from subscriber queues.
    _CRITICAL_MESSAGE_TYPES: ClassVar[set[str]] = {
        "result",
        "runtime_status",
        "log_entry",
        "log_turn_complete",
        "queued_message",
    }

    def enter_running(self) -> bool:
        """A message reached the CLI or the CLI was read starting work: the session enters running. Returns whether
        this call switched in from non-running.

        Runs synchronously (the send path and the actor callbacks are both on the event loop); from this moment idle
        cleanup and eviction treat the session as in progress, and any idle read before this becomes stale.

        消息送达 CLI 或读到 CLI 开始工作：会话进入 running。返回本次是否从非 running 切入。

        同步执行（发送路径与 actor 回调都在事件循环上），这一刻起闲置清理与驱逐就都把会话
        当作进行中，此前读出的 idle 随之过时。
        """
        self._running_epoch += 1
        self.last_activity = time.monotonic()
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            self._cleanup_task = None
        if self.status == "running":
            return False
        self.status = "running"
        self.turn_outcome = None
        self.last_turn_result = None
        return True

    def abandon_running(self, epoch: int) -> None:
        """One ``enter_running`` did not get the message to the CLI; ``epoch`` is the count before entering.

        The count does not go back, otherwise a later entry would collide with an idle read earlier. When there was
        no other enter-running signal in the meantime and the CLI reported idle before or during this entry, send
        one extra idle notification with the current count: the idle read earlier may have been ignored as stale,
        and no later idle would settle the turn in progress.

        一次 ``enter_running`` 没把消息送到 CLI，``epoch`` 是进入前的计数。

        计数不回退，否则之后的进入会与此前读出的 idle 重号。期间没有其他进入 running 的信号、
        CLI 在这次进入之前或期间已报 idle 时，按当前计数补一次 idle 通知：此前读出的 idle 可能
        已被当作过时忽略，此后也不会再有 idle 结算进行中的轮次。
        """
        if self._running_epoch != epoch + 1 or self._cli_idle_epoch not in (epoch, epoch + 1):
            return
        self._cli_idle_epoch = self._running_epoch
        self._inbox.put_nowait(_CliIdleNotice(epoch=self._running_epoch))

    def _on_actor_message(self, msg: dict[str, Any]) -> None:
        """on_message callback of SessionActor. Synchronous, in-memory only, no await.

        Responsibility: broadcast the message to subscribers.

        **Leaving running is not done here** -- after the CLI reports idle, the inbox switches once all of this
        turn's entries are processed (see SessionManager._settle_cli_idle), so the status does not arrive before the
        last entry.

        pending_questions registration is handled by SessionManager._handle_special_message.

        SessionActor 的 on_message 回调。同步，内存操作，不 await。

        职责：向订阅者广播消息。

        **离开 running 不在此处做**——CLI 报 idle 后由 inbox 在本轮条目全部处理完
        之后切换（见 SessionManager._settle_cli_idle），状态才不会先于末条条目到达。

        pending_questions 注册由 SessionManager._handle_special_message 处理。
        """
        self.channel.broadcast(msg)

    async def send_query(self, message: dict[str, Any], sdk_session_id: str = "default") -> None:
        """Return right after handing the user message frame to the CLI: the CLI queues it, merges it into the current
        turn or starts a new turn itself, and the actor keeps reading messages in the background.

        把用户消息帧交给 CLI 后立即返回：CLI 自行排队、并入当前轮或另开一轮，消息由 actor 在后台持续读取。"""
        self.enter_running()
        cmd = SessionCommand(type="query", message=message, session_id=sdk_session_id)
        await self.actor.enqueue(cmd)
        await cmd.done.wait()
        if cmd.error is not None:
            self.status = "error"
            raise cmd.error

    def find_queued_message(
        self, *, cli_uuid: str | None = None, message_id: str | None = None
    ) -> QueuedMessage | None:
        for queued in self.queued_messages:
            if queued.cli_uuid == cli_uuid or queued.id == message_id:
                return queued
        return None

    def remove_queued_message(self, queued: QueuedMessage, **broadcast_fields: Any) -> None:
        """Remove from the queued list and broadcast; do nothing if it is no longer in the list. ``broadcast_fields``
        are sent along with the removal broadcast.

        移出排队列表并广播；已不在列表中时什么也不做。``broadcast_fields`` 随移出广播一同下发。"""
        if queued not in self.queued_messages:
            return
        self.queued_messages.remove(queued)
        self.channel.broadcast({"type": "queued_message", "op": "remove", "id": queued.id, **broadcast_fields})

    def move_queued_message_first(self, queued: QueuedMessage) -> None:
        """Move a queued message to the front and rebroadcast the whole snapshot: after a send-immediately, the tray
        order follows the CLI's processing order. Do nothing if it is no longer in the list.

        把一条排队消息移到最前并重播整份快照：立即发送后托盘顺序跟随 CLI 的处理顺序。已不在列表中时什么也不做。"""
        if queued not in self.queued_messages or self.queued_messages[0] is queued:
            return
        self.queued_messages.remove(queued)
        self.queued_messages.insert(0, queued)
        self.channel.broadcast(
            {
                "type": "queued_message",
                "op": "reorder",
                "messages": [item.to_payload() for item in self.queued_messages],
            }
        )

    def discard_queued_message(self, queued: QueuedMessage, **broadcast_fields: Any) -> None:
        """A queued message leaves without being accepted: remove it and release its idempotency key, so a same-key
        retry sends it again.

        排队消息未被接纳就离开：移出并释放幂等键，同键重试会重新送入。"""
        self.remove_queued_message(queued, **broadcast_fields)
        if queued.client_key is not None and self.sent_client_keys.get(queued.client_key) == queued.id:
            del self.sent_client_keys[queued.client_key]

    def withdraw_queued_message(self, queued: QueuedMessage) -> None:
        """A queued message leaves because the user withdrew it: the removal broadcast carries the withdraw intent,
        plus the content when editing, so the page that started the withdrawal can put it back into the input box.

        Whichever is handled first, the successful withdraw reply or the CLI's cancelled frame, goes through here;
        repeated calls do nothing.

        排队消息按用户撤回离开：移出广播带上撤回意图，编辑时附上内容，发起撤回的页面据此退回输入框。

        撤回成功的答复与 CLI 的 cancelled 帧谁先处理都走这里，重复调用什么也不做。
        """
        if queued not in self.queued_messages:
            return
        assert queued.withdrawal is not None
        queued.withdrawn = True
        fields: dict[str, Any] = {"withdrawn": queued.withdrawal}
        if queued.withdrawal == "edit":
            fields["message"] = queued.to_payload()
        self.discard_queued_message(queued, **fields)

    def was_sent(self, message_id: str) -> bool:
        """This message was once sent to this session's CLI as a queued message.

        这条消息曾作为排队消息送入本会话的 CLI。"""
        return message_id in self.sent_message_entries.values()

    def drop_queued_messages(self, *, state: QueuedMessageState | None = None) -> None:
        """Queued messages will no longer be processed: drop and broadcast. ``state`` drops only messages in that
        state; by default all are dropped.

        排队消息不会再被处理：丢弃并广播。``state`` 只丢弃该状态的消息，缺省全部丢弃。"""
        for queued in list(self.queued_messages):
            if state is None or queued.state == state:
                self.discard_queued_message(queued)

    def mark_queued_messages_unsent(self) -> None:
        """The CLI has exited: messages still queued become "unsent" and are broadcast, left for the user to decide
        whether to send, edit or delete them.

        CLI 已退出：仍在排队的消息转为「未发送」并广播，留给用户决定发送、编辑或删除。"""
        for queued in self.queued_messages:
            if queued.state != "queued":
                continue
            queued.state = "unsent"
            # The withdraw intent only matters for a later cancelled from the CLI, and the CLI is gone
            # 撤回意图只对 CLI 之后的 cancelled 有意义，CLI 已不在
            queued.withdrawal = None
            self.channel.broadcast({"type": "queued_message", "op": "upsert", "message": queued.to_payload()})

    @property
    def actor_exited(self) -> bool:
        task = self.actor.task
        return task is not None and task.done()

    def detach_queued_messages(self) -> _DetachedQueue:
        """Take the queued messages and idempotency records without broadcasting: handed to the new session object when
        rebuilding the connection, the tray stays as it is.

        取走排队消息与幂等记录，不广播：重建连接时交给新的会话对象，托盘保持原样。"""
        detached = _DetachedQueue(
            messages=self.queued_messages,
            sent_message_entries=self.sent_message_entries,
            sent_client_keys=self.sent_client_keys,
        )
        self.queued_messages = []
        self.sent_message_entries = {}
        self.sent_client_keys = {}
        return detached

    def adopt_queued_messages(self, detached: _DetachedQueue) -> None:
        self.queued_messages[:0] = detached.messages
        self.sent_message_entries = {**detached.sent_message_entries, **self.sent_message_entries}
        self.sent_client_keys = {**detached.sent_client_keys, **self.sent_client_keys}

    async def send_interrupt(self) -> None:
        if self._interrupting:
            return
        self._interrupting = True
        try:
            cmd = SessionCommand(type="interrupt")
            await self.actor.enqueue(cmd)
            await cmd.done.wait()
            if cmd.error is not None:
                raise cmd.error
        finally:
            self._interrupting = False

    async def send_disconnect(self) -> None:
        cmd = SessionCommand(type="disconnect", interrupt_first=self.status == "running")
        await self.actor.enqueue(cmd)
        await cmd.done.wait()
        await self.actor.wait()
        # A session still running at disconnect stays as it is; the session layer closes it to a terminal state from
        # the latest turn's outcome
        # 断开时仍在 running 的会话保持原状，由会话层按最近一轮的结局收尾为终态
        if self.status != "running":
            self.status = "closed"

    def add_pending_question(self, payload: dict[str, Any]) -> PendingQuestion:
        """Register a pending AskUserQuestion payload."""
        question_id = str(payload.get("question_id") or f"aq_{uuid4().hex}")
        payload["question_id"] = question_id
        future: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
        pending = PendingQuestion(
            question_id=question_id,
            payload=payload,
            answer_future=future,
        )
        self.pending_questions[question_id] = pending
        return pending

    def resolve_pending_question(self, question_id: str, answers: dict[str, str]) -> bool:
        """Resolve a pending AskUserQuestion with user answers."""
        pending = self.pending_questions.pop(question_id, None)
        if not pending:
            return False
        if not pending.answer_future.done():
            pending.answer_future.set_result(answers)
        return True

    def cancel_pending_questions(self, reason: str = "session closed") -> None:
        """Cancel all pending AskUserQuestion waiters."""
        for pending in list(self.pending_questions.values()):
            if not pending.answer_future.done():
                pending.answer_future.set_exception(RuntimeError(reason))
        self.pending_questions.clear()

    def get_pending_question_payloads(self) -> list[dict[str, Any]]:
        """Return unresolved AskUserQuestion payloads for reconnect snapshot."""
        return [pending.payload for pending in self.pending_questions.values()]


# Fields in the result sent along with the session status (the status event of the entry stream uses them to label the
# error cause).
# result 中随会话状态一并下发的字段（entry 流的 status 事件据此标注错误原因）。
_RESULT_STATUS_FIELDS = ("subtype", "stop_reason", "is_error", "api_error_status")


def _cli_session_state(msg: dict[str, Any]) -> str | None:
    """The status reported by the CLI's ``session_state_changed`` frame (idle / running / requires_action); None for
    other frames.

    CLI 的 ``session_state_changed`` 帧报告的状态（idle / running / requires_action）；其他帧为 None。"""
    if msg.get("type") != "system" or msg.get("subtype") != "session_state_changed":
        return None
    data = msg.get("data")
    state = data.get("state") if isinstance(data, dict) else None
    return state if isinstance(state, str) else None


class SessionManager:
    """Manages all active ClaudeSDKClient instances."""

    DEFAULT_ALLOWED_TOOLS: ClassVar[list[str]] = [
        "Skill",
        "Task",
        # -- Bash family (allowed jointly by sandbox enabled + autoAllowBashIfSandboxed=True) --
        # —— Bash 系列（sandbox 启用 + autoAllowBashIfSandboxed=True 协同放行）——
        "Bash",
        "BashOutput",
        "KillBash",
        # -- SDK built-in tools (still go through the PreToolUse hook file fence + settings.json deny) --
        # —— SDK 内置工具（仍走 PreToolUse hook 文件围栏 + settings.json deny）——
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "WebFetch",
        "AskUserQuestion",
    ]
    DEFAULT_SETTING_SOURCES: ClassVar[list[SettingSource]] = ["project"]

    def __init__(
        self,
        project_root: Path,
        meta_store: SessionMetaStore,
        data_root: Path | None = None,
        in_docker: bool = False,
        sandbox_enabled: bool = True,
        event_log_store: EventLogStore | None = None,
        sdk_id_timeout: float = 60.0,
    ):
        self.event_log_store = event_log_store or EventLogStore()
        # Upper limit for a new session waiting for CLI init to report sdk_session_id; a timeout means startup failed.
        # 新会话等待 CLI init 上报 sdk_session_id 的上限；超时即判启动失败。
        self._sdk_id_timeout = sdk_id_timeout
        self.project_root = Path(project_root)
        # Resolve both paths, so the relative_to check in _resolve_project_cwd does not fail with symlinks
        # (project_cwd is already resolved). strict=False tolerates a missing directory.
        # Tests construct SessionManager directly without going through
        # AssistantService, so we fall back to the default data root
        # ``project_root/projects``. Production passes the configured app_data_dir() explicitly.
        # 两路都 resolve，避免符号链接场景下 _resolve_project_cwd 的 relative_to
        # 校验失败（project_cwd 已经 resolve 过）。strict=False 容忍目录不存在。
        self.data_root = (
            Path(data_root).resolve(strict=False)
            if data_root is not None
            else (self.project_root / "projects").resolve()
        )
        self.layout = DataRootLayout(self.data_root)
        self.meta_store = meta_store
        self.sessions: dict[str, ManagedSession] = {}
        # Session subscription broadcast channels, registered by sdk_session_id. The entry stream's lifetime follows
        # the session panel, not the CLI process: after a session is evicted the channel stays with its subscribers,
        # and a revived session keeps broadcasting to the same channel. Removed when there is neither a resident
        # session nor a subscriber.
        # 会话订阅广播通道，按 sdk_session_id 登记。entry 流的生命周期跟随会话面板而非
        # CLI 进程：会话被驱逐后通道随订阅者留下，复活的会话接着往同一个通道广播。
        # 既无常驻会话、又无订阅者时摘除。
        self._channels: dict[str, SseChannel] = {}
        # Notification outlet for when the CLI starts a new turn on its own and the session returns to running (args:
        # project name, session id); see _persist_cli_resumed.
        # CLI 自主开启新一轮、会话回到 running 时的通知出口（参数：项目名、会话 id），
        # 见 _persist_cli_resumed。
        self._autonomous_turn_listener: Callable[[str, str], None] | None = None
        # Session being evicted -> event set when eviction completes; a connection is rebuilt as a cold session only
        # after eviction completes, never interleaved with it.
        # 正在驱逐的会话 → 驱逐完成时置位的事件；连接在驱逐完成后才按冷会话重建，不与它交错。
        self._disconnecting: dict[str, asyncio.Event] = {}
        # Wait limit for a graceful send_disconnect; after a timeout each call site falls back to an unbounded cancel.
        # 优雅 send_disconnect 的等待上限；超时后各调用点再走无界的 cancel 兜底。
        self._session_actor_shutdown_timeout: float = 15.0
        self._connect_locks: dict[str, asyncio.Lock] = {}
        # Cache of instance invariants: avoids redoing path resolve every time the access policy is built.
        # 实例不变量缓存：避免每次构建 access policy 都重做 path resolve。
        self._project_root_resolved = self.project_root.resolve()
        # Actual location of agent_runtime_profile: ``ARCREEL_PROFILE_DIR`` env override > ``self.project_root /
        # "agent_runtime_profile"`` (test-friendly: does not read the global ``lib.infra.env_init.PROJECT_ROOT``).
        # agent_runtime_profile 实际位置：``ARCREEL_PROFILE_DIR`` env 覆盖 >
        # ``self.project_root / "agent_runtime_profile"``（test-friendly：
        # 不读 ``lib.infra.env_init.PROJECT_ROOT`` 全局）。
        profile_override = os.getenv("ARCREEL_PROFILE_DIR", "").strip()
        if profile_override:
            self._agent_profile_root = Path(profile_override).expanduser().resolve(strict=False)
        else:
            self._agent_profile_root = (self._project_root_resolved / "agent_runtime_profile").resolve(strict=False)
        # Source of truth for access rules: env parsing (profile directory) is done here; the policy only consumes the
        # resolved process-level root paths (zero-I/O pure construction).
        # 访问规则真相源：env 解析（profile 目录）在此完成，policy 只消费
        # resolve 后的进程级根路径（零 I/O 纯构造）。
        self.access_policy = AgentAccessPolicy(
            project_root=self._project_root_resolved,
            data_root=self.data_root,
            agent_profile_root=self._agent_profile_root,
            sandbox_enabled=sandbox_enabled,
            in_docker=in_docker,
        )
        self._load_config()
        self.ledger = Ledger(session_factory=getattr(meta_store, "_session_factory", None))
        # Options assembler: holds dependencies, allows I/O, and builds the SDK options asynchronously. access_policy
        # / max_turns / session_factory / user_id are all fetched on demand through provider callbacks -- the first
        # two take effect immediately for later sessions after configure_sandbox_runtime / refresh_config replace
        # them; the last two keep the timing of lazily reading self attributes as before extraction, the same source
        # as _finalize_turn reading _user_id live on the usage-recording side, so the store and usage do not end up in
        # different per-user namespaces. _resolve_project_cwd (project name validation/scope) stays on the
        # session-management side and is injected as a dependency.
        # Options 装配器：持依赖、允许 I/O，异步 build 产出 SDK options。access_policy /
        # max_turns / session_factory / user_id 一律用 provider 回调现取——前两者
        # configure_sandbox_runtime / refresh_config 换新后对后续会话立即生效；后两者
        # 保持析出前惰性读 self 属性的时点，与用量记录侧 _finalize_turn 实时读 _user_id
        # 同源，避免 store 与用量落到不同 per-user 命名空间。_resolve_project_cwd
        # （项目名校验/作用域）留在会话管理侧，作为依赖注入。
        self._options_assembler = OptionsAssembler(
            data_root=self.data_root,
            allowed_tools=self.DEFAULT_ALLOWED_TOOLS,
            setting_sources=self.DEFAULT_SETTING_SOURCES,
            access_policy_provider=lambda: self.access_policy,
            max_turns_provider=lambda: self.max_turns,
            resolve_project_cwd=self._resolve_project_cwd,
            session_factory_provider=lambda: getattr(self, "_session_factory", None),
            user_id_provider=lambda: getattr(self, "_user_id", DEFAULT_USER_ID),
        )

    def configure_sandbox_runtime(self, *, in_docker: bool, sandbox_enabled: bool) -> None:
        """Inject platform runtime facts at startup (Docker nesting, kernel sandbox availability).

        ``in_docker`` is passed through to SandboxSettings.enableWeakerNestedSandbox; ``sandbox_enabled=False``
        (Windows fallback) turns off the SDK SandboxSettings and switches Bash tool calls to the code allowlist
        path. AgentAccessPolicy is immutable and is replaced as a whole rather than poking fields -- hooks / options
        building read ``self.access_policy`` at call time, so replacing it takes effect for all later sessions and
        tool calls.

        startup 期注入平台运行时事实（Docker 嵌套、内核沙箱可用性）。

        ``in_docker`` 透传 SandboxSettings.enableWeakerNestedSandbox；
        ``sandbox_enabled=False``（Windows 回退）关闭 SDK SandboxSettings 并把
        Bash 工具调用切到代码白名单路径。AgentAccessPolicy 不可变，整体换新
        而非戳改字段——hook / options 构建都在调用时读 ``self.access_policy``，
        换新即对后续所有会话与工具调用生效。
        """
        self.access_policy = replace(
            self.access_policy,
            in_docker=bool(in_docker),
            sandbox_enabled=bool(sandbox_enabled),
        )

    def _load_config(self) -> None:
        """Load configuration from environment (sync fallback)."""
        max_turns_env = os.environ.get("ASSISTANT_MAX_TURNS", "").strip()
        self.max_turns = int(max_turns_env) if max_turns_env else None

    async def refresh_config(self) -> None:
        """Reload configuration from ConfigService (DB), falling back to env."""
        try:
            from lib.config.service import ConfigService
            from lib.db import async_session_factory

            async with async_session_factory() as session:
                svc = ConfigService(session)
                raw = await svc.get_setting("assistant_max_turns", "")
                raw = raw.strip()
                if raw:
                    self.max_turns = int(raw)
                    return
        except Exception:
            logger.warning("从 DB 加载 assistant 配置失败，回退到环境变量", exc_info=True)
        # Fallback to env var
        self._load_config()

    async def _build_options(
        self,
        project_name: str,
        resume_id: str | None = None,
        can_use_tool: Callable[[str, dict[str, Any], Any], Any] | None = None,
        locale: str = DEFAULT_LOCALE,
        stderr: Callable[[str], None] | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Delegates to ``OptionsAssembler.build`` -- SessionManager no longer builds options and hooks directly, it
        only calls the assembler; credential injection, prompt assembly and the hook factory are all owned by the
        assembler.

        委派给 ``OptionsAssembler.build``——SessionManager 不再直接构建 options 与
        hook，仅调用装配器；凭证注入、prompt 装配、hook 工厂均由装配器持有。"""
        return await self._options_assembler.build(
            project_name,
            resume_id=resume_id,
            can_use_tool=can_use_tool,
            locale=locale,
            stderr=stderr,
            session_id=session_id,
        )

    def _build_session_store(self):
        """Delegates to the assembler's session store singleton. AssistantService gets the same store instance as the
        SDK options through this (same per-user namespace), sharing the read/write cache.

        委派给装配器的 session store 单例。AssistantService 经此拿到与 SDK options
        同一份 store 实例（同一 per-user 命名空间），读写共享缓存。"""
        return self._options_assembler.build_session_store()

    def _resolve_project_cwd(self, project_name: str) -> Path:
        """Resolve and validate per-session project working directory."""
        try:
            project_cwd = safe_join(self.layout.projects_dir, project_name)
        except PathTraversalError as exc:
            raise ValueError("invalid project name") from exc
        if not project_cwd.exists() or not project_cwd.is_dir():
            raise FileNotFoundError(f"project not found: {project_name}")
        return project_cwd

    def _build_entry_pipeline(self, managed: "ManagedSession") -> SessionEntryPipeline:
        """Build the session's event log write-point pipeline.

        session_id is fetched on demand via a provider: for a new session it is None until sdk_session_id is ready,
        and the pipeline skips automatically (in that window there is only the init system message, which is not
        logged anyway).

        构建会话的事件日志写入点管道。

        session_id 用 provider 现取：新会话在 sdk_session_id 就绪前为 None，
        管道自动跳过（该窗口内只有 init 系统消息，本就不入日志）。
        """
        return SessionEntryPipeline(
            self.event_log_store,
            session_id_provider=lambda: managed.resolved_sdk_id,
            project_name_provider=lambda: managed.project_name,
            broadcast=managed.channel.broadcast,
        )

    def _make_actor_message_callback(
        self,
        managed_ref: list["ManagedSession | None"],
    ) -> Callable[[Any], None]:
        """Sync on_message callback shared by send_new_session and get_or_connect.

        Runs inside the actor task. Order is load-bearing:
        CLI replays of user messages skip broadcast but still queue the message
        for async sdk_session_id capture; _handle_special_message must mutate
        result messages with `session_status` before subscribers see them via
        broadcast; _inbox hand-off last so async post-processing never
        observes a message that hasn't been broadcast yet.
        """

        def _on_message(raw_msg: Any) -> None:
            managed = managed_ref[0]
            if managed is None:
                return
            msg_dict = message_to_dict(raw_msg)
            entry_uuid = (
                managed.sent_message_entries.get(str(msg_dict.get("uuid"))) if msg_dict.get("type") == "user" else None
            )
            if entry_uuid is not None:
                # User message replayed by the CLI: the entry is written at acceptance time (started); replay only
                # links the transcript identity by uuid and is tagged so the write point skips it after recording the
                # mapping; repeated replays of the same uuid are skipped as well.
                # CLI 回放的用户消息：条目由接纳时刻（started）写入，回放只按 uuid 关联 transcript
                # 身份，打标让写入点落映射后跳过；同一 uuid 重复回放同样跳过。
                msg_dict[REPLAYED_USER_ECHO_KEY] = True
                msg_dict[REPLAYED_USER_ECHO_ENTRY_UUID_KEY] = entry_uuid
                managed._inbox.put_nowait(msg_dict)
                return
            self._handle_special_message(managed, msg_dict)
            managed._on_actor_message(msg_dict)
            managed._inbox.put_nowait(msg_dict)
            cli_state = _cli_session_state(msg_dict)
            if cli_state == "idle":
                managed._cli_idle_epoch = managed._running_epoch
                managed._inbox.put_nowait(_CliIdleNotice(epoch=managed._running_epoch))
            elif (
                (cli_state in ("running", "requires_action") or is_main_turn_activity(msg_dict))
                and not managed._inbox_stopped
                and managed.enter_running()
            ):
                # The CLI reports it started working, or produces a main-thread frame without reporting running:
                # switch in as soon as it is read, without waiting for the inbox -- idle cleanup must not disconnect
                # this turn the CLI started on its own, and any idle read before it becomes stale.
                # CLI 报告开始工作，或未报 running 就产出主线程帧：读到即切入，不等 inbox——
                # 闲置清理不能断开 CLI 自主开启的这一轮，在它之前读出的 idle 也随之过时。
                managed._inbox.put_nowait(_CliBusyNotice())

        return _on_message

    @staticmethod
    def _make_command_lifecycle_callback(
        managed_ref: list["ManagedSession | None"],
    ) -> Callable[[CommandLifecycle], None]:
        """The CLI reports what happened to a queued message: hand it to the inbox, processed after the output read
        before it.

        CLI 报告排队消息的去向：交给 inbox，排在此前读出的输出之后处理。"""

        def _on_command_lifecycle(lifecycle: CommandLifecycle) -> None:
            managed = managed_ref[0]
            if managed is None or lifecycle.state in ("queued", "completed"):
                return
            # Decide by frame order: the result and cancelled of the interrupted turn both arrive before the started
            # of the jumping-the-queue message
            # 按帧序判断：被打断那一轮的 result 与 cancelled 都先于插队消息的 started 到达
            preempted = bool(managed.now_messages_in_flight)
            managed.now_messages_in_flight.discard(lifecycle.command_uuid)
            if managed.find_queued_message(cli_uuid=lifecycle.command_uuid) is None:
                return
            managed._inbox.put_nowait(_QueuedMessageSettled(lifecycle, preempted=preempted))

        return _on_command_lifecycle

    def _make_actor_done_callback(
        self,
        managed: "ManagedSession",
    ) -> Callable[[asyncio.Task], None]:
        """Actor task done_callback: push inbox sentinel + persist error state.

        On actor task exit the inbox processor is signalled via None sentinel
        so it can drain cleanly. If the actor died with an exception, we flip
        the session to `error` in memory and schedule a meta_store persist so
        the DB doesn't stay stuck on `running` after a crash.
        """

        def _on_done(task: asyncio.Task) -> None:
            error: BaseException | None = None
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    logger.warning(
                        "session actor 异常退出 session_id=%s: %s",
                        managed.session_id,
                        redact_diagnostic_text(error),
                    )
            try:
                managed._inbox.put_nowait(_ActorExitNotice(error=error))
            except Exception:
                logger.debug("inbox sentinel push failed", exc_info=True)

        return _on_done

    async def send_new_session(
        self,
        project_name: str,
        content: str | list[dict[str, Any]],
        *,
        locale: str = DEFAULT_LOCALE,
        user_entry: dict[str, Any] | None = None,
        client_key: str | None = None,
    ) -> str:
        """Create a new session via send-first: start actor, send query, wait for sdk_session_id.

        ``user_entry`` is the event log entry for this user message (in its form after the write point finalizes
        it); once sdk_session_id is ready, the inbox task first writes the log (seq 0) and then releases the waiter,
        and the authoritative entry lands in ``managed.initial_user_log_entry`` for the acceptance response to
        return.

        Create a new session via send-first: start actor, send query, wait for sdk_session_id.

        ``user_entry`` 是本条用户消息的事件日志条目（写入点定型后的形态）；
        sdk_session_id 就绪后由 inbox 任务先写日志（seq 0）再放行等待，权威
        条目落在 ``managed.initial_user_log_entry`` 供受理响应回传。
        """
        if not SDK_AVAILABLE:
            exc = RuntimeError("claude_agent_sdk is not installed")
            raise _make_agent_startup_error(exc, project_name=project_name, session_id=None) from exc

        await self._ensure_capacity()
        temp_id = uuid4().hex
        managed_ref: list[ManagedSession | None] = [None]

        # The callback lives for the whole session but only collects during startup; right after actor.start()
        # succeeds it is stopped and cleared, which keeps the full evidence of startup failures while not letting
        # stderr of long sessions occupy memory.
        # 回调在整个会话中存活，但仅启动阶段收集；actor.start() 成功后立刻
        # 停止并清空，既完整保留启动故障证据，也不让长会话 stderr 占用内存。
        startup_stderr = _StartupStderrCollector()

        try:
            options = await self._build_options(
                project_name,
                resume_id=None,
                can_use_tool=await self._build_can_use_tool_callback(temp_id, managed_ref),
                locale=locale,
                stderr=startup_stderr,
            )
        except Exception as exc:
            sdk_stderr = startup_stderr.render()
            startup_stderr.stop()
            raise _make_agent_startup_error(
                exc,
                project_name=project_name,
                session_id=None,
                sdk_stderr=sdk_stderr,
            ) from exc
        actor = SessionActor(
            client_factory=lambda: ClaudeSDKClient(options=options),
            on_message=self._make_actor_message_callback(managed_ref),
            on_command_lifecycle=self._make_command_lifecycle_callback(managed_ref),
        )

        managed = ManagedSession(
            session_id=temp_id,
            actor=actor,
            status="running",
            project_name=project_name,
            agent_pricing=agent_pricing_of(options),
        )
        if user_entry is not None:
            managed.pending_initial_user_entry = {"entry": user_entry, "client_key": client_key}
        managed.entry_pipeline = self._build_entry_pipeline(managed)
        managed_ref[0] = managed
        managed.last_activity = time.monotonic()
        self.sessions[temp_id] = managed

        try:
            await actor.start()
        except Exception as exc:
            sdk_stderr = startup_stderr.render()
            startup_error = _make_agent_startup_error(
                exc, project_name=project_name, session_id=None, sdk_stderr=sdk_stderr
            )
            self.sessions.pop(temp_id, None)
            startup_stderr.stop()
            raise startup_error from exc
        # Register done callback BEFORE spawning processor to avoid a race
        # where the actor task completes before add_done_callback is attached，
        # leaving the completion notice un-pushed and _process_inbox hanging.
        actor.add_done_callback(self._make_actor_done_callback(managed))

        # Spawn inbox processor BEFORE sending query so we don't miss messages.
        managed._process_task = asyncio.create_task(
            self._process_inbox(managed),
            name=f"inbox-{temp_id}",
        )

        async def _cleanup_on_error() -> None:
            """Unified cleanup for failure paths after _process_task spawn.

            Runs send_disconnect first (which causes actor to exit and
            _on_actor_done to push the None sentinel, letting _process_inbox
            finish naturally), bounded by _session_actor_shutdown_timeout so an
            SDK-side hang does not stall this error-only cleanup path on the
            graceful wait — on timeout the actor is cancelled so it does not leak
            with the failed session; then belt-and-suspenders cancels the
            processor in case it is stuck elsewhere.
            """
            self.sessions.pop(temp_id, None)
            # Once sdk_session_id is ready the key swap has moved the session under the real id, so clear both keys.
            # sdk_session_id 就绪后 key swap 已把会话挂到正式 id 下，两个键都清。
            self.sessions.pop(managed.session_id, None)
            self._release_channel(managed.session_id)
            try:
                await asyncio.wait_for(managed.send_disconnect(), timeout=self._session_actor_shutdown_timeout)
            except TimeoutError:
                logger.warning(
                    "send_disconnect on error path 超时，走 cancel 兜底 session_id=%s",
                    temp_id,
                )
                await managed.actor.cancel_and_wait()
            except Exception:
                logger.exception(
                    "send_disconnect on error path failed session_id=%s",
                    temp_id,
                )
            # Still running after disconnect (send_disconnect does not rewrite running, and failure or timeout also
            # stops here), and cancelling _process_task below makes the CancelledError branch of _process_inbox write
            # an interrupted terminal state based on this. A startup failure is not an interruption, so set error
            # first to settle this check.
            # 断开后仍在 running（send_disconnect 不改写 running，失败或超时也停在这里），
            # 而下面取消 _process_task 会让 _process_inbox 的 CancelledError
            # 分支据此写 interrupted 终态。启动失败不是中断，先落 error 收口这条判断。
            if managed.status == "running":
                managed.status = "error"
            if managed._process_task is not None and not managed._process_task.done():
                managed._process_task.cancel()
                await asyncio.gather(managed._process_task, return_exceptions=True)
            startup_stderr.stop()

        # The first message does not go through the queued list: the inbox writes it to the log (seq 0) once
        # sdk_session_id is ready. Register the uuid so the CLI's replay recognizes it, only linking the transcript
        # identity without persisting it twice.
        # 首条消息不经排队列表：sdk_session_id 就绪后由 inbox 写入日志（seq 0）。
        # 登记 uuid 让 CLI 的回放认出自己，只关联 transcript 身份、不二次落库。
        cli_uuid = str(uuid4())
        if user_entry is not None:
            managed.sent_message_entries[cli_uuid] = str(user_entry["uuid"])
        managed.last_user_prompt = _content_text(content)
        managed.turn_usage_recorded = False

        try:
            await managed.send_query(_user_message_frame(content, cli_uuid))
        except Exception as exc:
            sdk_stderr = startup_stderr.render()
            startup_error = _make_agent_startup_error(
                exc, project_name=project_name, session_id=None, sdk_stderr=sdk_stderr
            )
            await _cleanup_on_error()
            raise startup_error from exc

        # Wait for sdk_session_id with timeout. Monitor the inbox processor rather
        # than the actor task: actor may exit immediately after enqueueing init,
        # while the processor still has to persist that already-observed session id.
        # The done callback appends a completion notice after all actor messages, so processor
        # completion is the safe proof that no queued init remains to be consumed.
        event_task = asyncio.create_task(managed.sdk_id_event.wait())
        processor_task = managed._process_task
        assert processor_task is not None
        watch_tasks: set[asyncio.Task] = {event_task, processor_task}
        actor_task = actor.task
        try:
            await asyncio.wait(
                watch_tasks,
                timeout=self._sdk_id_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not event_task.done():
                event_task.cancel()

        if not managed.sdk_id_event.is_set():
            startup_exception = None
            if processor_task.done() and not processor_task.cancelled():
                try:
                    startup_exception = processor_task.result()
                except Exception as exc:
                    # When _process_inbox completes normally it passes the actor startup exception as its return
                    # value; if it crashes unexpectedly itself, Task.result() re-raises. Both are evidence of startup
                    # failure and must go through the unified cleanup below first, then be wrapped as a structured
                    # AgentStartupError.
                    # _process_inbox 正常完成时以返回值传递 actor 启动异常；它自身
                    # 意外崩溃时 Task.result() 会重新抛出。两者都属于启动失败证据，
                    # 必须先走下方统一清理，再包装为结构化 AgentStartupError。
                    startup_exception = exc
            if actor_task is not None and actor_task.done():
                logger.error("session actor 提前退出，未获得 sdk_session_id temp_id=%s", temp_id)
            else:
                logger.error("等待 sdk_session_id 超时 temp_id=%s", temp_id)
            managed.cancel_pending_questions("session creation timed out")
            sdk_stderr = startup_stderr.render()
            await _cleanup_on_error()
            if startup_exception is not None:
                raise _make_agent_startup_error(
                    startup_exception,
                    project_name=project_name,
                    session_id=None,
                    sdk_stderr=sdk_stderr,
                ) from startup_exception
            raise TimeoutError("SDK 会话创建超时")

        startup_stderr.stop()

        sdk_id = managed.resolved_sdk_id
        assert sdk_id is not None
        # Key swap already done in _on_sdk_session_id_received
        assert managed.session_id == sdk_id

        if managed.initial_user_entry_error is not None:
            # Failure to persist the first user message is an acceptance failure: the event log is the timeline's
            # single read source, and a session start missing seq 0 can never be shown. Same semantics as the regular
            # acceptance path -- the failure is reported explicitly (the caller receives an exception), the status is
            # written back as error, and the session does not keep running in the background. Clean up first, then
            # write back the status: the inbox writes a terminal state when handling the CLI's idle, and only an error
            # written after cleanup completes will not be overwritten concurrently.
            # 首条用户消息落库失败即受理失败：事件日志是时间线唯一读源，
            # seq 0 缺失的会话开头永远无法呈现。与常规受理路径同语义——
            # 失败显式回报（调用方收到异常）、状态回写 error、会话不再后台
            # 续跑。先清理再回写状态：inbox 处理 CLI 的 idle 时会写终态，
            # 清理完成后写入的 error 才不会被并发覆盖。
            managed.cancel_pending_questions("initial user entry persist failed")
            # Set the in-memory state to error early: when _cleanup_on_error cancels _process_task, the CancelledError
            # branch of _process_inbox decides from status == "running" whether to go to the interrupted terminal
            # state; setting it early avoids an extra interrupted write and an extra status-change broadcast, while DB
            # persistence still waits until cleanup completes.
            # 提前置内存态为 error：_cleanup_on_error 取消 _process_task 时，
            # _process_inbox 的 CancelledError 分支会依据 status == "running"
            # 判断是否需要走 interrupted 终态；提前置位避免多写一次 interrupted
            # 并广播一次多余的状态跳变，DB 落库仍留到 cleanup 完成之后。
            managed.status = "error"
            await _cleanup_on_error()
            try:
                await self.meta_store.update_status(sdk_id, "error")
            except Exception:
                logger.exception("持久化 error 状态失败 session_id=%s", sdk_id)
            raise RuntimeError("新会话首条用户消息写入事件日志失败") from managed.initial_user_entry_error

        return sdk_id

    async def _process_inbox(self, managed: ManagedSession) -> BaseException | None:
        """Drain ManagedSession._inbox and run async post-processing.

        Replaces the async tail of _consume_messages. The synchronous bits
        (state machine, buffer add, broadcast, _handle_special_message,
        replay tagging) already ran inside the actor's on_message
        callback, so this coroutine only handles:
        - sdk_session_id capture (DB create, tag, key swap, event set)
        - writing a queued message to the log once the CLI starts it
        - _finalize_turn on result messages
        - leaving running when the CLI reports idle or the actor exits
        - terminal status on cancel/error
        """
        try:
            while True:
                msg_dict = await managed._inbox.get()
                if isinstance(msg_dict, _ActorExitNotice):
                    # The CLI has exited and messages still queued will not be processed: they become "unsent" and
                    # stay in the tray, without automatic resend
                    # CLI 已退出，仍在排队的消息不会再被处理：转为「未发送」留在托盘，不自动重发
                    managed.mark_queued_messages_unsent()
                    if msg_dict.error is not None:
                        if managed.resolved_sdk_id is not None:
                            await self._mark_session_terminal(managed, "error", "session actor failed")
                        else:
                            managed.status = "error"
                    elif managed.status == "running" and managed.resolved_sdk_id is not None:
                        # The CLI exited without reporting idle: if this turn was already closed, use its outcome,
                        # otherwise record it as interrupted.
                        # CLI 退出时没报 idle：这一轮若已收尾取其结局，否则记为中断。
                        await self._mark_session_terminal(
                            managed, managed.turn_outcome or "interrupted", "session actor exited"
                        )
                    return msg_dict.error
                if isinstance(msg_dict, _CliIdleNotice):
                    # When persisting the first user message failed, short-circuit as in the result branch;
                    # send_new_session writes error as the terminal state
                    # 首条用户消息落库失败时同 result 分支短路，终态由 send_new_session 写 error
                    if managed.initial_user_entry_error is None:
                        await self._settle_cli_idle(managed, msg_dict.epoch)
                    continue
                if isinstance(msg_dict, _CliBusyNotice):
                    await self._persist_cli_resumed(managed)
                    continue
                if isinstance(msg_dict, _QueuedMessageSettled):
                    await self._settle_queued_message(managed, msg_dict.lifecycle, preempted=msg_dict.preempted)
                    continue
                if msg_dict is None:
                    return None
                depth = managed._inbox.qsize()
                if not managed._inbox_warned and depth >= _INBOX_BACKLOG_WARN_THRESHOLD:
                    managed._inbox_warned = True
                    logger.warning(
                        "inbox backlog 过深 session_id=%s depth=%d (async post-processing 跟不上)",
                        managed.session_id,
                        depth,
                    )
                elif managed._inbox_warned and depth <= _INBOX_BACKLOG_RESET_THRESHOLD:
                    managed._inbox_warned = False
                # Short-circuit once sdk_session_id is captured: stream_event
                # messages can be very high-frequency and _extract_sdk_session_id
                # only yields on the init system message.
                if managed.resolved_sdk_id is None:
                    try:
                        await self._on_sdk_session_id_received(managed, None, msg_dict)
                    except Exception:
                        logger.exception(
                            "sdk_session_id 处理失败 session_id=%s",
                            managed.session_id,
                        )
                if is_main_turn_activity(msg_dict):
                    # Main-thread output means a new turn has started; the previous turn's outcome is void / 主线程产出意味着新一轮已开始，上一轮的结局作废
                    managed.turn_outcome = None
                # Event log write point: once sdk_session_id is ready, entries are finalized into the log one by one.
                # handle_message swallows exceptions internally and will not interrupt session consumption.
                # 事件日志写入点：sdk_session_id 就绪后逐条定型入日志。
                # handle_message 内部吞异常，不会打断会话消费。
                if managed.entry_pipeline is not None and managed.resolved_sdk_id is not None:
                    await managed.entry_pipeline.handle_message(msg_dict)
                managed.usage_meter.observe(msg_dict)
                if msg_dict.get("type") == "result":
                    if managed.initial_user_entry_error is not None:
                        # Persisting the first user message already failed, and send_new_session's error-cleanup path
                        # is about to cancel this task; short-circuit here without finalize, to avoid first
                        # broadcasting/persisting a non-error terminal state (such as completed) that is then
                        # rewritten to error.
                        # 首条用户消息落库已失败，send_new_session 的错误清理路径
                        # 即将取消本任务；此处短路不再 finalize，避免先广播/落库
                        # 非 error 终态（如 completed），随后又被改写为 error。
                        continue
                    try:
                        await self._finalize_turn(managed, msg_dict)
                    except Exception:
                        # A finalize failure means status/interrupt_requested/cleanup may be partly unfinished; go to
                        # the terminal fallback instead of continuing the loop -- continuing would let the next turn
                        # see inconsistent leftover state.
                        # finalize 失败意味着 status/interrupt_requested/cleanup 可能部分未完成；
                        # 走终态兜底而非继续循环——继续会让下一轮看到不一致的残留状态。
                        logger.exception(
                            "_finalize_turn 失败，走 error 终态兜底 session_id=%s",
                            managed.session_id,
                        )
                        # The inbox stops here; later frames are no longer processed / inbox 就此停止，之后的帧不再处理
                        managed._inbox_stopped = True
                        with contextlib.suppress(Exception):
                            await self._mark_session_terminal(managed, "error", "finalize failed")
                        return None
        except asyncio.CancelledError:
            # Only mark interrupted if session was actually running. Cancel can
            # also happen during failed send_new_session cleanup or normal
            # shutdown, where the status is already terminal / error.
            if managed.status == "running":
                try:
                    await self._mark_session_terminal(
                        managed, managed.turn_outcome or "interrupted", "session interrupted"
                    )
                except Exception:
                    logger.exception(
                        "_mark_session_terminal 在 cancel 路径失败 session_id=%s",
                        managed.session_id,
                    )
            raise
        except Exception:
            logger.exception("_process_inbox 异常 session_id=%s", managed.session_id)
            managed._inbox_stopped = True
            try:
                await self._mark_session_terminal(managed, "error", "session error")
            except Exception:
                logger.debug("_mark_session_terminal cleanup failed", exc_info=True)
            raise

    async def get_or_connect(
        self,
        session_id: str,
        *,
        meta: SessionMeta | None = None,
        locale: str = DEFAULT_LOCALE,
        resumable: bool = True,
    ) -> ManagedSession:
        """Get existing managed session or spin up an actor for resumed session.

        ``resumable=False`` is for sessions whose metadata row exists but whose transcript is empty (a branch forked
        by rewriting the first message): such a session has no history to resume, so a brand-new session is started
        with the identity preset via ``session_id=``. Once the first turn has run the transcript exists, and after
        that it is revived by resume as usual.

        Get existing managed session or spin up an actor for resumed session.

        ``locale`` only shapes the system prompt of a session's first turn: the
        system prompt is snapshotted into the session on its first request
        (``SystemPromptPreset.snapshot``), so a resumed session keeps the language
        regulation it started with and an already-resident session returns from
        cache. Here it matters for ``resumable=False`` sessions, which start fresh.

        ``resumable=False`` 用于元数据行已建、transcript 却是空的会话（改写第一条
        消息分叉出的分支）：这类会话没有历史可 resume，改以 ``session_id=`` 预指定
        身份开一个全新会话。首轮跑完 transcript 即存在，之后照常按 resume 复活。
        """
        cached = self.sessions.get(session_id)
        if cached is not None and session_id not in self._disconnecting and not cached.actor_exited:
            return cached

        while True:
            # Per-session lock prevents concurrent connect() for the same session_id.
            lock = self._connect_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                if self._connect_locks.get(session_id) is not lock:
                    # While waiting for the lock, eviction removed this lock: queue on the current lock instead, not
                    # connecting concurrently with the new lock's holder
                    # 等锁期间驱逐摘掉了这把锁：改到当前的锁上排队，不与新锁的持有者并发连接
                    continue
                cached, detached = await self._retire_exited_session(session_id)
                if cached is not None:
                    return cached
                try:
                    managed = await self._connect_resumed_session(
                        session_id, meta=meta, locale=locale, resumable=resumable
                    )
                except BaseException:
                    channel = self._channels.get(session_id)
                    if detached is not None and channel is not None:
                        # The old session was evicted and the new connection was not established: drop the queued
                        # messages as eviction does; subscribers still present remove them from the tray accordingly
                        # 旧会话已驱逐、新连接没建起来：同驱逐一样丢弃排队消息，仍在的订阅者随之移出托盘
                        for queued in detached.messages:
                            channel.broadcast({"type": "queued_message", "op": "remove", "id": queued.id})
                    raise
                if detached is not None:
                    managed.adopt_queued_messages(detached)
                return managed

    async def _retire_exited_session(self, session_id: str) -> tuple[ManagedSession | None, _DetachedQueue | None]:
        """Call inside the connection lock: return a resident session that is still usable, or, when there is no usable
        session, the queued messages that must move to a new connection.

        A session being evicted waits for the eviction to complete and is then rebuilt as a cold session. For a
        session whose actor has exited (the CLI exited, the session not yet cleaned up), first wait for the inbox to
        process the exit notification, then take the queued messages (without broadcasting; "unsent" messages stay
        in the tray and are not resent automatically) and evict it.

        连接锁内调用：返回仍可用的常驻会话，或在没有可用会话时返回需要转入新连接的排队消息。

        正在驱逐的会话等驱逐完成，此后按冷会话重建。actor 已退出（CLI 退出、会话尚未被清理）的会话
        先等 inbox 处理完退出通知，再取走排队消息（不广播，「未发送」消息留在托盘、不自动重发）并驱逐它。
        """
        detached: _DetachedQueue | None = None
        while (cached := self.sessions.get(session_id)) is not None:
            eviction = self._disconnecting.get(session_id)
            if eviction is not None:
                await eviction.wait()
                continue
            if not cached.actor_exited:
                return cached, None
            if cached._process_task is not None and not cached._process_task.done():
                # The exit notification turns queued messages into "unsent" and puts the session into a terminal
                # state; taking the queued messages must come after it
                # 退出通知把排队消息转为「未发送」、把会话落为终态，取走排队消息要排在它之后
                await asyncio.wait({cached._process_task})
                continue
            # If the inbox stopped before the actor, it did not process the exit notification; the CLI is gone all the
            # same
            # inbox 先于 actor 停止时没处理退出通知，CLI 同样已不在
            cached.mark_queued_messages_unsent()
            detached = cached.detach_queued_messages()
            await self._evict_one(cached)
        return None, detached

    async def _connect_resumed_session(
        self, session_id: str, *, meta: SessionMeta | None, locale: str, resumable: bool
    ) -> ManagedSession:
        """Start the actor via the cold-session path and register the session; the caller holds the connection lock.

        按冷会话路径启动 actor 并登记会话，调用方持有连接锁。"""
        if meta is None:
            meta = await self.meta_store.get(session_id)
            if meta is None:
                raise FileNotFoundError(f"session not found: {session_id}")

        if not SDK_AVAILABLE:
            exc = RuntimeError("claude_agent_sdk is not installed")
            raise _make_agent_startup_error(exc, project_name=meta.project_name, session_id=session_id) from exc

        await self._ensure_capacity()
        managed_ref: list[ManagedSession | None] = [None]

        # See the comment of the same name in send_new_session: collect losslessly only during startup and release
        # after success.
        # 见 send_new_session 同名注释：只在启动阶段无损收集，成功后释放。
        startup_stderr = _StartupStderrCollector()

        try:
            options = await self._build_options(
                meta.project_name,
                meta.id if resumable else None,  # SessionMeta.id is the sdk_session_id / SessionMeta.id 就是 sdk_session_id
                can_use_tool=await self._build_can_use_tool_callback(session_id, managed_ref),
                locale=locale,
                stderr=startup_stderr,
                session_id=None if resumable else meta.id,
            )
        except Exception as exc:
            sdk_stderr = startup_stderr.render()
            startup_stderr.stop()
            raise _make_agent_startup_error(
                exc,
                project_name=meta.project_name,
                session_id=session_id,
                sdk_stderr=sdk_stderr,
            ) from exc
        actor = SessionActor(
            client_factory=lambda: ClaudeSDKClient(options=options),
            on_message=self._make_actor_message_callback(managed_ref),
            on_command_lifecycle=self._make_command_lifecycle_callback(managed_ref),
        )

        resumed_status: SessionStatus = (
            meta.status if meta.status in ("idle", "running", "interrupted", "error", "closed") else "idle"
        )
        managed = ManagedSession(
            session_id=meta.id,  # Now it is the sdk_session_id / 现在就是 sdk_session_id
            actor=actor,
            status=resumed_status,
            project_name=meta.project_name,
            agent_pricing=agent_pricing_of(options),
            # The CLI of a resumed session carries the earlier cumulative usage; the diff starts from the first result
            # after resuming
            # 恢复会话的 CLI 带着此前的累计用量，差分以恢复后第一条结果为起点
            usage_meter=TurnUsageMeter(resumed=resumable),
            resolved_sdk_id=meta.id,  # Mark as registered to prevent creating the DB record twice / 标记为已注册，防止重复创建 DB 记录
            channel=self._session_channel(meta.id),
        )
        managed.sdk_id_event.set()  # An existing session does not need to wait for sdk_id / 已有会话不需要等待 sdk_id
        managed.entry_pipeline = self._build_entry_pipeline(managed)
        managed_ref[0] = managed
        managed.last_activity = time.monotonic()
        self.sessions[session_id] = managed

        try:
            await actor.start()
        except Exception as exc:
            sdk_stderr = startup_stderr.render()
            startup_error = _make_agent_startup_error(
                exc,
                project_name=meta.project_name,
                session_id=session_id,
                sdk_stderr=sdk_stderr,
            )
            self.sessions.pop(session_id, None)
            self._release_channel(session_id)
            raise startup_error from exc
        finally:
            startup_stderr.stop()

        # done_callback BEFORE processor spawn (avoids race where actor
        # completes before the callback attaches and the None sentinel
        # is never pushed).
        actor.add_done_callback(self._make_actor_done_callback(managed))

        managed._process_task = asyncio.create_task(
            self._process_inbox(managed),
            name=f"inbox-{session_id}",
        )
        return managed

    async def send_message(
        self,
        session_id: str,
        content: str | list[dict[str, Any]],
        *,
        meta: SessionMeta | None = None,
        locale: str = DEFAULT_LOCALE,
        user_entry: dict[str, Any] | None = None,
        client_key: str | None = None,
        resumable: bool = True,
    ) -> dict[str, Any]:
        """Hand a user message to the session's CLI immediately; it is sent even while a turn is running, and the CLI
        queues it or merges it into the current turn.

        The message first becomes a queued message and ``{"queued_message": ...}`` is returned; only when the CLI
        starts handling it (``command_lifecycle`` started) is it written to the event log and assigned a seq (see
        ``_settle_queued_message``). ``user_entry`` is the user entry written on acceptance; if omitted it is built
        from ``content``. Retries with the same ``client_key`` are not sent to the CLI again: while still queued the
        same queued message is returned, and once in the log the authoritative entry ``{"entry": ...}`` is returned.

        ``resumable`` is passed through to ``get_or_connect``; see its docstring.

        把一条用户消息立即交给会话的 CLI，有轮次在跑时同样送入，由 CLI 排队或并入当前轮。

        消息先成为排队消息，返回 ``{"queued_message": ...}``；CLI 开始处理它（``command_lifecycle``
        的 started）时才写入事件日志、分配 seq（见 ``_settle_queued_message``）。``user_entry`` 是被
        接纳时写入的用户条目，缺省时按 ``content`` 构造。同一 ``client_key`` 的重试不再送 CLI：仍在
        排队时返回同一条排队消息，已入日志时返回权威条目 ``{"entry": ...}``。

        ``locale`` is forwarded to ``get_or_connect``; it shapes the system
        prompt only when the revival starts a fresh session (see there).
        ``resumable`` 透传给 ``get_or_connect``，见其文档。
        """
        exited = self.sessions.get(session_id)
        if client_key is not None and exited is not None and exited.actor_exited:
            # The CLI has exited: retries already accepted (such as "unsent" messages) are returned as they are,
            # without reviving the session for the duplicate check
            # CLI 已退出：已受理过的重试（如「未发送」消息）原样返回，不为查重复活会话
            async with exited.send_lock:
                accepted = await self._find_sent_message(exited, session_id, client_key)
            if accepted is not None:
                return accepted

        managed = await self.get_or_connect(session_id, meta=meta, locale=locale, resumable=resumable)
        managed.last_activity = time.monotonic()

        # Duplicate check, registration, delivery and failure rollback share one critical section; a retry only claims
        # messages whose delivery has completed.
        # 查重、登记、投递与失败回滚共用一个临界区；重试只认领已完成投递的消息。
        async with managed.send_lock:
            if client_key is not None:
                accepted = await self._find_sent_message(managed, session_id, client_key)
                if accepted is not None:
                    return accepted

            if user_entry is None:
                user_entry = build_user_entry(
                    [{"type": "text", "text": content}] if isinstance(content, str) else content
                )
            queued = QueuedMessage(entry=user_entry, cli_uuid=str(uuid4()), content=content, client_key=client_key)
            managed.queued_messages.append(queued)
            if client_key is not None:
                managed.sent_client_keys[client_key] = queued.id
            # Delivery failure is an acceptance failure: take down the queued message and release the idempotency key,
            # so a same-key retry sends it again
            # 投递失败即受理失败：撤下排队消息并释放幂等键，同键重试会重新送入
            await self._deliver_queued_message(managed, queued, on_failure=managed.discard_queued_message)
            return {"queued_message": queued.to_payload()}

    async def _deliver_queued_message(
        self,
        managed: ManagedSession,
        queued: QueuedMessage,
        *,
        on_failure: Callable[[QueuedMessage], None],
        priority: Literal["now"] | None = None,
    ) -> None:
        """Hand a message already registered in the queued list to the CLI; on delivery failure, first call
        ``on_failure`` to undo the registration, then raise. ``priority="now"`` makes the CLI interrupt the current
        turn and handle it first (send immediately).

        The caller holds ``managed.send_lock``.

        把已登记在排队列表里的消息交给 CLI；投递失败时先调 ``on_failure`` 撤回登记，再抛出。
        ``priority="now"`` 让 CLI 打断当前轮先处理它（立即发送）。

        调用方持有 ``managed.send_lock``。
        """
        session_id = managed.session_id
        cli_uuid = queued.cli_uuid
        managed.sent_message_entries[cli_uuid] = queued.id
        # Broadcast first, then send: the removal broadcast after the CLI reports started must not come before the add
        # broadcast
        # 先广播再送入：CLI 报告 started 后移出的广播不能早于加入的广播
        managed.channel.broadcast({"type": "queued_message", "op": "upsert", "message": queued.to_payload()})
        # Registering means entering running: idle cleanup and eviction must not disconnect the session before the
        # send
        # 登记即进入 running：闲置清理与驱逐不在送入之前断开会话
        epoch = managed._running_epoch
        turn_in_progress = not managed.enter_running()

        delivering = False
        try:
            await self.meta_store.update_status(session_id, "running")
            live = self.sessions.get(session_id)
            if live is not managed:
                # The session was fully evicted while persisting: this running lands after the terminal state written
                # by eviction, so rewrite it from the current truth -- if a new session with the same id exists, write
                # that session's status, otherwise write back the eviction's terminal state; then close as a failure
                # 落库期间会话已被驱逐完：这次 running 落在驱逐写的终态之后，按当前真相补写——同一 id 已有
                # 新会话就写新会话的状态，否则写回驱逐的终态；之后按失败收尾
                await self.meta_store.update_status(session_id, managed.status if live is None else live.status)
                raise RuntimeError("session evicted while the message was being delivered")
            delivering = True
            await managed.send_query(
                _user_message_frame(queued.content, cli_uuid, priority=priority), sdk_session_id=session_id
            )
        except Exception as exc:
            logger.error("会话消息处理失败: %s", redact_diagnostic_text(exc))
            on_failure(queued)
            if turn_in_progress:
                # If the meta write fails, the message was not sent to the CLI and the turn in progress is settled as
                # usual by the CLI's idle; a failure writing to the CLI means the pipe is broken and the actor exits
                # with it, so send_query and the actor exit path record error
                # meta 写入失败时消息没送入 CLI，进行中的轮次照常由 CLI 的 idle 结算；
                # 写入 CLI 失败说明管道已断，actor 随之退出，由 send_query 与 actor 退出路径落 error
                if not delivering:
                    managed.abandon_running(epoch)
                raise
            if session_id in self._disconnecting or self.sessions.get(session_id) is not managed:
                # The session is being evicted, or was fully evicted while waiting for the meta DB write: the terminal
                # state is written by the eviction close-out, so do not race to write error here
                # 会话正在被驱逐，或等 meta 写库期间已被驱逐完：终态由驱逐收尾写入，这里不抢写 error
                raise
            managed.status = "error"
            try:
                await self.meta_store.update_status(session_id, "error")
            except Exception:
                logger.exception("持久化 error 状态失败 session_id=%s", session_id)
            # Subscribers may already have switched to running along with the queued message, and no turn will report
            # idle again, so the terminal state must be broadcast explicitly
            # 订阅者可能已随排队消息切到 running，没有轮次会再报 idle，终态须显式广播
            managed.channel.broadcast({"type": "runtime_status", "status": "error", "reason": "send failed"})
            raise

    async def _find_sent_message(
        self, managed: ManagedSession, session_id: str, client_key: str
    ) -> dict[str, Any] | None:
        """Find an already accepted message by idempotency key: return the queued message while still queued, or the
        authoritative entry once in the log; return None if never accepted.

        If it was accepted but the log write failed, raise ``UnrecordedMessageError`` rather than sending the same
        message again.

        The caller holds send_lock until delivery or failure rollback completes, so the queued table only contains
        messages whose delivery has completed earlier.

        按幂等键找已受理的消息：仍在排队时返回排队消息，已入日志时返回权威条目；未受理过返回 None。

        已被接纳但日志写入失败时抛 ``UnrecordedMessageError``，不把同一消息再送一次。

        调用方持有 send_lock 直到投递或失败回滚完成，排队表只包含此前已完成投递的消息。
        """
        entry_uuid = managed.sent_client_keys.get(client_key)
        if entry_uuid is not None:
            queued = managed.find_queued_message(message_id=entry_uuid)
            if queued is not None:
                return {"queued_message": queued.to_payload()}
        entry = await self.event_log_store.find_by_client_key(session_id, client_key)
        if entry is not None:
            return {"entry": entry}
        if entry_uuid is not None:
            # The idempotency key is only released when the message leaves the queue without being accepted: a key
            # that still exists but is not in the log means the log write failed after acceptance
            # 幂等键只在消息未被接纳就离开排队时释放：键还在却不在日志里，是接纳后日志写入失败
            raise UnrecordedMessageError(f"message {entry_uuid} was accepted but not recorded")
        return None

    async def _settle_queued_message(
        self, managed: ManagedSession, lifecycle: CommandLifecycle, *, preempted: bool = False
    ) -> None:
        """The CLI reports what happened to a queued message: started means accepted; write it to the event log, then
        remove it from the queued list.

        Runs in inbox order, so the entry comes after output produced earlier; broadcast the entry first, then the
        removal, so by the time the client sees it disappear from the tray it is already on the timeline. Messages
        already written to the log are not in the queued list, so a later cancelled (the first message of an
        interrupted turn also receives one) is no longer handled.

        CLI 报告排队消息的去向：started 即被接纳，写入事件日志后移出排队列表。

        在 inbox 序上执行，条目排在此前已产生的输出之后；先广播条目、再广播移出，
        客户端看到它从托盘消失时，时间线上已经有它。已写入日志的消息不在排队列表里，
        之后的 cancelled（被打断那一轮的首条消息也会收到）不再处理。
        """
        queued = managed.find_queued_message(cli_uuid=lifecycle.command_uuid)
        if queued is None:
            return
        if lifecycle.state == "started":
            # Usage is billed per turn: when sent it may still be queued behind the turn in progress, and it only
            # counts toward this turn's prompt once accepted
            # 用量按轮次记账：送入时它可能还排在进行中的轮次之后，被接纳时才计入本轮的 prompt
            text = _content_text(queued.entry.get("content", []))
            managed.last_user_prompt = text if managed.turn_usage_recorded else f"{managed.last_user_prompt}\n{text}"
            managed.turn_usage_recorded = False
            if managed.entry_pipeline is not None:
                await managed.entry_pipeline.append_user_entry(queued.entry, client_key=queued.client_key)
            managed.remove_queued_message(queued)
            return
        if queued.withdrawal == "send_now":
            # The CLI did not handle it either before or after the withdraw reply of a send-immediately: send it again
            # with now priority
            # 立即发送的撤回答复之前或之后，CLI 都没处理它：以 now 优先级重新送入
            await self._redeliver_in_inbox(managed, queued, lifecycle.command_uuid, priority="now")
            return
        if queued.withdrawal is not None:
            # After the withdraw reply failed (the CLI already took it from the queue), the CLI still did not handle
            # it: close it according to the user's original intent
            # 撤回答复失败（CLI 已从队列取走它）之后 CLI 仍没处理它：按用户当初的意图收尾
            managed.withdraw_queued_message(queued)
            return
        if preempted and lifecycle.state == "cancelled":
            # The turn interrupted by a send-immediately dropped it and it has not entered the conversation yet: send
            # it again, after the jumping-the-queue message
            # 被立即发送打断的那一轮丢掉了它，它还没进入对话：排到插队消息之后重新送入
            await self._redeliver_in_inbox(managed, queued, lifecycle.command_uuid)
            return
        logger.warning(
            "排队消息未被 CLI 处理 session_id=%s state=%s message_id=%s",
            managed.session_id,
            lifecycle.state,
            queued.id,
        )
        managed.discard_queued_message(queued)

    async def withdraw_queued_message(
        self, session_id: str, message_id: str, intent: WithdrawalIntent
    ) -> tuple[WithdrawalOutcome, QueuedMessage | None]:
        """Withdraw a queued message on the user's edit / delete: withdraw it from the CLI through the actor, and
        remove it from the queue only if the withdrawal succeeds.

        Returns ``("withdrawn", message)``: withdrawn from the CLI queue and removed from the queue, with the
        idempotency key released. Returns ``("accepted", None)``: the CLI already took this message and it enters
        the conversation as usual; if the CLI still reports cancelled later, it is closed according to the recorded
        ``intent`` (see ``_settle_queued_message``). Raises ``QueuedMessageNotFoundError`` when it is not a message
        this session queued; raises ``QueuedMessageWithdrawalPendingError`` when a withdrawal of the same message is
        still waiting for the CLI's reply.

        按用户的编辑 / 删除撤回一条排队消息：经 actor 向 CLI 撤回，撤回成功才移出排队。

        返回 ``("withdrawn", 消息)``：已从 CLI 队列撤回并移出排队，同时释放幂等键。返回
        ``("accepted", None)``：CLI 已取走这条消息，它照常进入对话；之后 CLI 若仍报 cancelled，
        按记下的 ``intent`` 收尾（见 ``_settle_queued_message``）。不是本会话排队过的消息时抛
        ``QueuedMessageNotFoundError``；同一条消息的撤回仍在等待 CLI 答复时抛
        ``QueuedMessageWithdrawalPendingError``。
        """
        located = self._locate_queued_message(session_id, message_id)
        if located is None:
            return "accepted", None
        managed, queued = located
        if queued.state == "unsent":
            # The CLI is gone, so there is nothing to withdraw from: remove it directly according to the intent / CLI 已不在，没有可撤回的对象：直接按意图移出
            queued.withdrawal = intent
            managed.withdraw_queued_message(queued)
            return "withdrawn", queued
        if await self._cancel_in_cli(managed, queued, intent):
            managed.withdraw_queued_message(queued)
        if queued.withdrawn:
            return "withdrawn", queued
        return "accepted", None

    async def send_queued_message_now(self, session_id: str, message_id: str) -> SendNowOutcome:
        """Send a queued message immediately: first withdraw it from the CLI; if the withdrawal succeeds, send it again
        with a new uuid and ``now`` priority.

        The CLI interrupts the current turn and handles it first; it is still the same queued message (id and
        idempotency key unchanged) and is written to the log as usual when accepted. Returns ``"sent"``: sent again.
        Returns ``"accepted"``: the CLI already took this message, it is not resent and enters the conversation as
        usual; if the CLI still reports cancelled later, it is sent again with ``now`` priority. Exceptions are the
        same as ``withdraw_queued_message``.

        立即发送一条排队消息：先向 CLI 撤回，撤回成功以新的 uuid、``now`` 优先级重新送入。

        CLI 打断当前轮先处理它；它仍是同一条排队消息（id、幂等键不变），被接纳时照常写入日志。
        返回 ``"sent"``：已重新送入。返回 ``"accepted"``：CLI 已取走这条消息，不再重发，它照常
        进入对话；之后 CLI 若仍报 cancelled，以 ``now`` 优先级重新送入。异常同 ``withdraw_queued_message``。
        """
        located = self._locate_queued_message(session_id, message_id)
        if located is None:
            return "accepted"
        managed, queued = located
        if queued.state == "unsent":
            # The CLI has exited and there is no turn to interrupt: resend it as an "unsent" message / CLI 已退出、没有轮次可打断：按「未发送」消息重新发送
            await self.resend_queued_message(session_id, message_id)
            return "sent"
        cancelled_uuid = queued.cli_uuid
        if await self._cancel_in_cli(managed, queued, "send_now"):
            await self._redeliver_after_cancel(managed, queued, cancelled_uuid, priority="now")
        # When the CLI's cancelled frame arrives before the withdraw reply, the inbox has already sent it again and
        # replaced the uuid
        # CLI 的 cancelled 帧先于撤回答复到达时，inbox 已经重新送入、换掉了 uuid
        return "sent" if queued.cli_uuid != cancelled_uuid else "accepted"

    def _locate_queued_message(self, session_id: str, message_id: str) -> tuple[ManagedSession, QueuedMessage] | None:
        """Find the queued message the user wants to withdraw or send immediately; return None if it has already left
        the queue and entered the conversation.

        找到用户要撤回或立即发送的排队消息；已离开排队、进入对话时返回 None。"""
        managed = self.sessions.get(session_id)
        queued = managed.find_queued_message(message_id=message_id) if managed is not None else None
        if managed is None or queued is None:
            if managed is not None and managed.was_sent(message_id):
                # Already left the queue: the action was clicked before the tray removal arrived, and the message has
                # entered the conversation
                # 已离开排队：在托盘移除送达之前点的操作，消息已进入对话
                return None
            raise QueuedMessageNotFoundError(f"queued message {message_id} not found in session {session_id}")
        if queued.withdrawing:
            raise QueuedMessageWithdrawalPendingError(f"queued message {message_id} is being withdrawn")
        return managed, queued

    @staticmethod
    async def _cancel_in_cli(
        managed: ManagedSession, queued: QueuedMessage, intent: WithdrawalIntent | Literal["send_now"]
    ) -> bool:
        """Record the withdraw intent and withdraw it from the CLI through the actor; return whether the CLI removed it
        from its queue.

        If the CLI already answered an earlier withdrawal with failure, do not ask the CLI again; just replace it
        with the latest intent and return False.

        记下撤回意图并经 actor 向 CLI 撤回，返回 CLI 是否把它移出了队列。

        此前的撤回已被 CLI 答复失败时不再问 CLI，只换成最新的意图并返回 False。
        """
        previous_intent = queued.withdrawal
        queued.withdrawal = intent
        if previous_intent is not None:
            return False

        queued.withdrawing = True
        try:
            # Enqueue inside the send lock: the withdrawal comes after delivery of all earlier messages, so the CLI
            # already has this message when it receives the withdrawal
            # 在发送锁内入队：撤回排在此前所有消息的投递之后，CLI 收到撤回时已经有这条消息
            async with managed.send_lock:
                cmd = SessionCommand(type="cancel", message_uuid=queued.cli_uuid)
                await managed.actor.enqueue(cmd)
            await cmd.done.wait()
        finally:
            queued.withdrawing = False
        if cmd.error is not None:
            queued.withdrawal = None
            raise cmd.error
        return bool(cmd.cancelled)

    async def _redeliver_in_inbox(
        self,
        managed: ManagedSession,
        queued: QueuedMessage,
        cancelled_uuid: str,
        *,
        priority: Literal["now"] | None = None,
    ) -> None:
        """Resend on the inbox: if the send fails the message has already become "unsent", and the inbox must not exit
        because of it.

        inbox 上的重新送入：送入失败时消息已转为「未发送」，不让 inbox 随之退出。"""
        with contextlib.suppress(Exception):
            await self._redeliver_after_cancel(managed, queued, cancelled_uuid, priority=priority)

    async def _redeliver_after_cancel(
        self,
        managed: ManagedSession,
        queued: QueuedMessage,
        cancelled_uuid: str,
        *,
        priority: Literal["now"] | None = None,
    ) -> None:
        """The CLI removed the queued message from its queue and it has not entered the conversation yet: send it again
        unchanged with a new uuid; it is still the same queued message.

        ``cancelled_uuid`` is the uuid carried by the send that was removed. Both the withdraw reply and the CLI's
        cancelled frame trigger a resend; whichever arrives first replaces the uuid synchronously, and the other
        then does nothing. If the send fails, the message becomes "unsent".

        CLI 把排队消息移出了队列、它还没进入对话：换一个 uuid 原样再送入，仍是同一条排队消息。

        ``cancelled_uuid`` 是被移出的那次送入所带的 uuid。撤回答复与 CLI 的 cancelled 帧都会触发
        重送，先到的一方同步换掉 uuid，另一方随之什么也不做。送入失败时消息转为「未发送」。
        """
        if queued.cli_uuid != cancelled_uuid or queued not in managed.queued_messages:
            return
        queued.cli_uuid = str(uuid4())
        queued.withdrawal = None

        def _not_delivered(failed: QueuedMessage) -> None:
            # It is no longer in the CLI queue: make it "unsent", and let the user decide whether to resend, edit or
            # delete it
            # 它已不在 CLI 队列里：转为「未发送」，由用户决定重新发送、编辑或删除
            managed.now_messages_in_flight.discard(failed.cli_uuid)
            failed.state = "unsent"
            managed.channel.broadcast({"type": "queued_message", "op": "upsert", "message": failed.to_payload()})

        # Shares the send lock with sending: later withdrawals come after this send / 与发送共用发送锁：之后的撤回排在这次送入之后
        async with managed.send_lock:
            if queued not in managed.queued_messages:
                return
            # Register only once the send is certain: it may have left the queue while waiting for the lock, and a
            # leftover registration would make every later aborted_* turn count as interrupted by a queue jump
            # 确定送入才登记：等锁期间它可能已离开排队，登记残留会让之后的 aborted_* 轮次都被当成插队打断
            if priority == "now":
                managed.now_messages_in_flight.add(queued.cli_uuid)
                # The CLI handles it first: the tray also moves it to the front / CLI 先处理它：托盘也把它排到最前
                managed.move_queued_message_first(queued)
            await self._deliver_queued_message(managed, queued, on_failure=_not_delivered, priority=priority)

    async def resend_queued_message(
        self,
        session_id: str,
        message_id: str,
        *,
        meta: SessionMeta | None = None,
        locale: str = DEFAULT_LOCALE,
    ) -> QueuedMessage:
        """Hand an "unsent" message to the CLI again: it is still the same queued message (``id`` and idempotency key
        unchanged), with a new ``cli_uuid``.

        If the CLI has exited, first disconnect the old connection and revive the session; unsent messages move to
        the new connection and the tray stays as it is. If the message has already been handed to the CLI again
        (state ``queued``), it is not sent again and is returned as it is. On delivery failure the message goes back
        to "unsent". Raises ``QueuedMessageNotFoundError`` when it is not a queued message of this session.

        把一条「未发送」消息重新交给 CLI：仍是同一条排队消息（``id``、幂等键不变），换新的 ``cli_uuid``。

        CLI 已退出时先断开旧连接、复活会话，未发送的消息随之转入新连接，托盘保持原样。消息已重新
        交给 CLI（状态为 ``queued``）时不再送一次，原样返回。投递失败时消息回到「未发送」。不是本会话
        的排队消息时抛 ``QueuedMessageNotFoundError``。
        """
        managed = self.sessions.get(session_id)
        queued = managed.find_queued_message(message_id=message_id) if managed is not None else None
        if managed is None or queued is None:
            raise QueuedMessageNotFoundError(f"queued message {message_id} not found in session {session_id}")
        if queued.state != "unsent":
            return queued
        if managed.actor_exited:
            # Revive the session; unsent messages move to the new connection with it (see get_or_connect)
            # 复活会话，未发送的消息随之转入新连接（见 get_or_connect）
            managed = await self.get_or_connect(session_id, meta=meta, locale=locale)
            if managed.find_queued_message(message_id=message_id) is not queued or queued.state != "unsent":
                raise QueuedMessageNotFoundError(f"queued message {message_id} not found in session {session_id}")

        def _back_to_unsent(failed: QueuedMessage) -> None:
            failed.state = "unsent"
            managed.channel.broadcast({"type": "queued_message", "op": "upsert", "message": failed.to_payload()})

        async with managed.send_lock:
            if queued not in managed.queued_messages:
                raise QueuedMessageNotFoundError(f"queued message {message_id} not found in session {session_id}")
            if queued.state != "unsent":
                return queued
            queued.state = "queued"
            queued.cli_uuid = str(uuid4())
            await self._deliver_queued_message(managed, queued, on_failure=_back_to_unsent)
        return queued

    async def interrupt_session(self, session_id: str) -> SessionStatus:
        """Interrupt a running session via the actor."""
        meta = await self.meta_store.get(session_id)
        if meta is None:
            raise FileNotFoundError(f"session not found: {session_id}")

        managed = self.sessions.get(session_id)
        if managed is None:
            if meta.status == "running":
                await self.meta_store.update_status(session_id, "interrupted")
                return "interrupted"
            return meta.status

        if managed.status != "running":
            return managed.status

        # A plain interrupt only stops the current turn; queued messages in the CLI queue are kept, and the next turn
        # starts as usual afterwards
        # 普通中断只停当前轮，CLI 队列里的排队消息保留，之后照常开始下一轮
        managed.interrupt_requested = True
        managed.cancel_pending_questions("session interrupted by user")

        try:
            await managed.send_interrupt()
        except Exception:
            logger.exception("发送 interrupt 命令失败 session_id=%s", session_id)
            managed.status = "error"
            return managed.status

        managed.last_activity = time.monotonic()
        # This turn's result records the interrupted outcome; after the CLI reports idle the session lands in
        # "interrupted"
        # 这一轮的 result 记下中断结局，CLI 报 idle 后会话落到 "interrupted"
        return managed.status

    def _handle_special_message(self, managed: ManagedSession, msg_dict: dict[str, Any]) -> None:
        """Handle result messages before broadcast."""
        if msg_dict.get("type") == "result":
            msg_dict["session_status"] = self._resolve_result_status(
                msg_dict,
                interrupt_requested=managed.interrupt_requested,
                preempted=bool(managed.now_messages_in_flight),
            )
            # An interrupt only applies to the first result after it and is consumed when the frame arrives: the next
            # turn started by a queued message may finish before this turn is closed, so the flag must not carry over
            # 中断只作用于它之后的第一个 result，在帧到达时就消费：排队消息开启的下一轮
            # 可能在本轮收尾之前就结束，不能沿用这个标记
            managed.interrupt_requested = False
        elif msg_dict.get("type") == "system" and msg_dict.get("subtype") == "init":
            self._check_auto_memory_path(managed, msg_dict)

    def _check_auto_memory_path(self, managed: ManagedSession, init_msg: dict[str, Any]) -> None:
        """Check that the auto memory directory reported by init matches the project memory directory redirected at
        assembly time.

        On mismatch only log an error and open the session anyway: if the redirect did not take effect, notes fall
        back to the native derived directory and creative work goes on as usual; blocking the session costs far more
        than notes in the wrong place. The fence does not follow the reported path either -- the allowed range is
        compiled from the expected path, and following the actual value would let the subprocess's own claim widen
        the writable range.

        ``memory_paths`` is an @internal field of the CLI init message; when missing (old CLI, auto memory turned
        off) no judgment is made, and only an explicitly reported different directory counts as a mismatch.

        核对 init 上报的 auto memory 目录与装配时重定向的项目记忆目录是否一致。

        不符只记 error、会话照开：重定向没生效的后果是笔记落回原生派生目录，
        创作照常进行，把会话拦下来的代价远大于记错地方。围栏也不追随上报路径——
        放行范围按预期路径编译，跟着实际值走等于让子进程的自述扩大可写范围。

        ``memory_paths`` 是 CLI init 消息的 @internal 字段，缺失（旧 CLI、
        auto memory 被关）时不判定，只有明确上报了一个不同的目录才算不符。
        """
        raw = init_msg.get("data")
        data = raw if isinstance(raw, dict) else init_msg
        memory_paths = data.get("memory_paths")
        if not isinstance(memory_paths, dict):
            return
        reported = memory_paths.get("auto")
        if not reported:
            return
        try:
            expected = project_memory_dir(self._resolve_project_cwd(managed.project_name))
        except (ValueError, FileNotFoundError):
            return
        if Path(str(reported)).resolve(strict=False) == expected.resolve(strict=False):
            return
        logger.error(
            "auto memory 目录与装配预期不符，项目记忆笔记将落到别处",
            extra={
                "session_id": managed.session_id,
                "project_name": managed.project_name,
                "expected_auto_memory_dir": str(expected),
                "reported_auto_memory_dir": str(reported),
            },
        )

    def set_autonomous_turn_listener(self, listener: Callable[[str, str], None] | None) -> None:
        """Register the notification for when a session returns to running because the CLI started a new turn on its
        own (args: project name, session id).

        注册会话因 CLI 自主开启新一轮而回到 running 时的通知（参数：项目名、会话 id）。"""
        self._autonomous_turn_listener = listener

    async def _persist_cli_resumed(self, managed: ManagedSession) -> None:
        """The session returned to running without a send (the CLI reported it started working or started a new turn):
        persist and notify.

        The typical source is a turn the CLI starts on its own after a background task completes. An open session
        panel receives this turn through its resident entry stream; the notification lets other views such as the
        session list know the session is back to running.

        会话未经发送回到 running（CLI 报告开始工作或开启了新一轮）：持久化并通知。

        典型来源是后台任务完成后 CLI 自主开启的一轮。打开着的会话面板经常驻的 entry 流
        收到这一轮；通知供会话列表等其他视图得知会话回到 running。
        """
        try:
            await self.meta_store.update_status(managed.session_id, "running")
        except Exception:
            # Runs inside the inbox: an exception would make the inbox exit while the actor stays alive, and nobody
            # would ever close the session. The in-memory state has already switched; persistence is made up by the
            # terminal state written when leaving running.
            # 运行在 inbox 里：异常会让 inbox 退出、actor 却还活着，会话再也没人收尾。
            # 内存状态已切换，持久化由离开 running 时写入终态补上。
            logger.exception("持久化自主轮次 running 状态失败 session_id=%s", managed.session_id)
        # The resident entry stream pushes running immediately based on this: the panel does not stay on the old
        # terminal state before this turn's visible output
        # 常驻的 entry 流据此立即推 running：这一轮可见输出之前，面板不停在旧终态
        managed.channel.broadcast({"type": "runtime_status", "status": "running", "reason": "cli resumed"})
        listener = self._autonomous_turn_listener
        if listener is None:
            return
        try:
            listener(managed.project_name, managed.session_id)
        except Exception:
            logger.exception("自主轮次通知失败 session_id=%s", managed.session_id)

    async def _finalize_turn(self, managed: ManagedSession, result_msg: dict[str, Any]) -> None:
        """Close a turn on its result: record this turn's outcome and usage, without switching the session status.

        A result only means one turn ended, and the CLI may start the next turn right away; the session leaves
        running when the CLI reports idle (see _settle_cli_idle).

        一轮的 result 收尾：记下这一轮的结局与用量，不切换会话状态。

        result 只代表一轮结束，CLI 可能紧接着开启下一轮；会话离开 running 以 CLI 报 idle 为准
        （见 _settle_cli_idle）。
        """
        managed.cancel_pending_questions("session completed")
        explicit = str(result_msg.get("session_status") or "").strip()
        outcome: SessionStatus = (
            explicit
            if explicit in ("completed", "error", "interrupted")
            else self._resolve_result_status(
                result_msg,
                interrupt_requested=managed.interrupt_requested,
            )
        )
        managed.turn_outcome = outcome
        managed.last_turn_result = result_msg
        managed.last_activity = time.monotonic()
        if outcome == "error":
            logger.warning(
                "assistant session result error",
                extra={
                    "session_id": managed.session_id,
                    "subtype": result_msg.get("subtype"),
                    "is_error": result_msg.get("is_error"),
                    "api_error_status": result_msg.get("api_error_status"),  # SDK 0.1.76+
                    "stop_reason": result_msg.get("stop_reason"),
                },
            )
        try:
            await self._record_assistant_usage(managed, result_msg, outcome)
        except Exception:
            logger.exception("记录 assistant usage 失败 session_id=%s", managed.session_id)
        managed.turn_usage_recorded = True
        managed.interrupt_requested = False

    async def _settle_cli_idle(self, managed: ManagedSession, epoch: int) -> None:
        """The CLI reports idle: the session leaves running, with the status taken from the latest turn's outcome.

        Runs in inbox order, so this turn's entries are all processed. If another message was delivered, or the CLI
        reported it started working again, after the idle was read, this idle is stale and the session stays running
        until the next idle.

        CLI 报 idle：会话离开 running，状态取最近一轮的结局。

        在 inbox 序上执行，本轮条目都已处理完。读到 idle 之后又有消息送达、或 CLI 又报告
        开始工作时，这个 idle 已过时，会话保持 running 等下一个 idle。
        """
        if managed.status != "running" or epoch != managed._running_epoch:
            return
        status: SessionStatus = managed.turn_outcome or "completed"
        managed.status = status
        managed.last_activity = time.monotonic()
        # An interrupt arriving after the result has no turn to close; keeping it for the next turn would record that
        # turn's failure as an interruption
        # result 之后才到的中断没有轮次可收尾，留到下一轮会把它的失败记成中断
        managed.interrupt_requested = False
        await self.meta_store.update_status(managed.session_id, status)
        if epoch != managed._running_epoch:
            # The session went back to running while persisting (the CLI started working or a new message arrived):
            # the running written by the side that switched back may have been persisted before this terminal state,
            # so write it once more to make running the last one persisted; a stale terminal state is not broadcast,
            # and no cleanup is scheduled
            # 落库期间会话又回到 running（CLI 开始工作或新消息送达）：切回一方写入的 running
            # 可能先于这次终态落库，补写一次让 running 最后落库；过时的终态不广播，也不调度清理
            if managed.status == "running":
                await self.meta_store.update_status(managed.session_id, "running")
            return
        result = managed.last_turn_result or {}
        managed.channel.broadcast(
            {
                "type": "runtime_status",
                "status": status,
                "reason": "cli idle",
                **{key: result[key] for key in _RESULT_STATUS_FIELDS if key in result},
            }
        )
        self._schedule_cleanup(managed.session_id)

    async def _record_assistant_usage(
        self,
        managed: ManagedSession,
        result_msg: dict[str, Any],
        final_status: SessionStatus,
    ) -> None:
        """Usage of one turn: each model with usage writes one provider call record, priced by the Agent credential
        injected when the session connected and its multiplier and binding at that time.

        一轮的用量：每个有用量的模型写一条供应商调用记录，按会话连接时注入的 Agent 凭证及其当时的倍率与绑定计价。"""
        usages = managed.usage_meter.close_turn(result_msg.get("model_usage"))
        if not usages:
            return

        # A turn interrupted by the user is recorded as cancelled rather than failed, so it does not pollute the
        # failure rate; cost is still counted -- requests completed before the interrupt are real spending, unlike the
        # zero-cost cancel in the billing bracket where "the result never arrived and the cost is unknown".
        # 用户中断的一轮记 cancelled 而非 failed，不污染失败率；费用照常计入——
        # 中断前已完成的请求是真实花费，与记账括号里「结果未到、费用未知」的零费用取消不同。
        if final_status == "completed":
            status = CallStatus.SUCCESS
        elif final_status == "interrupted":
            status = CallStatus.CANCELLED
        else:
            status = CallStatus.FAILED

        pricing = managed.agent_pricing
        credential_id = pricing.credential_id
        await self.ledger.record_agent_turn(
            project_name=managed.project_name,
            session_id=managed.session_id,
            prompt=managed.last_user_prompt,
            user_id=getattr(self, "_user_id", DEFAULT_USER_ID),
            provider=agent_provider_id(credential_id) if credential_id is not None else PROVIDER_ANTHROPIC,
            pricing=pricing,
            status=status,
            calls=[_agent_model_call(usage) for usage in usages],
        )

    async def _mark_session_terminal(self, managed: ManagedSession, status: SessionStatus, reason: str) -> None:
        """Set terminal status on abnormal consumer exit."""
        # The CLI-exit path has already turned queued messages into "unsent"; on other paths the CLI may still hold
        # them, but nobody will close them any more
        # CLI 退出的路径已先把排队消息转为「未发送」；其余路径 CLI 可能仍持有它们，但已无人收尾
        managed.drop_queued_messages(state="queued")
        managed.cancel_pending_questions(reason)
        managed.status = status
        managed.last_activity = time.monotonic()
        await self.meta_store.update_status(managed.session_id, status)
        managed.interrupt_requested = False

        # Add a typed interrupt entry on the event log side: on this path inbox processing has stopped, so the SDK's
        # own echo will no longer reach the log through the write point. The tail check deduplicates so only one
        # remains alongside an echo already in the log (race double write).
        # 事件日志侧补写 typed 中断条目：此路径下 inbox 处理已终止，
        # SDK 自己的回显不会再经写入点入日志。尾检去重保证与已入日志的
        # 回显（竞态双写）只留一条。
        if status == "interrupted" and managed.entry_pipeline is not None:
            try:
                await managed.entry_pipeline.append_interrupt()
            except Exception:
                logger.exception("中断条目写入事件日志失败 session_id=%s", managed.resolved_sdk_id)

        # Broadcast terminal status so SSE subscribers unblock immediately
        # instead of waiting for the heartbeat timeout.
        managed.channel.broadcast(
            {
                "type": "runtime_status",
                "status": status,
                "reason": reason,
            }
        )
        self._schedule_cleanup(managed.session_id)

    def _schedule_cleanup(self, session_id: str) -> None:
        """Schedule delayed cleanup for a non-running session."""
        managed = self.sessions.get(session_id)
        if managed is None or session_id in self._disconnecting:
            return
        if managed._cleanup_task is not None and not managed._cleanup_task.done():
            managed._cleanup_task.cancel()
        managed._cleanup_task = asyncio.create_task(self._cleanup_idle(session_id))

    async def _cleanup_idle(self, session_id: str) -> None:
        try:
            delay = await self._get_cleanup_delay()
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        managed = self.sessions.get(session_id)
        if managed is None:
            return
        if managed.status == "running":
            # Disconnecting the CLI aborts the turn in flight and kills background subagents with it. The timer
            # restarts when the CLI reports idle.
            # 断开 CLI 会中止在途轮次、连带杀掉后台子智能体。CLI 报 idle 时重新计时。
            return
        if managed.status in ("idle", "interrupted", "error", "completed"):
            # Clear our own reference first so _evict_one's cleanup-task cancel doesn't self-cancel
            managed._cleanup_task = None
            await self._evict_one(managed)

    async def close_session(self, session_id: str, *, reason: str = "session closed") -> None:
        """Public close entry — gracefully tears down the actor and removes the session."""
        managed = self.sessions.get(session_id)
        if managed is None:
            return
        managed.cancel_pending_questions(reason)
        await self._evict_one(managed)

    async def _evict_one(self, managed: ManagedSession) -> None:
        """Gracefully disconnect an actor, cancel as fallback, and remove from registry."""
        session_id = managed.session_id
        if session_id in self._disconnecting:
            return
        self._disconnecting[session_id] = asyncio.Event()
        try:
            # Cancel any pending cleanup timer first
            if managed._cleanup_task is not None and not managed._cleanup_task.done():
                managed._cleanup_task.cancel()
                with contextlib.suppress(BaseException):
                    await managed._cleanup_task

            try:
                await asyncio.wait_for(
                    managed.send_disconnect(),
                    timeout=self._session_actor_shutdown_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "actor disconnect 超时，走 cancel 兜底 session_id=%s",
                    session_id,
                )
                await managed.actor.cancel_and_wait()
                managed.status = "interrupted"
            except Exception:
                logger.exception("actor 关停异常 session_id=%s", session_id)
                managed.status = "error"

            # Drain the inbox processor
            with contextlib.suppress(Exception):
                managed._inbox.put_nowait(None)
            if managed._process_task is not None and not managed._process_task.done():
                try:
                    await asyncio.wait_for(managed._process_task, timeout=5.0)
                except TimeoutError:
                    managed._process_task.cancel()
                    with contextlib.suppress(BaseException):
                        await managed._process_task
                except BaseException:
                    logger.exception(
                        "_process_inbox 退出异常 session_id=%s",
                        session_id,
                    )

            # If the session is still marked running when closed, persist a terminal state so it does not get stuck
            # after a process restart: send_message has already written running to the DB; without this step, after
            # get_or_connect revives it the session keeps running and never receives the CLI's idle. The CLI is
            # disconnected, so queued messages will no longer be processed; the channel survives eviction, so the
            # removal must be broadcast to subscribers still present
            # 若会话关闭时仍被标记为 running，持久化为终态以防进程重启后卡死：
            # send_message 已把 DB 写成 running；缺少此步，get_or_connect 复活后
            # 会话沿用 running，等不到 CLI 的 idle。
            # CLI 已断开，排队消息不会再被处理；通道跨驱逐存活，移出要广播给仍在的订阅者
            managed.drop_queued_messages()
            if managed.resolved_sdk_id is not None:
                if managed.status == "running":
                    managed.status = managed.turn_outcome or "interrupted"
                if managed.status in ("completed", "interrupted", "error"):
                    with contextlib.suppress(BaseException):
                        await self.meta_store.update_status(managed.resolved_sdk_id, managed.status)
        finally:
            self.sessions.pop(session_id, None)
            self._release_channel(session_id)
            # Do not remove it while the connection lock is held: the holder is waiting for this eviction to complete
            # to rebuild the connection, and removing it would let a newcomer create another lock and connect
            # concurrently
            # 连接锁被持有时不摘：持有者正在等这次驱逐完成后重建连接，摘掉会让后来者另起一把锁并发连接
            lock = self._connect_locks.get(session_id)
            if lock is not None and not lock.locked():
                del self._connect_locks[session_id]
            self._disconnecting.pop(session_id).set()

    async def _get_cleanup_delay(self) -> int:
        """Return the session cleanup delay in seconds, default 300 (5 minutes).

        返回会话清理延迟秒数，默认 300（5 分钟）。"""
        try:
            async with async_session_factory() as session:
                svc = ConfigService(session)
                val = await svc.get_setting("agent_session_cleanup_delay_seconds", "300")
            return max(int(val), 10)
        except Exception:
            logger.warning("读取 cleanup delay 配置失败，使用默认值", exc_info=True)
            return 300

    async def _get_max_concurrent(self) -> int:
        """Return the maximum number of concurrent sessions, default 5.

        返回最大并发会话数，默认 5。"""
        try:
            async with async_session_factory() as session:
                svc = ConfigService(session)
                val = await svc.get_setting("agent_max_concurrent_sessions", "5")
            return max(int(val), 1)
        except Exception:
            logger.warning("读取 max_concurrent 配置失败，使用默认值", exc_info=True)
            return 5

    async def _ensure_capacity(self) -> None:
        """Ensure there is a free concurrency slot, evicting the longest-inactive non-running session if needed.

        确保有空余并发槽位，必要时淘汰最久未活跃的非 running 会话。"""
        max_concurrent = await self._get_max_concurrent()
        active = [s for s in self.sessions.values() if s.session_id not in self._disconnecting]

        if len(active) < max_concurrent:
            return

        # Evictable sessions: not running. Before the CLI reports idle (turn in flight, background subagents) a
        # session counts as in progress; better to refuse a new session than disconnect it.
        # 可淘汰的会话：不在 running。CLI 报 idle 前（在途轮次、后台子智能体）都算进行中，
        # 宁可拒绝新会话也不断开它。
        evictable = sorted(
            [s for s in active if s.status != "running"],
            key=lambda s: s.last_activity or 0,
        )

        if evictable:
            victim = evictable[0]
            logger.info(
                "并发上限，淘汰 session_id=%s (status=%s)",
                victim.session_id,
                victim.status,
            )
            try:
                await self._evict_one(victim)
            except Exception as exc:
                logger.error(
                    "淘汰会话失败，无法释放并发槽位 session_id=%s",
                    victim.session_id,
                    exc_info=True,
                )
                raise SessionCapacityError("存在未能关闭的空闲会话，当前无法释放并发槽位，请稍后重试") from exc
            return

        # All sessions are running -> refuse / 所有会话都在 running → 拒绝
        raise SessionCapacityError(f"当前有{len(active)}个正在进行的会话，已达到最大上限，请稍后重试")

    _PATROL_INTERVAL = 300  # 5 minutes / 5 分钟

    async def _patrol_once(self) -> None:
        """Single sweep: clean up all timed-out non-running sessions.

        单次巡检：清理所有超时的非 running 会话。"""
        cleanup_delay = await self._get_cleanup_delay()
        now = time.monotonic()
        for sid, managed in list(self.sessions.items()):
            if managed.status == "running" or sid in self._disconnecting:
                continue
            activity_age = now - (managed.last_activity or 0)
            if activity_age > cleanup_delay * 2:
                logger.info("巡检兜底清理会话 session_id=%s status=%s", sid, managed.status)
                try:
                    m = self.sessions.get(sid)
                    if m is not None:
                        await self._evict_one(m)
                except Exception:
                    logger.warning(
                        "巡检兜底清理失败 session_id=%s",
                        sid,
                        exc_info=True,
                    )

    async def _patrol_loop(self) -> None:
        """Background periodic sweep loop.

        后台定期巡检循环。"""
        while True:
            await asyncio.sleep(self._PATROL_INTERVAL)
            try:
                await self._patrol_once()
            except Exception:
                logger.warning("巡检循环异常", exc_info=True)

    def start_patrol(self) -> None:
        """Start the background sweep task (should be called at app startup).

        启动巡检后台任务（应在应用 startup 时调用）。"""
        self._patrol_task = asyncio.create_task(self._patrol_loop())

    @staticmethod
    def _resolve_result_status(
        result_message: dict[str, Any],
        interrupt_requested: bool = False,
        preempted: bool = False,
    ) -> SessionStatus:
        """Map SDK result subtype/is_error to runtime session status."""
        return resolve_result_status(result_message, interrupt_requested=interrupt_requested, preempted=preempted)

    async def _handle_ask_user_question(
        self,
        managed: Optional["ManagedSession"],
        tool_name: str,
        input_data: dict[str, Any],
    ) -> Any:
        """Handle AskUserQuestion tool invocation within can_use_tool callback."""
        if managed is None:
            return PermissionResultAllow(updated_input=input_data)

        raw_questions = input_data.get("questions")
        questions = raw_questions if isinstance(raw_questions, list) else []
        payload = {
            "type": "ask_user_question",
            "question_id": f"aq_{uuid4().hex}",
            "tool_name": tool_name,
            "questions": questions,
            "timestamp": utc_now_iso(),
        }
        pending = managed.add_pending_question(payload)
        managed.channel.broadcast(payload)

        try:
            answers = await pending.answer_future
        except Exception as exc:
            return PermissionResultDeny(
                message=str(exc) or "session interrupted by user",
                interrupt=True,
            )
        merged_input = dict(input_data or {})
        merged_input["answers"] = answers
        return PermissionResultAllow(updated_input=merged_input)

    async def _build_can_use_tool_callback(
        self,
        session_id: str,
        managed_ref: list[Optional["ManagedSession"]] | None = None,
    ):
        """Create per-session can_use_tool callback (default-deny).

        This is step 5 (final fallback) in the SDK permission chain:
        Hooks → Deny rules → Permission mode → Allow rules → canUseTool.
        Only reached when prior steps don't resolve the decision.

        File access control uses the PreToolUse hook (step 1) because it
        fires for ALL tool calls.  Read/Glob/Grep are resolved by allow
        rules (step 4) and never reach this callback.

        This callback handles AskUserQuestion (async user interaction) and
        denies everything else as a whitelist fallback.

        Args:
            session_id: Initial session ID (may be temp_id for new sessions).
            managed_ref: Mutable single-element list holding the ManagedSession.
                When provided, the callback resolves the session via this
                reference instead of looking up session_id in self.sessions,
                so it survives the temp_id → sdk_id key swap.
        """

        async def _can_use_tool(
            tool_name: str,
            input_data: dict[str, Any],
            _context: Any,
        ) -> Any:
            normalized_tool = str(tool_name or "").strip().lower()

            if normalized_tool == "askuserquestion":
                managed = managed_ref[0] if managed_ref else self.sessions.get(session_id)
                return await self._handle_ask_user_question(
                    managed,
                    tool_name,
                    input_data,
                )

            # Windows fallback: with the sandbox off, the Bash family is not in allowed_tools and ends up here, going
            # through AgentAccessPolicy's prefix allowlist.
            # Windows 回退：sandbox 关闭时 Bash 系列不在 allowed_tools，
            # 落到这里走 AgentAccessPolicy 的前缀白名单。
            if not self.access_policy.sandbox_enabled and tool_name == "Bash":
                cmd = str((input_data or {}).get("command") or "").strip()
                if self.access_policy.is_bash_command_whitelisted(cmd):
                    return PermissionResultAllow(updated_input=input_data)
                return PermissionResultDeny(
                    message=self.access_policy.format_bash_whitelist_deny_message(cmd),
                )
            # BashOutput / KillBash are Bash management tools and are allowed directly in fallback mode.
            # BashOutput / KillBash 是 Bash 管理类工具，回退模式直接放行。
            if not self.access_policy.sandbox_enabled and tool_name in ("BashOutput", "KillBash"):
                return PermissionResultAllow(updated_input=input_data)

            # Whitelist fallback: deny any tool that was not pre-approved
            # by allowed_tools or settings.json allow rules.
            reason = getattr(_context, "decision_reason", None)  # SDK 0.1.74+
            reason_line = f"上游决策原因: {reason}\n" if reason else ""
            hint = (
                f"未授权的工具调用: {tool_name}"
                f"({json.dumps(input_data, ensure_ascii=False)[:200]})\n"
                f"{reason_line}"
                "请检查工具名是否正确，以及 file_path / 命令是否触发了 "
                "settings.json 的 deny 规则或 PreToolUse hook（跨项目/cwd 外写/代码扩展名）。"
            )
            return PermissionResultDeny(message=hint)

        return _can_use_tool

    async def _on_sdk_session_id_received(
        self,
        managed: ManagedSession,
        message: Any,
        msg_dict: dict[str, Any],
    ) -> None:
        """Handle sdk_session_id from stream. For new sessions: create DB record + signal event."""
        sdk_id = self._extract_sdk_session_id(message, msg_dict)
        if not sdk_id:
            return
        if managed.resolved_sdk_id is not None:
            return  # Already registered

        managed.resolved_sdk_id = sdk_id

        # Only create DB record for new sessions (no existing meta)
        if not managed.sdk_id_event.is_set():
            # Session-to-project ownership is only recorded in meta_store. Once the metadata is persisted the session
            # can be listed and its entry stream opened; the channel is registered under sdk_id first, and later
            # subscriptions attach to the channel the session actually broadcasts on; sessions that have left the
            # resident set are not registered.
            # 会话与项目的归属只记在 meta_store。元数据落库即可被列出、打开 entry 流，
            # 通道先按 sdk_id 登记，此后的订阅挂在会话实际广播的通道上；已离开常驻集合的会话不登记。
            registered = managed.session_id in self.sessions
            if registered:
                self._channels[sdk_id] = managed.channel
            try:
                await self.meta_store.create(managed.project_name, sdk_id)
            except BaseException:
                if registered and self._channels.get(sdk_id) is managed.channel:
                    del self._channels[sdk_id]
                raise
            await self.meta_store.update_status(sdk_id, "running")
            # The first user message of a new session is written to the log first to get its identity (seq 0): this
            # method runs serially inside the inbox task before any assistant entry is finalized, which keeps the
            # timeline order; sdk_id_event is set only after the write completes, so send_new_session gets the
            # authoritative entry as soon as it wakes up.
            # 新会话首条用户消息先写日志分配身份（seq 0）：本方法在 inbox 任务
            # 内串行执行于任何 assistant 条目定型之前，保证时间线顺序；写入
            # 完成后才 set sdk_id_event，send_new_session 醒来即可拿到权威条目。
            pending_user = managed.pending_initial_user_entry
            if pending_user is not None:
                managed.pending_initial_user_entry = None
                try:
                    authoritative, _created = await self.event_log_store.append_user_entry(
                        sdk_id,
                        pending_user["entry"],
                        client_key=pending_user.get("client_key"),
                    )
                    managed.initial_user_log_entry = authoritative
                    managed.channel.broadcast({"type": "log_entry", "session_id": sdk_id, "entry": authoritative})
                except Exception as exc:
                    # Do not swallow silently: record the exception so send_new_session reports the failure explicitly
                    # when it wakes up (clean up the session + write status back as error + raise to the caller).
                    # 不静默吞掉：记录异常供 send_new_session 醒来后显式回报
                    # 失败（清理会话 + 状态回写 error + 向调用方抛出）。
                    managed.initial_user_entry_error = exc
                    logger.exception("新会话用户条目写入事件日志失败 session_id=%s", sdk_id)
            # Key swap: replace temp_id with real sdk_id in sessions dict
            # BEFORE signaling the event. This prevents _finalize_turn from
            # using the stale temp_id if it runs before send_new_session
            # completes its own key swap.
            old_id = managed.session_id
            if old_id != sdk_id and old_id in self.sessions:
                del self.sessions[old_id]
                managed.session_id = sdk_id
                self.sessions[sdk_id] = managed
            managed.sdk_id_event.set()

    @staticmethod
    def _extract_sdk_session_id(message: Any, msg_dict: dict[str, Any]) -> str | None:
        """Extract SDK session id from either serialized payload or raw object.

        The CLI's system messages (init / status / api_retry) put ``session_id`` in ``data`` rather than at the top
        level. When the first API request fails (an invalid credential triggers CLI backoff retries), the stream
        only has messages like these, so the id must be parsable from init, otherwise a new session can only wait
        for the timeout.

        Extract SDK session id from either serialized payload or raw object.

        CLI 的 system 消息（init / status / api_retry）把 ``session_id`` 放在
        ``data`` 里而非顶层。首轮 API 请求失败（凭证无效触发 CLI 退避重试）时
        流里只有这类消息，id 必须能从 init 解析出来，否则新会话只能等超时。
        """
        sdk_id = msg_dict.get("session_id") or msg_dict.get("sessionId")
        if sdk_id:
            return str(sdk_id)
        data = msg_dict.get("data")
        if isinstance(data, dict):
            nested = data.get("session_id") or data.get("sessionId")
            if nested:
                return str(nested)
        raw_sdk_id = getattr(message, "session_id", None) or getattr(message, "sessionId", None)
        if raw_sdk_id:
            return str(raw_sdk_id)
        return None

    def get_draft_state(self, session_id: str) -> dict[str, Any]:
        """First-frame snapshot of the streaming preview state (draft) on reconnect: current accumulated state + rev
        filter threshold.

        ``rev`` is returned even when there is no active draft: old deltas queued between subscription and snapshot
        (rev <= threshold) are dropped by the client by rev, without content comparison.

        流式预览态（draft）重连首帧快照：当前累积态 + rev 过滤门槛。

        ``rev`` 在无活跃 draft 时也返回：订阅与快照之间已入队的旧 delta
        （rev <= 门槛）由客户端按 rev 丢弃，不做内容比对。
        """
        managed = self.sessions.get(session_id)
        if managed is None or managed.entry_pipeline is None:
            return {"draft": None, "rev": 0}
        draft = managed.entry_pipeline.draft
        return {"draft": draft.snapshot(), "rev": draft.rev}

    def find_queued_message_by_client_key(self, session_id: str, client_key: str) -> dict[str, Any] | None:
        """Return ``{"queued_message": ...}`` when the message for the idempotency key is still queued, otherwise None.

        幂等键对应的消息仍在排队时返回 ``{"queued_message": ...}``，否则 None。"""
        managed = self.sessions.get(session_id)
        if managed is None:
            return None
        entry_uuid = managed.sent_client_keys.get(client_key)
        queued = managed.find_queued_message(message_id=entry_uuid) if entry_uuid is not None else None
        return {"queued_message": queued.to_payload()} if queued is not None else None

    def get_queued_messages_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        """Snapshot of queued messages (in send order), sent when the entry stream opens; empty when the session is not
        resident.

        排队消息快照（按发送顺序），entry 流开场下发；会话不常驻时为空。"""
        managed = self.sessions.get(session_id)
        if managed is None:
            return []
        return [queued.to_payload() for queued in managed.queued_messages]

    async def get_pending_questions_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        """Get unresolved AskUserQuestion payloads for reconnect."""
        managed = self.sessions.get(session_id)
        if not managed:
            return []
        return managed.get_pending_question_payloads()

    async def answer_user_question(
        self,
        session_id: str,
        question_id: str,
        answers: dict[str, str],
    ) -> None:
        """Resolve AskUserQuestion answers for a running session."""
        managed = self.sessions.get(session_id)
        if managed is None:
            raise ValueError("会话未运行或无待回答问题")
        # A question arrives through the SDK's control request and may come before the actor reads the frame where the
        # CLI reports running
        # 提问经 SDK 的控制请求到达，可能先于 actor 读到 CLI 报告 running 的那一帧
        if managed.status != "running" and question_id not in managed.pending_questions:
            raise ValueError("会话未运行或无待回答问题")
        if not managed.resolve_pending_question(question_id, answers):
            raise ValueError("未找到待回答的问题")

    def _session_channel(self, session_id: str) -> SseChannel:
        """Get the session's broadcast channel, registering a new one if there is none.

        取会话的广播通道，没有就登记一个新的。"""
        channel = self._channels.get(session_id)
        if channel is None:
            channel = _make_session_channel()
            self._channels[session_id] = channel
        return channel

    def _release_channel(self, session_id: str) -> None:
        """Called after the session leaves the resident set or a subscriber leaves: the channel is removed only when
        both are gone.

        会话离开常驻集合或订阅者离开后调用：两者都没有了才摘除通道。"""
        channel = self._channels.get(session_id)
        if channel is None or channel.has_subscribers or session_id in self.sessions:
            return
        del self._channels[session_id]

    def _subscribe(self, session_id: str) -> tuple[SseChannel, asyncio.Queue]:
        """Register a live-message queue for a session.

        Does not revive cold sessions: an open panel should not take a CLI concurrency slot. The subscription
        attaches to the session's channel, and when the session is later revived by a send it reuses the same
        channel, so subscribers keep receiving broadcasts.

        Register a live-message queue for a session.

        不复活冷会话：面板开着不该占用 CLI 并发名额。订阅挂在会话的通道上，
        会话之后由发送复活时沿用同一个通道，订阅者照常收到广播。

        Private: the only consumer is :meth:`stream_messages`, which owns the
        deterministic unsubscribe via its context-manager ``__aexit__``.
        """
        managed = self.sessions.get(session_id)
        channel = managed.channel if managed is not None else self._session_channel(session_id)
        return channel, channel.subscribe()

    async def _unsubscribe(self, channel: SseChannel, session_id: str, queue: asyncio.Queue) -> None:
        """Remove a queue from a session's subscriber channel."""
        await channel.unsubscribe(queue)
        self._release_channel(session_id)

    @contextlib.asynccontextmanager
    async def stream_messages(
        self, session_id: str, *, idle_timeout: float = 20.0
    ) -> AsyncGenerator[AsyncIterator[SessionStreamEvent]]:
        """Subscribe to a session's messages as a self-cleaning async iterator.

        Yields an async iterator producing semantic events, in order:

        - a :class:`SubscriptionReady` barrier (always the first event) marking
          that the subscription is established — broadcasts after it are gap-free,
        - a :class:`LiveMessage` per broadcast message,
        - a :class:`Heartbeat` whenever *idle_timeout* elapses with no message
          (consumers run liveness / disconnect self-checks on it).

        The subscription outlives the session's residency: it survives eviction
        and keeps receiving once the session is revived. The stream ends only
        when the subscriber queue is dropped under backpressure — stream end is
        the reconnect signal; no overflow event reaches consumers (the overflow
        sentinel is internal to :class:`SseChannel`).

        Subscription, queue draining and unsubscribe all live behind this seam;
        cleanup is carried deterministically by ``__aexit__`` (see ADR-0005).
        Consume as ``async with stream_messages(...) as stream: async for event
        in stream``.
        """
        channel, queue = self._subscribe(session_id)

        async def _iter() -> AsyncIterator[SessionStreamEvent]:
            # NOTE: intentionally NO ``finally: _unsubscribe`` here. Cleanup is owned
            # by the enclosing context manager's __aexit__ (ADR-0005): a bare async
            # generator's finally only runs at GC on break/disconnect, which is the
            # exact leak this design avoids. Do not add a finally to this inner gen.
            yield SubscriptionReady()
            async for item in channel.iterate(queue, idle_timeout=idle_timeout):
                yield Heartbeat() if item is IDLE else LiveMessage(message=item)

        try:
            yield _iter()
        finally:
            await self._unsubscribe(channel, session_id, queue)

    async def get_status(self, session_id: str) -> SessionStatus | None:
        """Get session status."""
        if session_id in self.sessions:
            return self.sessions[session_id].status
        meta = await self.meta_store.get(session_id)
        return meta.status if meta else None

    async def shutdown_gracefully(self) -> None:
        """Gracefully shutdown all sessions using the actor teardown path."""
        patrol = getattr(self, "_patrol_task", None)
        if patrol is not None and not patrol.done():
            patrol.cancel()
            with contextlib.suppress(BaseException):
                await patrol

        sessions = list(self.sessions.values())
        if not sessions:
            return
        await asyncio.gather(
            *[self._evict_one(s) for s in sessions],
            return_exceptions=True,
        )
