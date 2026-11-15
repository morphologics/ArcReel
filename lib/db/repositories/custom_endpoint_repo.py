"""Custom call endpoint repository: CRUD of the definitions and queries of model rows referencing them. / 自定义调用端点仓储：定义本体 CRUD 与模型行引用查询。"""

from __future__ import annotations

from typing import Any, NamedTuple

from sqlalchemy import delete, select

from lib.db.models.custom_endpoint import CustomEndpoint
from lib.db.models.custom_provider import CustomProvider, CustomProviderModel
from lib.db.models.market_installation import MarketInstallation
from lib.db.repositories.base import BaseRepository


class EndpointReference(NamedTuple):
    """A model row referencing this call endpoint, together with its provider. / 一条引用该调用端点的模型行，连同它所属的供应商。"""

    provider_id: int
    provider_display_name: str
    model_id: str
    model_display_name: str


class EndpointAttachment(NamedTuple):
    """A model row referencing this call endpoint, together with the model discovery protocol declared by its provider.

    Kept separate from :class:`EndpointReference`: that one is the reference list shown to users, this one is the fact
    used to judge the "endpoint × provider protocol" pairing; adding a protocol field there would change the public shape of the 409 response.

    一条引用该调用端点的模型行，连同它所属供应商声明的模型发现协议。

    与 :class:`EndpointReference` 分开：那一条是给用户看的引用清单，这一条是判「端点 × 供应商
    协议」配对用的事实，多带一个协议字段就得改动 409 响应的公开形状。
    """

    provider_id: int
    model_id: str
    discovery_format: str


class CustomEndpointRepository(BaseRepository):
    """Reads and writes of the ``custom_endpoint`` table.

    Mirror columns (``kind`` / ``schema_version`` / ``media_type`` / ``display_name``) are derived from ``definition`` by
    the caller before writing and passed in together; this layer does not parse definitions: derivation rules belong to
    the format layer in ``lib.custom_provider.endpoint_resolution``, and the repository only persists.

    ``custom_endpoint`` 表的读写。

    镜像列（``kind`` / ``schema_version`` / ``media_type`` / ``display_name``）由调用方在写入
    前从 ``definition`` 派生后一并传入，本层不解析定义：派生规则属格式层，落在
    ``lib.custom_provider.endpoint_resolution``，仓储只负责持久化。
    """

    async def create(
        self,
        *,
        definition: dict[str, Any],
        kind: str,
        schema_version: str,
        media_type: str,
        display_name: str,
    ) -> CustomEndpoint:
        endpoint = CustomEndpoint(
            definition=definition,
            kind=kind,
            schema_version=schema_version,
            media_type=media_type,
            display_name=display_name,
        )
        self.session.add(endpoint)
        await self.session.flush()  # fetch the auto-increment id: the endpoint key ce-<id> derives from it / 取回自增 id：端点键 ce-<id> 由它派生
        return endpoint

    async def get(self, endpoint_id: int) -> CustomEndpoint | None:
        stmt = select(CustomEndpoint).where(CustomEndpoint.id == endpoint_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[CustomEndpoint]:
        stmt = select(CustomEndpoint).order_by(CustomEndpoint.id)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def update(
        self,
        endpoint_id: int,
        *,
        definition: dict[str, Any],
        kind: str,
        schema_version: str,
        media_type: str,
        display_name: str,
    ) -> CustomEndpoint | None:
        """Replace the definition and mirror columns in place, keeping the id (i.e. the endpoint key) and model row attachments. Returns None if not found.

        原地整份替换定义与镜像列，保留 id（即端点键）与模型行挂接。不存在时返回 None。"""
        endpoint = await self.get(endpoint_id)
        if endpoint is None:
            return None
        endpoint.definition = definition
        endpoint.kind = kind
        endpoint.schema_version = schema_version
        endpoint.media_type = media_type
        endpoint.display_name = display_name
        await self.session.flush()
        return endpoint

    async def delete(self, endpoint_id: int) -> None:
        """Delete together with installation records.

        Installation records are deleted explicitly instead of relying on FK CASCADE: cascade only works on connections with
        the foreign_keys pragma on, and leftover records would make the same market entry impossible to install again.

        连同安装记录一起删除。

        显式删除安装记录而非依赖 FK CASCADE：级联只在开启 foreign_keys pragma 的连接上生效，
        残留记录会让同一市场条目再也装不上。
        """
        await self.session.execute(
            delete(MarketInstallation).where(MarketInstallation.custom_endpoint_id == endpoint_id)
        )
        await self.session.execute(delete(CustomEndpoint).where(CustomEndpoint.id == endpoint_id))
        await self.session.flush()

    # ── Referential integrity ──────────────────────────────────────────────── / ── 引用完整性 ────────────────────────────────────────────────

    async def list_attachments(self, endpoint_key: str) -> list[EndpointAttachment]:
        """List model rows referencing the literal ``endpoint`` key, with their provider protocols.

        Used when replacing a whole definition to judge whether the new container type still fits; the pairing rules live in
        ``lib.custom_provider.discovery_formats``; this layer only fetches facts.

        按 ``endpoint`` 键字面量列出引用它的模型行及其供应商协议。

        整份替换定义时用来判断新的容器类型还挂不挂得住：配对规则本身在
        ``lib.custom_provider.discovery_formats``，本层只取事实。
        """
        stmt = (
            select(
                CustomProvider.id,
                CustomProviderModel.model_id,
                CustomProvider.discovery_format,
            )
            .join(CustomProvider, CustomProvider.id == CustomProviderModel.provider_id)
            .where(CustomProviderModel.endpoint == endpoint_key)
            .order_by(CustomProviderModel.provider_id, CustomProviderModel.id)
        )
        result = await self.session.execute(stmt)
        return [EndpointAttachment(*row) for row in result.all()]

    async def list_references(self, endpoint_key: str) -> list[EndpointReference]:
        """List model rows referencing the literal ``endpoint`` key, for the 409 response to say "who still uses it".

        Referential integrity relies on this query rather than an FK: the 409 must return the reference list, an FK
        constraint can only raise an IntegrityError, and the delete path has to look up references first anyway (docs/adr/0067).

        按 ``endpoint`` 键字面量列出引用它的模型行，供 409 响应说明「谁还在用」。

        引用完整性靠本查询而非 FK：409 要回引用清单，FK 约束只能抛一个 IntegrityError，
        而删除路径本就要先查引用（docs/adr/0067）。
        """
        stmt = (
            select(
                CustomProvider.id,
                CustomProvider.display_name,
                CustomProviderModel.model_id,
                CustomProviderModel.display_name,
            )
            .join(CustomProvider, CustomProvider.id == CustomProviderModel.provider_id)
            .where(CustomProviderModel.endpoint == endpoint_key)
            .order_by(CustomProviderModel.provider_id, CustomProviderModel.id)
        )
        result = await self.session.execute(stmt)
        return [EndpointReference(*row) for row in result.all()]
