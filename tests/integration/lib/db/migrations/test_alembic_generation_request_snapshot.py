"""Up/down migration tests for tasks.request_snapshot_json and the incremental sync indexes (API G16 / G24).

tasks.request_snapshot_json 与增量同步索引的双向迁移测试（API G16 / G24）。"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from alembic import command

_GLOB = "*_add_generation_request_snapshot_and_sync_indexes.py"
_NEW_INDEXES = {
    "tasks": {"idx_tasks_updated_at", "idx_tasks_project_queued_at"},
    "api_calls": {"idx_api_calls_finished_at"},
}


def _columns(db_path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    engine.dispose()
    return {r[1] for r in rows}


def _indexes(db_path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = conn.execute(sa.text(f"PRAGMA index_list('{table}')")).fetchall()
    engine.dispose()
    return {r[1] for r in rows}


def test_upgrade_adds_the_nullable_snapshot_and_the_indexes(alembic_cfg, migration_revisions) -> None:
    """Tasks queued before the column existed have no snapshot (NULL); the dedupe index survives.

    该列出现之前入队的任务没有快照（NULL）；去重索引保留。"""
    cfg, db_path = alembic_cfg
    revision, down_revision = migration_revisions(_GLOB)
    command.upgrade(cfg, down_revision)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tasks (task_id, project_name, task_type, media_type, resource_id, status, source, "
                "queued_at, updated_at) VALUES ('T-old', 'demo', 'storyboard', 'image', 'E1S01', 'failed', "
                "'webui', '2026-11-20 00:00:00', '2026-11-20 00:00:00')"
            )
        )
    engine.dispose()

    command.upgrade(cfg, revision)

    assert "request_snapshot_json" in _columns(db_path, "tasks")
    for table, names in _NEW_INDEXES.items():
        assert names <= _indexes(db_path, table)
    assert "idx_tasks_dedupe_active" in _indexes(db_path, "tasks")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        value = conn.execute(sa.text("SELECT request_snapshot_json FROM tasks WHERE task_id='T-old'")).scalar()
    engine.dispose()
    assert value is None


def test_downgrade_drops_column_and_new_indexes_and_keeps_the_others(alembic_cfg, migration_revisions) -> None:
    cfg, db_path = alembic_cfg
    revision, down_revision = migration_revisions(_GLOB)
    command.upgrade(cfg, down_revision)
    before = {table: _indexes(db_path, table) for table in _NEW_INDEXES}
    command.upgrade(cfg, revision)

    command.downgrade(cfg, down_revision)

    assert "request_snapshot_json" not in _columns(db_path, "tasks")
    for table in _NEW_INDEXES:
        assert _indexes(db_path, table) == before[table]


def test_upgrade_is_idempotent_when_column_and_an_index_already_exist(alembic_cfg, migration_revisions) -> None:
    cfg, db_path = alembic_cfg
    revision, down_revision = migration_revisions(_GLOB)
    command.upgrade(cfg, down_revision)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE tasks ADD COLUMN request_snapshot_json TEXT"))
        conn.execute(sa.text("CREATE INDEX idx_tasks_updated_at ON tasks (updated_at)"))
    engine.dispose()

    command.upgrade(cfg, revision)

    assert "request_snapshot_json" in _columns(db_path, "tasks")
    for table, names in _NEW_INDEXES.items():
        assert names <= _indexes(db_path, table)
