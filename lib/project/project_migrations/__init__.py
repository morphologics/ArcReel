"""File-level schema migration framework for projects.

Convention: top-level schema_version in project.json. Missing means v0. The current version is defined by
``lib.project.project_schema.CURRENT_PROJECT_SCHEMA_VERSION``. Migrators are pure, idempotent functions with the
signature ``def migrate(project_dir: Path) -> None``.

Project 文件级 schema 迁移框架。

约定：project.json 顶层 schema_version。缺失视为 v0。当前版本以 ``lib.project.project_schema.CURRENT_PROJECT_SCHEMA_VERSION`` 为准。
迁移器是纯函数，幂等，签名 ``def migrate(project_dir: Path) -> None``。
"""

from lib.project.project_migrations.runner import (
    CURRENT_SCHEMA_VERSION,
    MIGRATORS,
    cleanup_stale_backups,
    migrate_project_with_verdict,
    run_project_migrations,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATORS",
    "cleanup_stale_backups",
    "migrate_project_with_verdict",
    "run_project_migrations",
]
