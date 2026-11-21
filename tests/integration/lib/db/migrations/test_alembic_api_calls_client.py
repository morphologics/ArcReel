"""Up/down migration tests for the api_calls.client column.

api_calls.client 列的双向迁移测试。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from alembic import command


def _columns(db_path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = conn.execute(sa.text("PRAGMA table_info(api_calls)")).fetchall()
    engine.dispose()
    return {r[1] for r in rows}


def _indexes(db_path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = conn.execute(sa.text("PRAGMA index_list('api_calls')")).fetchall()
    engine.dispose()
    return {r[1] for r in rows}


def test_upgrade_adds_a_nullable_client_and_old_rows_have_none(alembic_cfg, migration_revisions):
    """Rows written before the column existed have no client: NULL, not a guessed "webui".

    该列出现之前写入的行没有客户端：为 NULL，而不是猜一个 "webui"。"""
    cfg, db_path = alembic_cfg
    revision, down_revision = migration_revisions("*_add_client_to_api_calls.py")
    command.upgrade(cfg, down_revision)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO api_calls (project_name, call_type, model, status, started_at, user_id, created_at, "
                "updated_at) VALUES ('demo', 'image', 'm', 'success', '2026-11-20 00:00:00', 'default', "
                "'2026-11-20 00:00:00', '2026-11-20 00:00:00')"
            )
        )
    engine.dispose()

    command.upgrade(cfg, revision)

    assert "client" in _columns(db_path)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        value = conn.execute(sa.text("SELECT client FROM api_calls")).scalar_one()
    engine.dispose()
    assert value is None


def test_downgrade_drops_the_column_and_keeps_the_indexes(alembic_cfg, migration_revisions):
    cfg, db_path = alembic_cfg
    revision, down_revision = migration_revisions("*_add_client_to_api_calls.py")
    command.upgrade(cfg, revision)
    indexes_before = _indexes(db_path)

    command.downgrade(cfg, down_revision)

    assert "client" not in _columns(db_path)
    assert _indexes(db_path) == indexes_before


def test_upgrade_is_idempotent_when_the_column_already_exists(alembic_cfg, migration_revisions):
    """A database that already has the column (added by hand or by another branch) upgrades without error.

    已有该列的库（手工加过或别的分支加过）升级不报错。"""
    cfg, db_path = alembic_cfg
    revision, down_revision = migration_revisions("*_add_client_to_api_calls.py")
    command.upgrade(cfg, down_revision)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE api_calls ADD COLUMN client VARCHAR"))
    engine.dispose()

    command.upgrade(cfg, revision)

    assert "client" in _columns(db_path)
