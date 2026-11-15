"""Text backend factory.

provider/model resolution still lives here (resolver.text_backend_for_task); backend construction is funneled into
the unified seam assemble_backend (media_type=text): all built-in text providers go through the ProviderSpec table,
custom providers go through load_custom_backend moved down into lib, and the text side no longer has its own
imperative construction and custom resolution.

文本 backend 工厂。

provider/model 解析仍在此（resolver.text_backend_for_task），backend 构造收口到统一缝
assemble_backend（media_type=text）：内置文本 provider 全部经 ProviderSpec 表，
自定义 provider 经下移到 lib 的 load_custom_backend，文本侧不再各写一份命令式构造与自定义解析。
"""

from __future__ import annotations

from lib.backends.backend_assembly import assemble_backend
from lib.backends.backend_assembly.assembler import OutputLimitFacts, output_limit_facts
from lib.backends.text_backends.base import TextBackend, TextTaskType
from lib.config.resolver import ConfigResolver
from lib.db import async_session_factory


async def create_text_backend_for_task(
    task_type: TextTaskType,
    project_name: str | None = None,
) -> tuple[TextBackend, str]:
    """Create a text backend from DB config, returning the resolver-layer registry provider_id together with the backend.

    provider_id is the single source of truth for the billing provider, delivered paired with the backend; the caller
    (TextGenerator) bills by it, and backend.name is no longer a billing input.

    从 DB 配置创建文本 backend，随 backend 返回解析层 registry provider_id。

    provider_id 是记账 provider 的单一真相源，与 backend 成对交付；调用方（TextGenerator）
    据此记账，backend.name 不再作为记账输入。
    """
    resolver = ConfigResolver(async_session_factory)

    async with resolver.session() as r:
        provider_id, model_id = await r.text_backend_for_task(task_type, project_name)
        backend = await assemble_backend(
            provider_id=provider_id,
            media_type="text",
            model_id=model_id,
            resolver=r,
        )
    return backend, provider_id


async def text_model_output_limit(provider_id: str, model_id: str) -> OutputLimitFacts:
    """Read the max output length registered for the text model, and indicate whether it comes from a custom provider.

    读取文本模型登记的最大输出长度，并标明它是否由自定义供应商提供。"""
    return await output_limit_facts(
        provider_id=provider_id, model_id=model_id, resolver=ConfigResolver(async_session_factory)
    )
