"""Import routing: is a pasted JSON an endpoint definition, an API-format workflow, or a UI-format workflow?

The most common file users have is not an endpoint definition but a workflow exported by ComfyUI itself, and the export menu
has two items: "Export" gives the UI format (a canvas save with ``nodes`` / ``links``, no ``class_type``, can't be
submitted), "Export (API)" gives the node table used for submission. Neither carries ``kind``, so they are told apart by
shape: otherwise the user could only guess from a pile of field-level errors that they used the wrong menu item.

The API format is wrapped here into a ``kind: comfyui`` definition with empty node bindings: wrapping only brings it onto
the endpoint definition path; binding inference is a separate step, and at this point no semantic is resolved yet.

导入分流：一份粘进来的 JSON 是端点定义、API 格式 workflow，还是 UI 格式 workflow。

用户手上最常见的文件不是端点定义，而是 ComfyUI 自己导出的 workflow，且导出菜单有两项：
「Export」给出 UI 格式（带 ``nodes`` / ``links`` 的画布存档，没有 ``class_type``，提交不了），
「Export (API)」给出提交用的节点表。两者都不带 ``kind``，靠形状区分：分不出来就只能让用户对着
一堆字段级报错猜自己导错了菜单项。

API 格式在这里被包成一份 ``kind: comfyui`` 定义，节点绑定留空——包装只是把它接进端点定义这条
路，绑定推断是另一步，此刻还没有任何一项语义有着落。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from arcreel_market_core.comfyui.validator import CURRENT_SCHEMA_VERSION
from arcreel_market_core.comfyui.workflow import has_any_node
from arcreel_market_core.definition_diagnostics import (
    ROOT_PATH,
    DefinitionDiagnostics,
    DefinitionErrorCode,
    DefinitionIssue,
)
from arcreel_market_core.endpoint_definition.kinds import COMFYUI_KIND

#: Placeholder values written into ``meta`` when auto-wrapping: the raw workflow has no author or name to take; the user rewrites them before saving.
#: 自动包装时写进 ``meta`` 的占位值：原始 workflow 里没有作者与名称可取，保存前由用户改写。
PLACEHOLDER_META_NAME = "ComfyUI workflow"
PLACEHOLDER_META_AUTHOR = "unknown"
PLACEHOLDER_META_VERSION = "1.0.0"


class ImportShape(StrEnum):
    """一份导入载荷的形状。"""

    #: An endpoint definition with ``kind``, validated per its own kind. / 带 ``kind`` 的端点定义，按它自己的 kind 校验。
    ENDPOINT_DEFINITION = "endpoint_definition"
    #: ComfyUI "Export (API)" node table, automatically wrapped into a ComfyUI endpoint definition.
    #: ComfyUI「Export (API)」的节点表，自动包成 ComfyUI 端点定义。
    COMFYUI_API_WORKFLOW = "comfyui_api_workflow"
    #: ComfyUI "Export" canvas save; can't be submitted, rejected with a prompt to use Export (API) instead.
    #: ComfyUI「Export」的画布存档，提交不了，拒绝并让用户改用 Export (API)。
    COMFYUI_UI_WORKFLOW = "comfyui_ui_workflow"


def route_import_payload(document: object, *, media_type: str) -> tuple[ImportShape, Any]:
    """Determine the shape of an import payload, and when it is an API workflow also wrap it into an endpoint definition.

    Non-objects always go as endpoint definitions: the definition's container layer already reports "definition must be an
    object", and switching to "this is not a workflow" would describe the same root type error as two things. The UI format is
    recognized by ``nodes`` being an array plus a ``links``: API format keys are node ids whose values are node objects, not arrays, so the two can't collide.

    An object with no nodes at all also goes as an endpoint definition: an endpoint definition that forgot ``kind`` should hear
    "missing kind" here, not have each of its sections treated as a node and get back a series of secondary errors located at ``workflow.<section name>``.

    判定一份导入载荷的形状，并在它是 API workflow 时一并包成端点定义。

    非对象一律按端点定义走：定义的容器层已经会报出「定义必须是对象」，改口说「这不是 workflow」
    只会把根上的同一个类型错误说成两件事。UI 格式认 ``nodes`` 是数组加一个 ``links``——API 格式
    的键是节点 id，它的值是节点对象而非数组，两者撞不上。

    一个节点也没有的对象同样按端点定义走：漏写 ``kind`` 的端点定义此时该听见「缺 kind」，
    而不是它的每一节都被当成节点、报回一串定位到 ``workflow.<节名>`` 的次生错误。
    """
    if not isinstance(document, Mapping) or "kind" in document:
        return ImportShape.ENDPOINT_DEFINITION, document
    if isinstance(document.get("nodes"), list) and "links" in document:
        return ImportShape.COMFYUI_UI_WORKFLOW, document
    if not has_any_node(document):
        return ImportShape.ENDPOINT_DEFINITION, document
    return ImportShape.COMFYUI_API_WORKFLOW, wrap_api_workflow(document, media_type=media_type)


def ui_workflow_refusal() -> DefinitionDiagnostics:
    """Refusal reason for the UI format: it doesn't go into the definition validator; one structured diagnostic says which export menu to use instead.

    UI 格式的拒绝理由：它不进定义校验器，一条结构化的诊断说清该改用哪个导出菜单。"""
    return DefinitionDiagnostics(errors=(DefinitionIssue(ROOT_PATH, DefinitionErrorCode.COMFYUI_UI_FORMAT_WORKFLOW),))


def wrap_api_workflow(workflow: Mapping[str, Any], *, media_type: str) -> dict[str, Any]:
    """Wrap a raw API workflow into a ComfyUI endpoint definition.

    ``bindings`` is an empty object rather than per-key empty lists: an empty list means "explicitly unsupported", while at this
    point every item simply hasn't been inferred yet. The produced definition therefore fails "prompt and artifact must be bound", which is its true state.

    把一份原始 API workflow 包成 ComfyUI 端点定义。

    ``bindings`` 是空对象而不是逐键的空列表：空列表表示「显式不支持」，而此刻每一项都只是还没
    推断过。产出的定义因此过不了「提示词与产物必须绑定」，这正是它的真实状态。
    """
    return {
        "kind": COMFYUI_KIND,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "meta": {
            "name": PLACEHOLDER_META_NAME,
            "author": PLACEHOLDER_META_AUTHOR,
            "version": PLACEHOLDER_META_VERSION,
        },
        "media_type": media_type,
        "workflow": dict(workflow),
        "bindings": {},
    }
