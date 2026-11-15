"""LoadedConfig — product of the async loading stage of the built-in backend construction seam, and the sole input of the sync construction stage.

Carries three things: (1) the credential overlay (api_key / access_key / secret_key / base_url etc. resolved from
db_config, key names are column names, per ADR 0037 "registry key name = column name = constructor parameter name =
config key"); (2) provider registry meta (sources of default_base_url / api_model_name); (3) the shared rate_limiter.

Construction closures only read this envelope to build the backend, never querying the DB or awaiting -- this divide is
the structural guarantee that "construction can be unit-tested directly without a DB" (hand-craft a LoadedConfig +
model_id and assert on the resulting constructor arguments).

LoadedConfig — 内置 backend 构造缝的 async 装载段产物、sync 构造段唯一输入。

承载三样东西：① 凭证 overlay（db_config 解析出的 api_key / access_key / secret_key /
base_url 等，键名即列名，承 ADR 0037「registry key 名 = 列名 = 构造参数名 = config key」）；
② provider registry meta（default_base_url / api_model_name 来源）；③ 共享 rate_limiter。

构造闭包只读这个信封拼 backend，不查 DB、不 await —— 这条中线是「构造可脱离 DB 直接单测」的
结构保证（手搓一个 LoadedConfig + model_id 即可断言造出的构造参数）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lib.config.registry import ProviderMeta


@dataclass(frozen=True)
class LoadedConfig:
    """The sole input envelope of built-in build closures. / 内置侧 build 闭包的唯一输入信封。"""

    credentials: dict[str, str | None]
    provider_meta: ProviderMeta | None
    # The simple family's _build_simple doesn't consume rate_limiter (consistent with the pre-migration _fill_simple_provider_kwargs: rate limiting is
    # built into each simple backend's own httpx client); the envelope reserves this slot for build closures of special families migrated later (e.g. gemini media).
    # 简单族 _build_simple 不消费 rate_limiter（与迁移前 _fill_simple_provider_kwargs 一致：限流由
    # 简单后端各自的 httpx 客户端内建）；信封预留此槽供后续迁入的特例族 build 闭包（如 gemini 媒体）读取。
    rate_limiter: Any | None
