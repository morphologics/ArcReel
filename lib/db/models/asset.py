"""Asset ORM: entries of the global asset library. / Asset ORM: 全局资产库条目。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("type", "name", name="uq_asset_type_name"),
        Index("ix_asset_type", "type"),
        Index("ix_asset_name", "name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # character/scene/prop
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    voice_style: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_project: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AssetDerivative(TimestampMixin, Base):
    """Derivatives attached to a character asset in the global asset library (see ``docs/adr/0072``).

    Derivatives move in and out of the asset library together with the base asset: ``from-project`` writes the
    derivative table of the character entry together with sheets into this table, and ``apply-to-project`` writes them
    back into the target project's character entry and to disk. Names are unique only within the owning asset, the same
    rule as the project's derivative table.

    全局资产库里挂在一条角色资产下的衍生（见 ``docs/adr/0072``）。

    衍生随本体资产整套进出资产库：``from-project`` 把角色条目里的衍生表连同资产图一起
    写进本表，``apply-to-project`` 再把它们写回目标项目的角色条目并落盘。名字只在所属
    资产内唯一，与项目内的衍生表同口径。
    """

    __tablename__ = "asset_derivatives"
    __table_args__ = (
        UniqueConstraint("asset_id", "name", name="uq_asset_derivative_asset_name"),
        Index("ix_asset_derivative_asset_id", "asset_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
