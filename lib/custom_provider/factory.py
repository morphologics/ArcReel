"""Custom provider Backend factory (dispatched by endpoint). / 自定义供应商 Backend 工厂（按 endpoint 派发）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arcreel_market_core.endpoint_definition import COMFYUI_KIND
from lib.backends.image_capabilities import custom_image_model_capabilities
from lib.custom_provider.backends import (
    CustomAudioBackend,
    CustomImageBackend,
    CustomTextBackend,
    CustomVideoBackend,
)
from lib.custom_provider.capabilities import synthesize_video_capabilities_with_overrides
from lib.custom_provider.endpoints import EndpointSpec, get_endpoint_spec
from lib.custom_provider.model_facts import image_model_facts
from lib.custom_provider.path_overrides import effective_path_overrides

if TYPE_CHECKING:
    from lib.db.models.custom_provider import CustomProvider


def create_custom_backend(
    *,
    provider: CustomProvider,
    model_id: str,
    endpoint: str,
    capability_overrides: object | None = None,
    path_overrides: object | None = None,
    endpoint_spec: EndpointSpec | None = None,
    entry_binding: str | None = None,
) -> CustomTextBackend | CustomImageBackend | CustomVideoBackend | CustomAudioBackend:
    """Look up the endpoint in ENDPOINT_REGISTRY and build the Backend.

    Video backends additionally get the effective capabilities injected (endpoint spec system judgement ⊕ model-level user
    override); the execution layer gates on these instead of reading the wrapped backend's declaration directly. Image backends
    get the model-fact side's text-to-image / image-to-image injected (entry ⊕ capability override; no constraint for ComfyUI
    endpoints); the execution layer takes its intersection with the channel's declaration.

    Args:
        provider: custom provider ORM object (needs base_url / api_key / provider_id attributes)
        model_id: the concrete model id used for this call
        endpoint: key in ENDPOINT_REGISTRY
        capability_overrides: the model's raw `capability_overrides` value; None = follow the system judgement throughout
        path_overrides: the model's raw `path_overrides` value; request names the endpoint no longer declares are ignored (docs/adr/0101)
        entry_binding: the model's entry binding ("model vendor/official model name"); None = match the entry by name

    Raises:
        ValueError: endpoint is not in ENDPOINT_REGISTRY

    按 endpoint 查 ENDPOINT_REGISTRY 并构造 Backend。

    视频后端额外注入生效能力（endpoint spec 系统判定 ⊕ 模型级用户覆盖），执行层据此门控，
    不直接读被包装 backend 的声明。图片后端注入模型事实一侧的文生图 / 图生图（条目 ⊕ 能力覆盖，
    ComfyUI 端点不加约束），执行层取它与通道声明的交集。

    Args:
        provider: 自定义供应商 ORM 对象（需 base_url / api_key / provider_id 属性）
        model_id: 该次调用使用的具体模型 id
        endpoint: ENDPOINT_REGISTRY 的键
        capability_overrides: 该模型的 `capability_overrides` 原始值；None = 全部跟随系统判定
        path_overrides: 该模型的 `path_overrides` 原始值；端点已不声明的请求名忽略（docs/adr/0101）
        entry_binding: 该模型的条目绑定（「模型厂商/官方模型名」）；None = 按名字对应条目

    Raises:
        ValueError: endpoint 不在 ENDPOINT_REGISTRY 中
    """
    spec = endpoint_spec or get_endpoint_spec(endpoint)
    paths = effective_path_overrides(path_overrides, spec.requests)
    backend = spec.build_backend(provider, model_id, paths)
    if isinstance(backend, CustomVideoBackend):
        merged, applied = synthesize_video_capabilities_with_overrides(
            endpoint=endpoint,
            model_id=model_id,
            overrides=capability_overrides,
            endpoint_spec=spec,
        )
        # Remember both the endpoint and the path overrides: both are the real input granularity of backend construction; resuming an
        # already submitted task compares them to tell whether the protocol has been swapped out (docs/adr/0054, docs/adr/0101).
        # 一并记住 endpoint 与路径覆盖：两者都是 backend 构造的真正输入粒度，已提交任务的续跑
        # 据此比对协议是否已被换掉（docs/adr/0054、docs/adr/0101）。
        return backend.with_endpoint(endpoint, path_overrides=paths).with_video_capabilities(merged, overrides=applied)
    if isinstance(backend, CustomImageBackend):
        # a fixed-size model's allowed sizes (its row's ``sizes`` override); ComfyUI endpoints size from the workflow
        sizes = (
            None
            if spec.kind == COMFYUI_KIND
            else image_model_facts(model_id, capability_overrides, entry_binding=entry_binding).sizes
        )
        return backend.with_model_capabilities(
            custom_image_model_capabilities(
                model_id, overrides=capability_overrides, endpoint_kind=spec.kind, entry_binding=entry_binding
            ),
            sizes,
        )
    return backend
