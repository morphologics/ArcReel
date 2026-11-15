"""Tests for the Alembic collapse_image_backend_buckets_to_default migration: exhaustive existing key combinations.

After image t2i / i2i went from mandatory slots to optional override buckets, the default layer
default_image_backend became user-visible config. This file enumerates existing combinations by the value shapes of
"default layer x t2i x i2i", pinning the key state and resolution result after migration.

Alembic collapse_image_backend_buckets_to_default 迁移测试：存量键组合穷举。

图片 t2i / i2i 由强制槽位降级为可选覆盖桶后，默认层 default_image_backend 升为用户可见
配置。本文件按「默认层 × t2i × i2i」的取值形态穷举存量组合，锁定迁移后的键状态与解析结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic import command
from lib.config.resolver import _IMAGE_LAYERED_KEYS, ConfigResolver

PROJECT_ROOT = Path(__file__).resolve().parents[5]

_PREV_REVISION = "d10f1df40f96"
_KEYS = ("default_image_backend", "default_image_backend_t2i", "default_image_backend_i2i")


def _sync_engine(db_path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{db_path}")


def _seed(engine: sa.Engine, settings: dict[str, str]) -> None:
    if not settings:
        return
    with engine.begin() as conn:
        for key, value in settings.items():
            conn.execute(
                sa.text(
                    "INSERT INTO system_setting (key, value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)"
                ).bindparams(k=key, v=value)
            )


def _read_image_settings(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT key, value FROM system_setting WHERE key IN :keys").bindparams(
                sa.bindparam("keys", value=_KEYS, expanding=True)
            )
        ).fetchall()
    return {row.key: row.value for row in rows}


# (case name, key state before migration, key state after migration)
#
# Semantic changes only happen in the two rows with "bucket has no valid value + default layer configured"
# (empty_buckets_with_default, and the i2i side of mixed_empty_bucket): before migration an empty bucket meant
# "follow auto-inference", after migration it falls back to the default layer. All other rows resolve the same before
# and after; the migration only normalizes key shape.
# (用例名, 迁移前键状态, 迁移后键状态)
#
# 语义变更只发生在「桶无有效值 + 默认层有配置」的两行（empty_buckets_with_default、
# mixed_empty_bucket 的 i2i 侧）：迁移前空桶表示「跟随自动推断」，迁移后回退默认层。
# 其余各行迁移前后解析结果一致，迁移只做键形态归一。
_CASES = [
    ("all_absent", {}, {}),
    (
        "default_only",
        {"default_image_backend": "grok/grok-2-image"},
        {"default_image_backend": "grok/grok-2-image"},
    ),
    (
        "equal_buckets_without_default",
        {"default_image_backend_t2i": "openai/gpt-image-2", "default_image_backend_i2i": "openai/gpt-image-2"},
        {"default_image_backend": "openai/gpt-image-2"},
    ),
    (
        "equal_buckets_matching_default",
        {
            "default_image_backend": "openai/gpt-image-2",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "openai/gpt-image-2",
        },
        {"default_image_backend": "openai/gpt-image-2"},
    ),
    (
        "equal_buckets_diverging_from_default",
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "openai/gpt-image-2",
        },
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "openai/gpt-image-2",
        },
    ),
    (
        "distinct_buckets_without_default",
        {"default_image_backend_t2i": "openai/gpt-image-2", "default_image_backend_i2i": "ark/kolors-img2img"},
        {"default_image_backend_t2i": "openai/gpt-image-2", "default_image_backend_i2i": "ark/kolors-img2img"},
    ),
    (
        "distinct_buckets_with_default",
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "ark/kolors-img2img",
        },
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "ark/kolors-img2img",
        },
    ),
    (
        "empty_buckets_with_default",
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "",
            "default_image_backend_i2i": "",
        },
        {"default_image_backend": "grok/grok-2-image"},
    ),
    (
        "empty_buckets_without_default",
        {"default_image_backend_t2i": "", "default_image_backend_i2i": ""},
        {},
    ),
    (
        "mixed_empty_bucket",
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "",
        },
        {"default_image_backend": "grok/grok-2-image", "default_image_backend_t2i": "openai/gpt-image-2"},
    ),
    (
        "empty_default_with_equal_buckets",
        {
            "default_image_backend": "",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "openai/gpt-image-2",
        },
        {"default_image_backend": "openai/gpt-image-2"},
    ),
    (
        "bucket_without_slash_dropped",
        {"default_image_backend": "grok/grok-2-image", "default_image_backend_t2i": "openai"},
        {"default_image_backend": "grok/grok-2-image"},
    ),
]


@pytest.mark.parametrize(("name", "before", "after"), _CASES, ids=[case[0] for case in _CASES])
def test_upgrade_exhausts_legacy_key_combinations(alembic_cfg, name, before, after):
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, _PREV_REVISION)
    engine = _sync_engine(db_path)
    _seed(engine, before)

    command.upgrade(cfg, "head")

    assert _read_image_settings(engine) == after


# Effective model per combination after migration (t2i, i2i); None = no config to land on, left to auto-inference.
# 迁移后各组合的生效模型（t2i, i2i）；None = 无配置可落、留给自动推断。
_RESOLVED_AFTER: dict[str, tuple[str | None, str | None]] = {
    "all_absent": (None, None),
    "default_only": ("grok/grok-2-image", "grok/grok-2-image"),
    "equal_buckets_without_default": ("openai/gpt-image-2", "openai/gpt-image-2"),
    "equal_buckets_matching_default": ("openai/gpt-image-2", "openai/gpt-image-2"),
    "equal_buckets_diverging_from_default": ("openai/gpt-image-2", "openai/gpt-image-2"),
    "distinct_buckets_without_default": ("openai/gpt-image-2", "ark/kolors-img2img"),
    "distinct_buckets_with_default": ("openai/gpt-image-2", "ark/kolors-img2img"),
    # Semantic-change row: before migration the empty bucket followed auto-inference, after migration it lands on the
    # default layer
    # 语义变更行：迁移前空桶跟随自动推断，迁移后落默认层
    "empty_buckets_with_default": ("grok/grok-2-image", "grok/grok-2-image"),
    "empty_buckets_without_default": (None, None),
    # Semantic-change row: same on the i2i side
    # 语义变更行：i2i 侧同上
    "mixed_empty_bucket": ("openai/gpt-image-2", "grok/grok-2-image"),
    "empty_default_with_equal_buckets": ("openai/gpt-image-2", "openai/gpt-image-2"),
    "bucket_without_slash_dropped": ("grok/grok-2-image", "grok/grok-2-image"),
}


@pytest.mark.parametrize(("name", "before", "after"), _CASES, ids=[case[0] for case in _CASES])
async def test_resolution_after_upgrade(name, before, after):
    """Evaluate the migration output with the real resolution skeleton, pinning each combination's effective model after
    upgrade.

    迁移产物经真实解析骨架求值，锁定各组合升级后的生效模型。"""
    resolver = ConfigResolver.__new__(ConfigResolver)

    class _Settings:
        async def get_all_settings(self) -> dict[str, str]:
            return dict(after)

    for generation_type, expected in zip(("t2i", "i2i"), _RESOLVED_AFTER[name], strict=True):
        if expected is None:
            # No config to land on: resolution must go to auto-inference (needs a real provider table, not evaluated here); assert
            # its precondition, that after migration neither the bucket nor the default layer has a valid value
            # 无配置可落：解析必然走自动推断（需真实供应商表，此处不求值），
            # 断言其前提——迁移后该桶与默认层都无有效值
            assert not any("/" in after.get(key, "") for key in (f"default_image_backend_{generation_type}", _KEYS[0]))
            continue
        resolved = await resolver._resolve_layered_backend(
            _Settings(),
            None,
            None,
            _IMAGE_LAYERED_KEYS[generation_type],
        )
        assert resolved == tuple(expected.split("/", 1))


def test_downgrade_restores_buckets_from_default(alembic_cfg):
    """Downgrade copies the default layer back into both buckets, restoring the "bucket is authoritative" shape.

    降级把默认层复制回两桶，还原「桶即权威」形态。"""
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, "head")
    engine = _sync_engine(db_path)
    _seed(engine, {"default_image_backend": "grok/grok-2-image"})

    command.downgrade(cfg, _PREV_REVISION)

    assert _read_image_settings(engine) == {
        "default_image_backend": "grok/grok-2-image",
        "default_image_backend_t2i": "grok/grok-2-image",
        "default_image_backend_i2i": "grok/grok-2-image",
    }


def test_downgrade_restores_default_over_emptied_bucket(alembic_cfg):
    """A bucket cleared through the config UI after upgrade (leaving an empty-string row) does not count as a valid
    override on downgrade, so the default layer is still restored.

    Otherwise the old semantics would treat this empty bucket as authoritative and skip the default layer, and that
    capability would silently land on auto-inference.

    升级后经配置面清空的桶（留下空串行）在降级时不算有效覆盖，仍还原默认层。

    否则旧语义会把这个空桶当权威、跳过默认层，该能力静默落到自动推断。
    """
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, "head")
    engine = _sync_engine(db_path)
    _seed(
        engine,
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "",
        },
    )

    command.downgrade(cfg, _PREV_REVISION)

    assert _read_image_settings(engine) == {
        "default_image_backend": "grok/grok-2-image",
        "default_image_backend_t2i": "grok/grok-2-image",
        "default_image_backend_i2i": "grok/grok-2-image",
    }


def test_downgrade_keeps_existing_buckets(alembic_cfg):
    """Downgrade does not overwrite existing bucket config.

    已有桶配置时降级不覆盖。"""
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, "head")
    engine = _sync_engine(db_path)
    _seed(
        engine,
        {
            "default_image_backend": "grok/grok-2-image",
            "default_image_backend_t2i": "openai/gpt-image-2",
            "default_image_backend_i2i": "ark/kolors-img2img",
        },
    )

    command.downgrade(cfg, _PREV_REVISION)

    assert _read_image_settings(engine) == {
        "default_image_backend": "grok/grok-2-image",
        "default_image_backend_t2i": "openai/gpt-image-2",
        "default_image_backend_i2i": "ark/kolors-img2img",
    }
