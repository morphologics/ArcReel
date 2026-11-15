"""The single mapping from SDK result messages to ArcReel session final states.

SDK result 消息到 ArcReel 会话终态的唯一映射。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from server.agent_runtime.models import SessionStatus

_SESSION_STATUSES = frozenset({"idle", "running", "completed", "error", "interrupted"})


def resolve_result_status(
    message: Mapping[str, Any],
    *,
    interrupt_requested: bool = False,
    preempted: bool = False,
) -> SessionStatus:
    """Map a result to the session final state; user interruption takes precedence over the SDK's generic error final
    state.

    ``preempted``: a send-now message has not yet been accepted. The turn the CLI interrupted for it still reports
    success and can only be recognized by an ``aborted_*`` ``terminal_reason``; it is recorded as interrupted, same
    as a user stop.

    把 result 映射为会话终态；用户中断优先于 SDK 的通用错误终态。

    ``preempted``：有立即发送的消息尚未被接纳。CLI 为它打断的那一轮仍报 success，只能凭
    ``terminal_reason`` 的 ``aborted_*`` 认出，与用户停止一样记为中断。
    """
    explicit = str(message.get("session_status") or "").strip().lower()
    if explicit in _SESSION_STATUSES:
        return cast(SessionStatus, explicit)
    if preempted and str(message.get("terminal_reason") or "").startswith("aborted_"):
        return "interrupted"
    subtype = str(message.get("subtype") or "").strip().lower()
    is_error = bool(message.get("is_error")) or subtype.startswith("error")
    if interrupt_requested and (subtype in {"interrupted", "interrupt"} or is_error):
        return "interrupted"
    return "error" if is_error else "completed"


def result_indicates_error(message: Mapping[str, Any]) -> bool:
    """Decide whether it is a failure only from the structured markers the SDK has given, without interpreting the
    error cause.

    只根据 SDK 已给出的结构化标记判断是否为失败，不解释错误原因。"""
    return resolve_result_status(message) == "error"
