"""add generation request snapshot and incremental sync indexes

``tasks.request_snapshot_json``: the inputs a media generation task was submitted with, taken at enqueue (the
editable prompt, the declared options and model, the reference inputs with their versions), so an attempt that is
refused or cancelled before a provider call still keeps its inputs. Nullable: tasks queued before this revision and
text / render tasks have none. Indexes for the "changed since" reads and the generation record list:
``idx_tasks_updated_at`` (``GET /tasks?updated_since=`` across projects), ``idx_tasks_project_queued_at`` (a project's
generation records, newest first), ``idx_api_calls_finished_at`` (``GET /usage/records?updated_since=``).

``tasks.request_snapshot_json``：媒体生成任务提交时的输入，在入队时取下（可编辑提示词、声明的选项与模型、参考输入及其版本），
在供应商调用之前被拒绝或取消的尝试也留有输入。可空：本版本之前入队的任务与文本 / 渲染任务没有。另加「自某时刻以来的变化」
读取与生成记录列表用的索引：``idx_tasks_updated_at``（跨项目的 ``GET /tasks?updated_since=``）、
``idx_tasks_project_queued_at``（一个项目的生成记录，新的在前）、``idx_api_calls_finished_at``
（``GET /usage/records?updated_since=``）。

Revision ID: e5b9c3d71a42
Revises: c7e2a91d4b38
Create Date: 2026-11-21 18:00:00.000000

"""

import warnings
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from lib.db.migration_helpers import preserve_sqlite_indexes

# revision identifiers, used by Alembic.
revision: str = "e5b9c3d71a42"
down_revision: str | Sequence[str] | None = "c7e2a91d4b38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("idx_tasks_updated_at", "tasks", ["updated_at"]),
    ("idx_tasks_project_queued_at", "tasks", ["project_name", "queued_at"]),
    ("idx_api_calls_finished_at", "api_calls", ["finished_at"]),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    # Only names are needed; SQLAlchemy warns that it cannot reflect the expression index idx_tasks_dedupe_active.
    # 只要名字；SQLAlchemy 会警告反射不出表达式索引 idx_tasks_dedupe_active。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", sa.exc.SAWarning)
        indexes = sa.inspect(op.get_bind()).get_indexes(table)
    return {index["name"] for index in indexes if index.get("name")}


def upgrade() -> None:
    """Upgrade schema."""
    # Idempotent: a database that already has the column or an index (added by hand or by a branch of this fork)
    # upgrades cleanly.
    # 幂等：已有该列或某个索引的库（手工加过，或本 fork 的某个分支加过）照常升级。
    if "request_snapshot_json" not in _columns("tasks"):
        with preserve_sqlite_indexes("tasks"), op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("request_snapshot_json", sa.Text(), nullable=True))
    for name, table, columns in _INDEXES:
        if name not in _indexes(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    """Downgrade schema."""
    # Indexes first and outside preserve_sqlite_indexes: that context replays indexes missing on exit, which would
    # bring back the ones this downgrade drops on purpose.
    # 先删索引，且放在 preserve_sqlite_indexes 之外：该上下文会回放退出时缺失的索引，会把本降级有意删除的索引又加回来。
    for name, table, _columns_of_index in _INDEXES:
        if name in _indexes(table):
            op.drop_index(name, table_name=table)
    if "request_snapshot_json" in _columns("tasks"):
        with preserve_sqlite_indexes("tasks"), op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.drop_column("request_snapshot_json")
