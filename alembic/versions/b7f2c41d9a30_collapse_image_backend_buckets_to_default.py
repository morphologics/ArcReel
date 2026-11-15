"""collapse image backend buckets into the default layer

Revision ID: b7f2c41d9a30
Revises: d10f1df40f96
Create Date: 2026-08-02 20:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeGuard

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f2c41d9a30"
down_revision: str | Sequence[str] | None = "d10f1df40f96"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_KEY = "default_image_backend"
_BUCKET_KEYS = ("default_image_backend_t2i", "default_image_backend_i2i")


def _read(bind: sa.Connection, key: str) -> str | None:
    row = bind.execute(sa.text("SELECT value FROM system_setting WHERE key = :k").bindparams(k=key)).fetchone()
    return None if row is None else row[0]


def _write(bind: sa.Connection, key: str, value: str) -> None:
    updated = bind.execute(
        sa.text("UPDATE system_setting SET value = :v, updated_at = CURRENT_TIMESTAMP WHERE key = :k").bindparams(
            k=key, v=value
        )
    ).rowcount
    if not updated:
        bind.execute(
            sa.text(
                "INSERT INTO system_setting (key, value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)"
            ).bindparams(k=key, v=value)
        )


def _delete(bind: sa.Connection, key: str) -> None:
    bind.execute(sa.text("DELETE FROM system_setting WHERE key = :k").bindparams(k=key))


def _configured(value: str | None) -> TypeGuard[str]:
    """A valid configured value looks like `provider/model`; an empty string or a leftover value without a slash is not a configuration.

    有效配置值 = 形如 ``provider/model``；空串与无斜杠的残值都不构成配置。
    """
    return value is not None and "/" in value


def upgrade() -> None:
    """Collapse image t2i / i2i from forced slots into optional override buckets, and promote the default layer to default_image_backend.

        Existing key combinations are handled one by one (`docs/adr/0054`):

        - Invalid bucket value (empty string / no slash): delete it. Under the old semantics it meant "explicitly cleared → follow auto inference"; under the new semantics
          a bucket with no value falls back to the default layer, so after deletion the two have the same shape, and the config surface no longer leaves an empty bucket that could be misread as "configured".
        - Both buckets have the same value and the default layer is not configured: promote the bucket value to the default layer and delete both buckets. A user who had configured only one image model
          before the split thereby returns to the canonical "default only" shape.
        - Both buckets have the same value, equal to the default layer: delete both buckets. This is a redundant copy made by the split migration.
        - Everything else (the two buckets differ, or have the same value but the default layer is configured differently): keep as is, and the buckets continue to take effect as overrides.

    把图片 t2i / i2i 从强制槽位收敛为可选覆盖桶，默认层升为 default_image_backend。

    存量键组合逐一处置（``docs/adr/0054``）：

    - 桶值无效（空串 / 无斜杠）：删除。旧语义下它表示「显式清空 → 跟随自动推断」，新语义
      下桶无值即回退默认层——删除后两者同形，配置面不再留下会被误读为「已配置」的空桶。
    - 两桶同值且默认层未配置：桶值升为默认层、删除两桶。拆分前只配过一个图片模型的用户
      由此回到「只配默认」的规范形态。
    - 两桶同值且与默认层相同：删除两桶。这是拆分迁移复制出的冗余副本。
    - 其余（两桶取值不同，或同值但默认层另有配置）：原样保留，桶继续作为覆盖生效。
    """
    bind = op.get_bind()
    default = _read(bind, _DEFAULT_KEY)
    buckets = {key: _read(bind, key) for key in _BUCKET_KEYS}

    for key, value in buckets.items():
        if value is not None and not _configured(value):
            _delete(bind, key)
            buckets[key] = None

    t2i, i2i = (buckets[key] for key in _BUCKET_KEYS)
    if not _configured(t2i) or t2i != i2i:
        return
    if not _configured(default):
        _write(bind, _DEFAULT_KEY, t2i)
    elif default != t2i:
        return
    for key in _BUCKET_KEYS:
        _delete(bind, key)


def downgrade() -> None:
    """Restore the "bucket is authoritative" shape: when the default layer is configured and both buckets have no valid value, copy the default value back to both buckets.

        First normalize invalid bucket rows by the same rule as upgrade, then judge: clearing a bucket in the config surface writes an empty-string row rather than deleting the row
        (see the backend key write in `server/routers/system_config.py`). If judged by "row exists means valid override", after downgrade the old semantics
        would treat this empty bucket as authoritative and skip the default layer, so the capability would silently drop from the user's configured default layer to auto inference.

        Invalid bucket values that were deleted cannot be restored one by one (under the old semantics "explicitly cleared" and "never configured" have the same shape after downgrade),
        so after downgrade such configurations are treated as unconfigured.

    还原「桶即权威」形态：默认层有配置而两桶皆无有效值时，把默认值复制回两桶。

    先按 upgrade 的同一口径规范化无效桶行再判断：配置面清空某个桶写入的是空串行而非删行
    （见 ``server/routers/system_config.py`` 的 backend 键写入），若按「行存在即有效覆盖」判定，
    降级后旧语义会把这个空桶当权威、跳过默认层，使该能力从用户配置的默认层静默落到自动推断。

    无法逐条还原被删除的无效桶值（旧语义的「显式清空」与「从未配置」在降级后同形），
    降级后这类配置按未配置处理。
    """
    bind = op.get_bind()
    default = _read(bind, _DEFAULT_KEY)
    if not _configured(default):
        return
    for key in _BUCKET_KEYS:
        if not _configured(_read(bind, key)):
            _delete(bind, key)
    if any(_read(bind, key) is not None for key in _BUCKET_KEYS):
        return
    for key in _BUCKET_KEYS:
        _write(bind, key, default)
