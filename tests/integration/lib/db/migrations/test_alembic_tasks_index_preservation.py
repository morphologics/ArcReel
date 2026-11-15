"""Regression for index survival of table-rebuilding downgrades of the tasks table on SQLite.

SQLAlchemy cannot reflect an expression-based partial unique index like ``idx_tasks_dedupe_active``, so any downgrade
that rebuilds the table via batch may silently drop it; losing it disables the dedupe gate, and two active tasks can
run concurrently for the same resource. Gate it from two angles: paired assertions per rebuilding migration
(pinpointing the exact migration), and a head->base walk of the whole chain that does not depend on the enumeration
(goes red when a new migration introduces the same defect).

SQLite 上 tasks 表重建型 downgrade 的索引存活回归。

SQLAlchemy 反射不出 ``idx_tasks_dedupe_active`` 这种表达式型 partial unique 索引，凡是走
batch 重建表的 downgrade 都可能把它静默丢掉——丢了等于去重闸失效，同一资源可并发起两个活动
任务。这里从两个角度上闸：逐个重建型迁移的成对断言（定位到具体迁移），以及一条不依赖枚举的
head→base 全链走查（新迁移引入同类缺陷时也会红）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[5]
DEDUPE_INDEX = "idx_tasks_dedupe_active"

# tasks indexes each migration's downgrade drops explicitly: their disappearance is an expected rollback, distinct from
# silent loss by table rebuild.
# 各迁移 downgrade 显式 drop 的 tasks 索引：消失属预期回滚，与重建表的静默丢失区分开。
INTENTIONAL_TASKS_INDEX_DROPS = {
    "26c2f9d11c8e": frozenset({"ix_tasks_batch_id"}),
    "285dbe1e9824": frozenset({"idx_tasks_status_provider_queued"}),
    "ea2e1a477bbf": frozenset({"ix_tasks_user_id"}),
}

# Migrations whose downgrade rebuilds the tasks table via batch.
# downgrade 走 batch 重建 tasks 表的迁移。
REBUILD_MIGRATIONS = [
    "26c2f9d11c8e",
    "d4f8b1c73a20",
    "c4a91f7d2b18",
    "285dbe1e9824",
    "548f6ca3e91c",
    "ea2e1a477bbf",
]

# initial schema: its downgrade drops the tasks table itself, so the full-chain walk stops here.
# initial schema，其 downgrade 删掉 tasks 表本身，全链走查到此为止。
INITIAL_SCHEMA_REVISION = "156fe0aa0414"


def _tasks_indexes(db_path: Path) -> set[str] | None:
    """Index names on the tasks table; None when the table does not exist.

    tasks 表上的索引名；表不存在时返回 None。"""
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        exists = conn.execute(sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'")).first()
        rows = conn.execute(sa.text("PRAGMA index_list('tasks')")).fetchall() if exists else []
    engine.dispose()
    return {r[1] for r in rows} if exists else None


@pytest.mark.parametrize("revision", REBUILD_MIGRATIONS)
def test_downgrade_keeps_indexes(alembic_cfg, revision):
    """After one downgrade step, none of the indexes from before the downgrade is missing, except those the migration
    itself drops explicitly.

    Step down from head to the migration under test instead of upgrading from base: the upgrade path also drops the
    dedupe index at ``ea2e1a477bbf`` and only rebuilds it at ``a3f1c9b27e54``, so when upgrading from base it does not
    exist on early migrations at all and the assertion would be empty. Switching back to upgrading from base would
    silently make these tests meaningless.

    降级一步后，除该迁移自身显式删除的索引外，降级前的索引一个不少。

    从 head 逐级降到被测迁移，而不是从 base 升上来：升级路径在 ``ea2e1a477bbf`` 处同样会丢掉
    去重索引、直到 ``a3f1c9b27e54`` 才重建，从 base 升起来时它在早期迁移上根本不存在，断言会
    是空的。改回从 base 升会让这些用例静默失去意义。
    """
    cfg, db_path = alembic_cfg
    down_revision = ScriptDirectory.from_config(cfg).get_revision(revision).down_revision
    command.upgrade(cfg, "head")
    command.downgrade(cfg, revision)

    before = _tasks_indexes(db_path)
    assert before is not None
    assert DEDUPE_INDEX in before

    command.downgrade(cfg, str(down_revision))
    after = _tasks_indexes(db_path)
    assert after is not None

    assert before - after == INTENTIONAL_TASKS_INDEX_DROPS.get(revision, frozenset())


def test_full_chain_downgrade_only_drops_intentional_indexes(alembic_cfg):
    """Step down from head to base; each step drops only the tasks indexes that migration drops explicitly.

    Does not depend on ``REBUILD_MIGRATIONS`` being complete: a new migration introducing the same pattern goes red here
    even if not registered in the list above. The dedupe index survives all the way to the initial schema that drops the
    table.

    head 逐级降到 base，每步只丢该迁移显式删除的 tasks 索引。

    不依赖 ``REBUILD_MIGRATIONS`` 的枚举完备性：新迁移若引入同一模式，即便没被登记进上面的
    列表也会在这里红。去重索引一路存活到删表的 initial schema 为止。
    """
    cfg, db_path = alembic_cfg
    script = ScriptDirectory.from_config(cfg)
    command.upgrade(cfg, "head")

    before = _tasks_indexes(db_path)
    assert before is not None
    assert DEDUPE_INDEX in before

    for revision in (s.revision for s in script.walk_revisions("base", "heads")):
        down_revision = script.get_revision(revision).down_revision or "base"
        command.downgrade(cfg, str(down_revision))
        after = _tasks_indexes(db_path)
        if after is None:
            assert revision == INITIAL_SCHEMA_REVISION
            assert DEDUPE_INDEX in before
            return

        assert before - after == INTENTIONAL_TASKS_INDEX_DROPS.get(revision, frozenset()), (
            f"{revision} 降级丢失了未登记的 tasks 索引"
        )
        before = after

    pytest.fail(f"降到 base 也没走到 {INITIAL_SCHEMA_REVISION} 的删表步骤")
