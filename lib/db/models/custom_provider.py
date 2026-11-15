"""Custom provider ORM models."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, TimestampMixin


class CustomProvider(TimestampMixin, Base):
    """User-defined AI provider. / 用户自定义的 AI 供应商。"""

    __tablename__ = "custom_provider"
    # Kept in sync with the add-column migration: the three concurrency limit columns are forced ≥1 at the DB level, so
    # neither direct repo writes nor manual SQL can write 0 or negatives; NULL = unset, fall back to default. 0 is not a
    # valid user input, only an internal CapacityTable "lane not supported" sentinel (produced in memory by the lane projection, never written back to these columns).
    # 与加列迁移保持同步：三条并发上限列在 DB 层强制 ≥1，repo 直写或手工 SQL 都无法写入
    # 0 或负值；NULL=未设置回退默认。0 不是合法用户输入，仅作 CapacityTable 内部「不支持该
    # lane」哨兵（由 lane 投影在内存里产生，绝不写回这些列）。
    __table_args__ = (
        CheckConstraint(
            "image_max_workers IS NULL OR image_max_workers >= 1",
            name="ck_custom_provider_image_max_workers_positive",
        ),
        CheckConstraint(
            "video_max_workers IS NULL OR video_max_workers >= 1",
            name="ck_custom_provider_video_max_workers_positive",
        ),
        CheckConstraint(
            "audio_max_workers IS NULL OR audio_max_workers >= 1",
            name="ck_custom_provider_audio_max_workers_positive",
        ),
        # Deleted provider IDs are not recycled: provider_id (custom-<id>) lands in failure records and usage, and reuse would misattribute
        # 删除的供应商 ID 不回收：provider_id（custom-<id>）落在失败记录与用量里，复用会错归
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    discovery_format: Mapped[str] = mapped_column(String(32), nullable=False)  # "openai" | "google"
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)  # sensitive, masked in API responses
    # Typed concurrency limit columns named by lane; NULL = unset -> capacity loading falls back to the global default.
    # Custom providers are not in the built-in registry, so there is no declared default layer; fallback is two layers (user column value -> global default).
    # 按 lane 命名的并发上限定型列；NULL = 未设置 → 容量装载回退全局默认。自定义供应商不在
    # 内置注册表，故无声明默认层，回退为两层（用户列值 → 全局默认）。
    image_max_workers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_max_workers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_max_workers: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def provider_id(self) -> str:
        from lib.custom_provider import make_provider_id

        return make_provider_id(self.id)


class CustomProviderModel(TimestampMixin, Base):
    """Model configuration under a custom provider. / 自定义供应商下的模型配置。"""

    __tablename__ = "custom_provider_model"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_id", name="uq_custom_provider_model"),
        Index("ix_custom_provider_model_provider_id", "provider_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("custom_provider.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(32), nullable=False)  # ENDPOINT_REGISTRY key
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    resolution: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # standard token ("1080p"/"2K") or native "WxH"
    # Model-level capability overrides, sparse dict, key names aligned with VideoCapabilities field names. NULL or key missing =
    # follow the system verdict; no hard-coded list of capability dimensions in the schema, open to new dimensions without
    # migrations. Merge semantics live solely in lib.custom_provider.capabilities.
    # 模型级能力覆盖，稀疏字典，键名对齐 VideoCapabilities 字段名。NULL 或字典缺该键 =
    # 跟随系统判定；写死的能力维度列表不进 schema，向新维度开放无需迁移。合成语义由
    # lib.custom_provider.capabilities 唯一承载。
    capability_overrides: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    # Path overrides: call endpoint request name -> full replacement path (docs/adr/0101). NULL = all default endpoint paths;
    # cleared by the writer when the call endpoint changes; request names no longer in the endpoint definition are ignored on read and execution.
    # 路径覆盖：调用端点的请求名 → 整条替换的路径（docs/adr/0101）。NULL = 全部按端点默认路径；
    # 更换调用端点时由写入侧清空，端点定义里已不存在的请求名在读取与执行时忽略。
    path_overrides: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    # Provider model multiplier (docs/adr/0100): multiplied with the provider multiplier and applied to this model's whole reference cost; default 1.
    # 供应商模型倍率（docs/adr/0100）：与供应商倍率相乘后作用在这个模型的整笔参考费用上；默认 1。
    multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default=sa_text("1"))
    # Entry binding (docs/adr/0105): the model entry the user chose for this model, stored as "model vendor/official model name", media type from the call endpoint.
    # NULL = look up by name; when the bound entry is no longer in the catalogue, also fall back to lookup by name.
    # 条目绑定（docs/adr/0105）：用户为这个模型指定的模型条目，存「模型厂商/官方模型名」，媒体类型取调用端点。
    # NULL = 按名字现查；绑定的条目已不在目录里时同样回到按名字现查。
    entry_binding: Mapped[str | None] = mapped_column(String(255), nullable=True)
