"""Spec resolution split by endpoint key: built-ins via table lookup, ``ce-`` prefix read from the DB and built on the fly.

``get_endpoint_spec`` stays a synchronous pure table lookup (the built-in registry is a constant in place at import time);
callers that need to see both namespaces use this module's :func:`resolve_endpoint_spec` instead. The order is decided
entirely by the prefix; there is no "custom first, then built-in" fallback: the two namespaces share a domain but never
overlap (built-in keys may not use the ``ce-`` prefix, see ``lib.custom_provider.endpoints``).

The definition itself is the single source of truth; the spec and the mirror columns are both its projections, and both
dispatch by the definition's ``kind`` to that kind's projection implementation: the media type is always decided by the
definition, and the endpoint key implies no media type. The per-kind projection implementations are not in this module:
each of the two kinds has one, both in ``endpoints`` (where ``EndpointSpec`` and its invariants live), and the declarative
one is also shared with bundled definitions, since user and bundled definitions are the same thing; this module only does
"read the row from the DB → find the projection by kind". Loading is async (DB read) and construction is a pure function,
two clearly separate stages (``docs/adr/0039``); there is no full load into an in-memory registry at startup: in-place
definition updates must take effect for new tasks immediately.

按端点键分流的 spec 解析：内置查表，``ce-`` 前缀读库现构造。

``get_endpoint_spec`` 保持同步纯查表（内置注册表是 import 期就位的常量），需要同时看到两个
命名空间的调用方改走本模块的 :func:`resolve_endpoint_spec`。顺序完全由前缀决定，不存在「先自
定义后内置」的兜底——两个命名空间同域但永不重叠（内置键禁用 ``ce-`` 前缀，见
``lib.custom_provider.endpoints``）。

定义本体是唯一真相源，spec 与镜像列都是它的投影，两者都按定义的 ``kind`` 分派到该 kind 的投影
实现：媒体类型一律由定义决定，端点键不蕴含任何媒体类型。某一种 kind 的投影实现本身不在本模块——
两种 kind 各有一份，都落在 ``endpoints``（``EndpointSpec`` 与它的不变式在那里），声明式的那份
另与随版定义共用，用户定义与随版定义本就是同一种东西；本模块只管「读库取行 → 按 kind 找投影」。
装载走 async（读库）、构造是纯函数，两段分明
（``docs/adr/0039``）；不做启动时全量加载进内存注册表——定义原地更新须立即对新任务生效。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arcreel_market_core.endpoint_definition import COMFYUI_KIND, DECLARATIVE_KIND, definition_media_type
from lib.custom_provider import is_custom_endpoint, make_endpoint_key, parse_endpoint_key
from lib.custom_provider.endpoints import (
    EndpointSpec,
    comfyui_endpoint_spec,
    declarative_endpoint_spec,
    get_endpoint_spec,
)

if TYPE_CHECKING:
    from lib.db.models.custom_endpoint import CustomEndpoint


@dataclass(frozen=True)
class MirrorColumns:
    """Read-only mirror columns derived from the definition at write time, so the DB layer can filter and display without parsing JSON.

    写入时由定义派生的只读镜像列，供 DB 层不解析 JSON 就能过滤与展示。"""

    kind: str
    schema_version: str
    media_type: str
    display_name: str


#: ``kind`` → implementation that projects a definition into a spec. / ``kind`` → 把定义投影成 spec 的实现。
_SPEC_BY_KIND: Mapping[str, Callable[[str, Mapping[str, Any]], EndpointSpec]] = {
    DECLARATIVE_KIND: lambda key, definition: declarative_endpoint_spec(key, definition, source="custom"),
    COMFYUI_KIND: comfyui_endpoint_spec,
}


def _dispatch[T](table: Mapping[str, T], definition: Mapping[str, Any]) -> T:
    kind = str(definition["kind"])
    implementation = table.get(kind)
    if implementation is None:
        raise ValueError(f"unsupported endpoint definition kind: {kind!r}")
    return implementation


def derive_mirror_columns(definition: Mapping[str, Any]) -> MirrorColumns:
    """Derive mirror columns from an **already validated** definition.

    Validation guarantees ``kind`` / ``schema_version`` / ``meta.name`` exist and are strings, so values are taken directly here:
    deriving from an unvalidated definition would only write an incomplete structure into the DB.

    从一份**已过校验**的定义派生镜像列。

    校验保证了 ``kind`` / ``schema_version`` / ``meta.name`` 存在且是字符串，故此处直接取值：
    在未过校验的定义上派生只会把残缺结构写进库。
    """
    meta: Mapping[str, Any] = definition["meta"]
    return MirrorColumns(
        kind=str(definition["kind"]),
        schema_version=str(definition["schema_version"]),
        media_type=definition_media_type(definition),
        display_name=str(meta["name"]),
    )


def endpoint_spec_from_row(row: CustomEndpoint) -> EndpointSpec:
    """Build a spec from a DB row; the key is derived from the row's auto-increment id, and the source is marked ``custom``.

    Raises:
        ValueError: the row's definition ``kind`` has no projection implementation at this layer.

    按库里的行构造 spec；键由行的自增 id 派生，来源标为 ``custom``。

    Raises:
        ValueError: 行上定义的 ``kind`` 在本层没有投影实现。
    """
    definition: Mapping[str, Any] = row.definition
    return _dispatch(_SPEC_BY_KIND, definition)(make_endpoint_key(row.id), definition)


async def resolve_endpoint_spec(
    endpoint: str,
    get_custom_endpoint: Callable[[int], Awaitable[CustomEndpoint | None]],
) -> EndpointSpec:
    """Get the spec by endpoint key: ``ce-`` prefix reads the ``custom_endpoint`` table, everything else is delegated to the built-in lookup.

    Raises:
        ValueError: the key does not exist (not found in the built-in registry, or the custom endpoint was deleted)

    按端点键取 spec：``ce-`` 前缀读 ``custom_endpoint`` 表，其余委托内置查表。

    Raises:
        ValueError: 键不存在（内置注册表查无此键，或自定义端点已被删除）
    """
    if not is_custom_endpoint(endpoint):
        return get_endpoint_spec(endpoint)

    try:
        endpoint_id = parse_endpoint_key(endpoint)
    except ValueError:
        raise ValueError(f"unknown endpoint: {endpoint!r}") from None
    row = await get_custom_endpoint(endpoint_id)
    if row is None:
        raise ValueError(f"unknown endpoint: {endpoint!r}")
    return endpoint_spec_from_row(row)
