"""backend_assembly — the unified construction seam for "provider config + model → backend".

Exposes the single entry assemble_backend, internally routing by is_custom_provider to the built-in / custom families.
The two families share the entry but not table structure (see docs/adr/0039): the built-in side uses the ProviderSpec
closure table (specs.py), the custom side delegates to the existing ENDPOINT_REGISTRY (lib/custom_provider/endpoints.py).

backend_assembly — 「provider config + model → backend」的统一构造缝。

暴露唯一入口 assemble_backend，内部按 is_custom_provider 分流到内置 / 自定义两族。
两族共享入口、不共享表结构（见 docs/adr/0039）：内置侧用 ProviderSpec 闭包表（specs.py），
自定义侧委托现成 ENDPOINT_REGISTRY（lib/custom_provider/endpoints.py）。
"""

from __future__ import annotations

from lib.backends.backend_assembly.assembler import assemble_backend

__all__ = ["assemble_backend"]
