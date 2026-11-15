"""Domain errors for JianYing draft export and download: problem codes are stable; HTTP, Agent tools and queue tasks each map them.

剪映草稿导出与下载的领域错误：问题码稳定，HTTP、Agent 工具与队列任务各自映射。"""

from __future__ import annotations

from typing import Any, Literal

type JianyingDraftErrorCode = Literal[
    "jianying_draft_narration_unavailable",
    "jianying_draft_blocked",
    "jianying_draft_empty",
    "jianying_draft_presentation_unavailable",
    "jianying_draft_hold_frame_unavailable",
    "jianying_draft_acceptance_failed",
    "jianying_draft_not_exported",
    "jianying_draft_invalid",
]


class JianyingDraftError(Exception):
    """The JianYing draft cannot be exported, failed verification or cannot be downloaded; ``code`` is for adapter mapping, ``params`` carries locating info.

    剪映草稿无法导出、未通过验收或无法下载；``code`` 供适配器映射，``params`` 携带定位信息。"""

    def __init__(self, code: JianyingDraftErrorCode, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code: JianyingDraftErrorCode = code
        self.params = params


__all__ = ["JianyingDraftError", "JianyingDraftErrorCode"]
