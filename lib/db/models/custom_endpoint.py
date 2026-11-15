"""Custom call endpoint ORM model. / 自定义调用端点 ORM model。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin


class CustomEndpoint(TimestampMixin, Base):
    """A call endpoint definition imported or created manually by the user.

    The whole ``definition`` JSON is the single source of truth; the other columns are read-only mirrors derived from the
    definition on write, letting the DB layer filter and sort without parsing JSON (``docs/adr/0067``). Mirror columns
    cannot be edited independently: renaming means changing ``meta.name``, changing the protocol means changing the definition itself.

    Global entity without a user dimension (like :class:`~lib.db.models.custom_provider.CustomProvider`).
    ``display_name`` has no unique constraint: duplicate imports are a hint, not a rejection; lineage is decided by
    ``meta.author + meta.name`` and handled by the client.

    用户导入或手工创建的调用端点定义。

    ``definition`` 整份 JSON 是唯一真相源；其余列都是写入时由定义派生的只读镜像，供 DB 层
    在不解析 JSON 的前提下过滤与排序（``docs/adr/0067``）。镜像列不接受独立编辑——改名即改
    ``meta.name``，改协议即改定义本体。

    全局实体，无 user 维度（与 :class:`~lib.db.models.custom_provider.CustomProvider` 一致）。
    ``display_name`` 不加唯一约束：重复导入是提示而非拒绝，血统按 ``meta.author + meta.name``
    判定并由客户端处置。
    """

    __tablename__ = "custom_endpoint"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Container type, from definition.kind (declarative / comfyui; Python plugins are a reserved slot for a separate topic)
    # 容器类型，取自 definition.kind（declarative / comfyui；Python 插件是独立议题的预留槽位）
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Format version the definition follows; the file's value is kept as is, never rewritten or migrated / 定义遵循的格式版本，原样保留文件里的值、不改写，也不做迁移
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # From the definition's media_type; "video" when a declarative definition omits it / 取自定义里的 media_type；声明式定义不写时为 "video"
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)  # ← definition.meta.name
