"""Alembic migration (YouReel): create the client error reports table, dropped on downgrade."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command


def _columns(db_path: Path) -> dict[str, list[str]]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        inspector = sa.inspect(engine)
        return {name: [c["name"] for c in inspector.get_columns(name)] for name in inspector.get_table_names()}
    finally:
        engine.dispose()


def test_upgrade_creates_table_and_downgrade_drops_it(
    alembic_cfg: tuple[Config, Path], migration_revisions: Callable[[str], tuple[str, str]]
):
    cfg, db_path = alembic_cfg
    revision, parent = migration_revisions("*_add_client_error_reports_table.py")

    command.upgrade(cfg, revision)
    assert _columns(db_path)["client_error_reports"] == [
        "id",
        "created_at",
        "kind",
        "message",
        "detail",
        "user_agent",
        "app_build",
    ]

    command.downgrade(cfg, parent)
    assert "client_error_reports" not in _columns(db_path)
