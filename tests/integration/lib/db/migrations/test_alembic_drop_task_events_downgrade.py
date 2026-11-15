"""Downgrade regression test for Alembic migration 3649100774fa (drop task_events).

This migration's downgrade must rebuild task_events, and the FK must have the same name as in the table-creating
migration (``fk_task_events_task_id``): the downgrade of the earlier migration b942e8c5d545 drops that constraint by
name, and if the names do not match the whole downgrade chain breaks there, which tests that only run ``upgrade
head`` cannot catch.

Alembic 迁移 3649100774fa（drop task_events）的降级回归测试。

该迁移的 downgrade 需重建 task_events，且 FK 必须与建表迁移同名
（``fk_task_events_task_id``）：更早一级迁移 b942e8c5d545 的 downgrade 按名 drop 该约束，
名字对不上时整条下行链断在那里，而只测 ``upgrade head`` 的用例发现不了。
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command

#: FK constraint name used by the table-creating migration b942e8c5d545; the downgrade rebuild must match it.
#: 建表迁移 b942e8c5d545 使用的 FK 约束名，downgrade 重建时必须一致。
_FK_NAME = "fk_task_events_task_id"

#: Parent revision of b942e8c5d545: downgrading to here crosses the step that drops the constraint by name.
#: b942e8c5d545 的父 revision——下行到这里就跨过了按名 drop 约束的那一步。
_BELOW_FK_MIGRATION = "ecbb53758daa"


def test_downgrade_rebuilds_task_events_with_named_fk(alembic_cfg: tuple[Config, Path]):
    """The task_events rebuilt by downgrade has a named FK, and can continue downgrading past the migration that drops the
    constraint by name.

    降级重建的 task_events 带具名 FK，且能继续下行跨过按名 drop 约束的迁移。"""
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        inspector = sa.inspect(engine)
        assert "task_events" not in inspector.get_table_names()

        command.downgrade(cfg, "b41d7c5e9a02")

        inspector = sa.inspect(engine)
        assert "task_events" in inspector.get_table_names()
        fk_names = {fk["name"] for fk in inspector.get_foreign_keys("task_events")}
        assert _FK_NAME in fk_names
        assert "idx_task_events_project_id" in {idx["name"] for idx in inspector.get_indexes("task_events")}
    finally:
        engine.dispose()

    # Continue downgrading past b942e8c5d545 (drops that FK by name); if the names do not match this raises
    # ValueError: No such constraint.
    # 继续下行跨过 b942e8c5d545（按名 drop 该 FK）——名字对不上时这里抛
    # ValueError: No such constraint。
    command.downgrade(cfg, _BELOW_FK_MIGRATION)

    # Upgrading to head again should not error because of leftovers from the rebuilt table
    # 重新升到 head 不应因重建过的表残留报错
    command.upgrade(cfg, "head")
