"""ComfyUI endpoints: import routing, capability inference, request construction and execution.

An endpoint definition with ``kind: comfyui`` is an API-format workflow plus its node bindings (``docs/adr/0081`` and
``docs/adr/0082``). The definition contract and save-time checks live in ``arcreel_market_core.comfyui``: structural contract
``schema.json``, semantic checks ``validator``, semantic key registry ``bindings``, workflow-reading primitives
``workflow`` and ``graph``. This package is the runtime side: splitting a pasted payload three ways into definition / API
workflow / UI workflow is in :mod:`.import_shapes`, node binding inference in :mod:`.inference`, capabilities in :mod:`.capabilities`.

This package exports no aggregate entry; consumers import the submodules they need directly: the single entry for
definition validation is ``arcreel_market_core.endpoint_definition.validate_definition``, which dispatches by ``kind`` to
``arcreel_market_core.comfyui.validator``.

ComfyUI 端点：导入分流、能力推断、请求构造与执行。

``kind: comfyui`` 的端点定义是一份 API 格式 workflow 加它的节点绑定（``docs/adr/0081`` 与
``docs/adr/0082``）。定义契约与保存期判定在 ``arcreel_market_core.comfyui``：结构契约
``schema.json``、语义判定 ``validator``、语义键名录 ``bindings``、读 workflow 的原语
``workflow`` 与 ``graph``。本包是运行侧：把粘进来的载荷分成定义 / API workflow / UI workflow
三路在 :mod:`.import_shapes`，节点绑定推断在 :mod:`.inference`，能力在 :mod:`.capabilities`。

本包不导出聚合入口，消费方直接 import 需要的子模块：定义校验的唯一入口在
``arcreel_market_core.endpoint_definition.validate_definition``，它按 ``kind`` 分派到
``arcreel_market_core.comfyui.validator``。
"""
