"""Failure carrier and failure codes of the ComfyUI channel.

One exception type spans construction time and execution time: both stages are within the same generation, and the stored
shape (stable ``code`` + serializable ``params``) and read-side rendering path are the same; splitting into two types would
only add a slot each to the worker's exception union and ``call_failure``'s classification, with nowhere branching on type.

``code`` must be registered in ``FAILURE_CODE_KEYS`` of ``lib.generation.task_failure`` and ``_TASK_FAILURE_ACTIONS`` of
``lib.generation.generation_result``, otherwise the worker downgrades to raw text when encoding the failure reason.

ComfyUI 通道的失败载体与失败码。

一个异常类型贯穿构造期与执行期：两段都在同一次生成里，落库的形状（稳定 ``code`` + 可序列化
``params``）与读侧的渲染路径也是同一条，分成两个类型只会让 worker 的异常联合与 ``call_failure``
的分类各多一格，而没有任何一处按类型分叉。

``code`` 必须已登记进 ``lib.generation.task_failure`` 的 ``FAILURE_CODE_KEYS`` 与 ``lib.generation.generation_result``
的 ``_TASK_FAILURE_ACTIONS``，否则 worker 编码失败原因时会降级成裸文本。
"""

from __future__ import annotations

from typing import Any

#: Asset upload failed: an address, credential or disk problem; resending the same request might just work.
#: 素材上传失败：地址、凭据或磁盘的问题，重发同一请求可能就好了。
UPLOAD_FAILED = "comfyui_upload_failed"

#: ComfyUI rejected this workflow (400, or 200 with non-empty ``node_errors``): missing model, parameter out of range, node doesn't exist.
#: ComfyUI 拒收这份 workflow（400，或 200 带非空 ``node_errors``）：缺模型、参数越界、节点不存在。
NODE_ERRORS = "comfyui_node_errors"

#: Submit succeeded once, but there is no record in history, and neither running nor pending of ``/queue`` contains this id.
#: A server restart drops the queue together with executions not yet written to history; this is the only shape of that visible to the client.
#: 提交成功过，但 history 里没有记录、``/queue`` 的 running 与 pending 也都不含这个 id。
#: 服务端重启会把队列连同未落 history 的执行一起丢掉，这是它在客户端唯一看得见的形状。
JOB_LOST = "comfyui_job_lost"

#: A node raised an exception during execution (the last event of ``status.messages`` is ``execution_error``).
#: 执行过程中某个节点抛了异常（``status.messages`` 末尾是 ``execution_error``）。
EXECUTION_ERROR = "comfyui_execution_error"

#: Execution was interrupted (the last event of ``status.messages`` is ``execution_interrupted``): someone pressed cancel in
#: ComfyUI, or another client sent ``/interrupt``.
#: 执行被打断（``status.messages`` 末尾是 ``execution_interrupted``）：有人在 ComfyUI 上按了
#: 取消、或另一个客户端发了 ``/interrupt``。
INTERRUPTED = "comfyui_interrupted"

#: Execution reached a terminal state, but the nodes bound to ``output`` produced no file with ``type == "output"``.
#: 执行到了终态，但 ``output`` 绑定的节点没有产出任何 ``type == "output"`` 的文件。
OUTPUT_MISSING = "comfyui_output_missing"

#: The artifact exists, but its extension is not the kind this endpoint's ``media_type`` should have.
#: 产物在，但扩展名不是这个端点 ``media_type`` 该有的那一类。
OUTPUT_TYPE_MISMATCH = "comfyui_output_type_mismatch"

#: The artifact's extension matches, but the file header of the bytes on disk is not a container of this ``media_type``.
#: 产物的扩展名对得上，但落盘字节的文件头不是这个 ``media_type`` 的容器。
OUTPUT_CONTAINER_MISMATCH = "comfyui_output_container_mismatch"

#: The cascade from image-reading nodes to delete for reference images or first/end frames reached the artifact node (construction time, see ``request_builder``).
#: 参考图或首尾帧要删的读图节点，其级联触到了产物节点（构造期，见 ``request_builder``）。
IMAGE_DROP_UNSUPPORTED = "comfyui_image_drop_unsupported"


class ComfyuiError(RuntimeError):
    """ComfyUI channel failure, carrying a persistable, localizable stable failure code.

    Same shape as the declarative runtime's ``DeclarativeRuntimeError`` (``code`` + ``params``), so encoding and rendering of
    failure reasons need no separate path for ComfyUI.

    ComfyUI 通道失败，携带可持久化、可本地化的稳定失败码。

    形状与声明式运行时的 ``DeclarativeRuntimeError`` 一致（``code`` + ``params``），失败原因的
    编码与渲染两侧因此不必为 ComfyUI 另写一条路径。
    """

    def __init__(self, code: str, **params: Any) -> None:
        self.code = code
        self.params: dict[str, Any] = params
        super().__init__(code)
