"""Terminal-state judgement and artifact picking for one execution: both media channels read this one.

A ComfyUI history entry does not say "this succeeded": it lays out every node's outputs alongside a series of execution
events; success or failure must be read from the last event, the final output from the node the ``output`` binding points
to, and "is the output an image or a video" is only reliably told by the file extension (``VHS_VideoCombine`` can also
export animated webp; node type isn't enough to tell).

An extension is just a string in history and says nothing about the container of the bytes on disk, so this module gives a
separate file-header criterion (:func:`container_matches`), checked by the execution layer after writing to disk.

Judgement and picking are pure functions living in the subpackage: they only know the shape of history and the binding
table, unrelated to the backend layer's request / result types. The extension whitelist is kept in one table keyed by
``media_type`` rather than one per channel: two tables would mean two understandings of "what this endpoint should
produce", and an extension one side lets through would be judged a type mismatch by the other.

一次执行的终态判定与产物挑选：两个媒体通道共读这一份。

ComfyUI 的 history 条目不说「这次成功了」——它把每个节点的产出与一串执行事件摊在一起，成败
要读末尾事件，成片要读 ``output`` 绑定指的那个节点，而「产出的是图还是片」只有文件扩展名说得
准（``VHS_VideoCombine`` 也能导 webp 动图，节点类型不足以分辨）。

扩展名只是 history 里的一个字符串，说不了落盘字节的容器，故本模块另给一条文件头判据
（:func:`container_matches`），由执行层在落盘后核一遍。

判定与挑选都是纯函数，落在子包里：它们只认 history 的形状与绑定表，与 backend 层的请求 / 结果
类型无关。扩展名白名单按 ``media_type`` 收在一张表上，而不是各通道一份——两份表意味着「这个端点
该产什么」有两种理解，一边放行的扩展名另一边会判类型不符。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from arcreel_market_core.comfyui.bindings import targets_of
from lib.backends.container_sniff import sniff_container

from .failures import EXECUTION_ERROR, INTERRUPTED, ComfyuiError

#: ``media_type`` → extensions allowed for artifacts of that media type.
#:
#: The image side deliberately excludes ``.gif`` / ``.apng``: both are animations, and an image endpoint producing them means
#: the artifact binding points to the wrong node (most likely a video combine node); storing it as a storyboard image would
#: give an image with only the first frame. The video side excludes them too: animations are not final video containers.
#:
#: The three video extensions all belong to the ISO BMFF container family, the same rules as the three accepted for reference
#: video uploads (see ``server.services.currency.upload_finalize.UPLOAD_VIDEO_EXTENSIONS``): the final video lands on the
#: canonical ``.mp4`` path of ``lib.project.resource_paths`` with bytes moved as is, so only containers that fit that name are
#: accepted. ``.webm`` is not among them: with its bytes under a ``.mp4`` name, both the extension and the served MIME would lie.
#: ``media_type`` → 该媒体类型的产物允许的扩展名。
#:
#: 图像这一侧刻意不含 ``.gif`` / ``.apng``：两者是动图，一份图像端点产出它们意味着产物绑定指
#: 错了节点（多半指在了视频合成节点上），当成分镜图入库会得到一张只有首帧的图。视频这一侧同理
#: 不含它们——动图不是成片容器。
#:
#: 视频这三个扩展名同属 ISO BMFF 容器家族，与参考视频上传收的那三个（见
#: ``server.services.currency.upload_finalize.UPLOAD_VIDEO_EXTENSIONS``）是同一份口径：成片落
#: 在 ``lib.project.resource_paths`` 的规范 ``.mp4`` 路径上，字节原样搬运，故只收能装进这个名字
#: 的容器。``.webm`` 不在其中——它的字节装进 ``.mp4`` 的名字后，扩展名与下发 MIME 都在说谎。
ARTIFACT_SUFFIXES_BY_MEDIA_TYPE: Mapping[str, frozenset[str]] = {
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
    "video": frozenset({".mp4", ".mov", ".m4v"}),
}

#: ``media_type`` → container MIMEs allowed for artifacts of that media type, matching the extensions in the table above one to one:
#: the three video extensions all belong to ISO BMFF (all sniffed as ``video/mp4``), so video has only one item.
#: ``media_type`` → 该媒体类型的产物允许的容器 MIME，与上表的扩展名一一对上：视频那三个扩展名
#: 同属 ISO BMFF（一律嗅成 ``video/mp4``），故视频只有一项。
ARTIFACT_CONTAINERS_BY_MEDIA_TYPE: Mapping[str, frozenset[str]] = {
    "image": frozenset({"image/png", "image/jpeg", "image/webp"}),
    "video": frozenset({"video/mp4"}),
}

#: The three keys in a history entry that may hold artifacts. Only these three are read, and only for the node the ``output`` binding points to.
#: history 条目里可能挂产物的三个键。只读这三个，且只读 ``output`` 绑定的那个节点。
_ARTIFACT_KEYS = ("images", "gifs", "audio")

#: The two event names in ``status.messages`` meaning this execution failed to produce output. / ``status.messages`` 里表示这次执行没能出片的两个事件名。
_EXECUTION_ERROR_EVENT = "execution_error"
_EXECUTION_INTERRUPTED_EVENT = "execution_interrupted"

#: Placeholder value of ``node`` in ``comfyui_execution_error`` when the failing node can't be identified.
#: 认不出出错节点时 ``comfyui_execution_error`` 的 ``node`` 占位值。
_UNKNOWN_NODE = "-"


def output_nodes_of(definition: Mapping[str, Any]) -> list[str]:
    """Node ids the ``output`` binding points to, in binding order. / ``output`` 绑定指的那些节点号，按绑定次序。"""
    return [str(target["node"]) for target in targets_of((definition.get("bindings") or {}).get("output"))]


def output_artifacts(entry: Mapping[str, Any], output_nodes: Sequence[str]) -> list[Mapping[str, Any]]:
    """Files produced this time by nodes bound to ``output``, in binding order, and per node in the order of the three keys.

    Only the bound nodes are read: side branches in a workflow like ``PreviewImage`` also write artifacts to history, and
    scanning the whole graph would take a preview image as the final output. Entries with ``type != "output"`` are always
    skipped: ``temp`` is an intermediate preview the server may clean up at any time.

    ``output`` 绑定的节点这次产出的文件，按绑定次序、每个节点按三个键的次序。

    只读被绑定的那些节点：一份 workflow 里 ``PreviewImage`` 之类的旁支同样会往 history 写产物，
    扫全图会把一张预览图当成成片取走。``type != "output"`` 的条目一律跳过——``temp`` 是中间预览，
    服务端随时会清掉它。
    """
    outputs = entry.get("outputs")
    if not isinstance(outputs, Mapping):
        return []
    found: list[Mapping[str, Any]] = []
    for node_id in output_nodes:
        node = outputs.get(node_id)
        if not isinstance(node, Mapping):
            continue
        for key in _ARTIFACT_KEYS:
            items = node.get(key)
            if not isinstance(items, list):
                continue
            found.extend(
                item for item in items if isinstance(item, Mapping) and str(item.get("type") or "") == "output"
            )
    return found


def pick_artifact(artifacts: Sequence[Mapping[str, Any]], media_type: str) -> Mapping[str, Any] | None:
    """The first file in this batch of artifacts that belongs to ``media_type``; ``None`` if none.

    Picked by extension rather than taking the first: a bound node can write to both ``images`` and ``gifs`` (thumbnail plus
    final output), and the order :func:`output_artifacts` gives is ``_ARTIFACT_KEYS``'s own order, unrelated to "which is the final output".

    Extensions outside the whitelist never count as a hit, nor does an unregistered ``media_type``: in those two cases the
    caller reports ``comfyui_output_type_mismatch``, telling the user "the binding points to the wrong node", rather than
    taking a file that shouldn't be stored as this version's artifact.

    这批产物里第一个属于 ``media_type`` 的文件；一个都没有时 ``None``。

    按扩展名挑而不是取第一个：一个绑定节点可以同时往 ``images`` 与 ``gifs`` 写（缩略图加成片），
    而 :func:`output_artifacts` 给出的次序是 ``_ARTIFACT_KEYS`` 自己的次序，与「哪个是成片」无关。

    白名单外的扩展名一律不算命中，连同没有登记的 ``media_type``：那两种情形下调用方会报
    ``comfyui_output_type_mismatch``，把「绑定指错了节点」说给用户，而不是把一份不该入库的文件
    当成这一版的产物。
    """
    allowed = ARTIFACT_SUFFIXES_BY_MEDIA_TYPE.get(media_type, frozenset())
    return next((item for item in artifacts if Path(filename_of(item)).suffix.lower() in allowed), None)


def expected_suffixes_text(media_type: str) -> str:
    """Extensions allowed for ``media_type``, joined into the list part of the failure message; empty string for unregistered media types.

    The read side computes it on the fly from the stored ``media_type`` when rendering ``comfyui_output_type_mismatch``, so
    the whitelist is not part of the stored params: the list is a projection of a static table, not a fact of this failure,
    and storing a copy would only let history records and the table disagree.

    ``media_type`` 允许的扩展名，拼成失败文案里那一段清单；未登记的媒体类型给空串。

    读侧渲染 ``comfyui_output_type_mismatch`` 时按落库的 ``media_type`` 现算，白名单因此不进
    落库参数：清单是一张静态表的投影，不是这次失败的事实，存一份只会让历史记录与表各说各话。
    """
    return " / ".join(sorted(ARTIFACT_SUFFIXES_BY_MEDIA_TYPE.get(media_type, frozenset())))


def filename_of(artifact: Mapping[str, Any]) -> str:
    """File name of an artifact entry; empty string when missing (both the extension check and the failure message tolerate it).

    一个产物条目的文件名；缺失时空串（扩展名判定与失败文案都容得下它）。"""
    return str(artifact.get("filename") or "")


def container_matches(head: bytes, media_type: str) -> bool:
    """Whether this file header is the container ``media_type`` should have; never for an unregistered ``media_type``.

    The extension whitelist only governs the string in history, and a save node can perfectly well write webm bytes under a
    ``.mp4`` name. The bytes on disk are ultimately served with a MIME declared by extension, so the container must be checked again by file header.

    Magic numbers are not recognized here (:func:`lib.backends.container_sniff.sniff_container`); this function only says
    "whether the recognized container counts as what this kind should produce": the three video extensions all belong to ISO
    BMFF, so ``video/webm`` is not in the video allowed set.

    这段文件头是不是 ``media_type`` 该有的容器；没有登记的 ``media_type`` 一律不是。

    扩展名白名单只管 history 里那个字符串，而一个保存节点完全可以把 webm 的字节写进 ``.mp4``
    的名字。落盘的字节最终按扩展名声明 MIME 并下发，故容器要按文件头再核一遍。

    魔数本身不在这里认（:func:`lib.backends.container_sniff.sniff_container`），本函数只说
    「认出来的这个容器算不算这一类该产的」——视频那三个扩展名同属 ISO BMFF，``video/webm``
    因此不在视频的允许集里。
    """
    return sniff_container(head) in ARTIFACT_CONTAINERS_BY_MEDIA_TYPE.get(media_type, frozenset())


def terminal_failure(entry: Mapping[str, Any]) -> ComfyuiError | None:
    """Whether a terminal record says success or failure: a failure code for failure, ``None`` for success.

    The criterion is the **last event** of ``status.messages`` rather than ``status_str`` / ``completed``: within one execution
    it is normal for an earlier node to error while later nodes still run to completion; judging by "did an error ever occur"
    would misjudge a finished output as failure, and ``completed`` is also true for an interrupted execution.

    When ``status`` is null (the shape of some versions and proxies) there is nothing to judge by: this case goes by outputs:
    if there is output, treat it as having finished; with no output at all fall back to execution failure, otherwise the
    execution would go all the way to "the artifact node produced nothing", calling an environment problem a misconfigured binding.

    一条终态记录说的是成功还是失败——失败给出失败码，成功给 ``None``。

    判据是 ``status.messages`` 的**末尾事件**而不是 ``status_str`` / ``completed``：一次执行里
    前面的节点报错、后面的节点照跑完是常态，按「有没有出现过 error」判会把成片误判成失败，而
    ``completed`` 在被打断的执行上同样为真。

    ``status`` 为 null（部分版本与代理的形状）时无从判起：这一格照 outputs 分——有产出就当它跑
    完了，一个产出都没有则按执行失败兜底，否则这次执行会一路走到「产物节点没出东西」，把一个
    环境问题说成绑定配错了。
    """
    status = entry.get("status")
    if not isinstance(status, Mapping):
        if _has_outputs(entry):
            return None
        return ComfyuiError(
            EXECUTION_ERROR,
            node=_UNKNOWN_NODE,
            detail="ComfyUI reported no execution status and no outputs",
        )
    event, data = _last_message(status)
    if event == _EXECUTION_ERROR_EVENT:
        return ComfyuiError(EXECUTION_ERROR, node=_error_node(data), detail=_error_detail(data))
    if event == _EXECUTION_INTERRUPTED_EVENT:
        return ComfyuiError(INTERRUPTED)
    return None


def history_digest(entry: Mapping[str, Any], output_nodes: Sequence[str]) -> dict[str, Any]:
    """History digest for tracing: keeps only the status and the ``output`` node section.

    The whole history carries every node's full outputs, and a single trace could swell the diagnostic column to hundreds of
    KB, while troubleshooting only needs these two parts.

    留痕用的 history 摘要：只留状态与 ``output`` 节点那一段。

    整份 history 带着每个节点的全部产出，一条留痕就能把诊断列撑到几百 KB，而排查要看的只有这
    两块。
    """
    outputs = entry.get("outputs")
    kept = (
        {node_id: outputs[node_id] for node_id in output_nodes if node_id in outputs}
        if isinstance(outputs, Mapping)
        else {}
    )
    return {"status": entry.get("status"), "outputs": kept}


def _last_message(status: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """The last event of ``status.messages``, shaped ``[event name, data]``; the event name is an empty string when there are no messages.

    ``status.messages`` 的末尾事件，形状是 ``[事件名, 数据]``；没有消息时事件名为空串。"""
    messages = status.get("messages")
    if not isinstance(messages, list) or not messages:
        return "", {}
    last = messages[-1]
    if not isinstance(last, list) or not last:
        return "", {}
    data = last[1] if len(last) > 1 and isinstance(last[1], Mapping) else {}
    return str(last[0] or ""), data


def _error_node(data: Mapping[str, Any]) -> str:
    """The failing node's name as the user knows it: ``node_type`` is the node type on the canvas; falls back to the node id when unrecognized.

    报错节点在用户那里的名字：``node_type`` 就是画布上的节点类型，认不出退到节点号。"""
    return str(data.get("node_type") or data.get("node_id") or _UNKNOWN_NODE)


def _error_detail(data: Mapping[str, Any]) -> str:
    """The exception summary takes only the message and type lines, no traceback: that runs to hundreds of lines, and the failure reason is stored in full.

    异常摘要只取消息与类型两行，不带 traceback——它有上百行，而失败原因要整条落库。"""
    message = str(data.get("exception_message") or "").strip()
    exception_type = str(data.get("exception_type") or "").strip()
    if message and exception_type:
        return f"{exception_type}: {message}"
    return message or exception_type or "node raised an exception"


def _has_outputs(entry: Mapping[str, Any]) -> bool:
    outputs = entry.get("outputs")
    return isinstance(outputs, Mapping) and bool(outputs)
