"""Agent runtime data models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

SessionStatus = Literal["idle", "running", "completed", "error", "interrupted", "closed"]


@dataclass(frozen=True, slots=True)
class SubscriptionReady:
    """First event of the session message stream: a barrier marker that the subscription has been established
    atomically.

    After consuming this event the consumer can be sure the live broadcasts after it have no gap: the entry stream
    uses it as the boundary, first back-filling existing entries from the DB and then consuming live messages, with
    duplicates filtered by the seq threshold (identity comparison).

    会话消息流的首个事件：订阅已原子建立的屏障标记。

    消费方消费到该事件后，可确信其后的直播广播无缝隙——entry 流以此为界
    先补库读存量条目，再消费直播消息，重复由 seq 门槛过滤（身份比对）。
    """


@dataclass(frozen=True, slots=True)
class LiveMessage:
    """Live event of the session message stream: messages broadcast one by one after the subscription barrier.

    会话消息流的直播事件：订阅屏障之后逐条广播的消息。"""

    message: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Heartbeat event of the session message stream: produced when there is no message within idle_timeout.

    The consumer runs liveness self-checks on it (SSE checks for disconnect, sync collectors check deadline/session
    state), guaranteeing a deterministic wake-up even during idle periods (see ADR-0005).

    会话消息流的心跳事件：idle_timeout 内无消息时产出。

    消费方在其上执行存活自检（SSE 查断线、同步收集方查 deadline/会话状态），
    保证空闲期也有确定性的醒来时机（见 ADR-0005）。
    """


QueuedMessageState = Literal["queued", "unsent"]
"""排队消息的状态。``queued``：已交给 CLI，在 CLI 的队列里等待被并入一轮。``unsent``：CLI 退出时
仍在排队，没有被处理；只有用户点发送才重新交给 CLI，从不自动重发。"""

WithdrawalIntent = Literal["edit", "delete"]
"""用户撤回排队消息的意图：``edit`` 把内容退回输入框，``delete`` 直接丢弃。"""


@dataclass(slots=True)
class QueuedMessage:
    """Queued message: a user message handed to the CLI but not yet accepted into the conversation by the Agent (server
    in-memory state, cleaned up with the session).

    ``id`` is the uuid of the user entry after acceptance and does not change over the message's lifetime;
    ``cli_uuid`` is the message uuid carried when sent to the CLI, by which the CLI's ``command_lifecycle`` frames
    and user message replay identify this message.

    排队消息：已交给 CLI、尚未被 Agent 接纳进对话的用户消息（服务端内存态，随会话清理）。

    ``id`` 即被接纳后用户条目的 uuid，在消息的整个生命周期内不变；``cli_uuid`` 是送入 CLI 时
    携带的消息 uuid，CLI 的 ``command_lifecycle`` 帧与用户消息回放都以它指认这条消息。
    """

    entry: dict[str, Any]
    """被接纳时写入会话事件日志的用户条目。"""
    cli_uuid: str
    content: str | list[dict[str, Any]] = ""
    """送入 CLI 的消息内容。「未发送」消息重发、立即发送或被 CLI 丢弃后重排时，原样再送一次。"""
    client_key: str | None = None
    state: QueuedMessageState = "queued"
    withdrawal: WithdrawalIntent | Literal["send_now"] | None = None
    """用户要求撤回时的意图，``send_now`` 是立即发送：撤回后以 ``now`` 优先级重新送入。CLI 答复撤回失败后
    仍保留：CLI 随后若报 cancelled，按这个意图收尾。"""
    withdrawing: bool = False
    """撤回请求正在等待 CLI 答复。"""
    withdrawn: bool = False
    """已按用户撤回离开排队。"""
    id: str = field(init=False)

    def __post_init__(self) -> None:
        self.id = str(self.entry["uuid"])

    def to_payload(self) -> dict[str, Any]:
        """External shape: shared by the send response and the queued message event of the entry stream.

        对外形状：发送响应与 entry 流的排队消息事件共用。"""
        return {
            "id": self.id,
            "content": self.entry.get("content", []),
            "timestamp": self.entry.get("timestamp"),
            "state": self.state,
        }


SessionStreamEvent = SubscriptionReady | LiveMessage | Heartbeat
"""``SessionManager.stream_messages`` 产出的语义化事件。

序列协议：SubscriptionReady（恰好一次、必为首个）→ LiveMessage / Heartbeat 交错；
订阅队列溢出以流结束表达，流结束即重连信号，无专门事件。
"""


class SessionMeta(BaseModel):
    """Session metadata stored in database."""

    id: str  # Exposed externally, filled with the sdk_session_id value / 对外暴露，填充 sdk_session_id 值
    project_name: str
    title: str = ""
    status: SessionStatus = "idle"
    superseded_by: str | None = None
    """非空表示本会话已被分支会话取代，值为新会话的 sdk_session_id。"""
    fork_parent_session_id: str | None = None
    """非空表示本会话由分叉产生，值为被分叉会话的 sdk_session_id。"""
    fork_anchor_uuid: str | None = None
    """分叉锚点在 transcript 域的 entry uuid，与 fork_parent_session_id 同时写入。"""
    created_at: datetime
    updated_at: datetime
