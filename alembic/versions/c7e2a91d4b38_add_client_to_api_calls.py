"""add client to api_calls

The client (caller label) a provider call was made for: the source of the task it serves, or the client of the HTTP
request / worker / agent tool call that made it (``lib/billing/call_client.py``). Nullable: rows written before this
revision have no client.

供应商调用所属的客户端（调用方标签）：它服务的任务的 source，或发起它的 HTTP 请求 / worker / Agent 工具调用的
客户端（``lib/billing/call_client.py``）。可空：本版本之前写入的行没有客户端。

Revision ID: c7e2a91d4b38
Revises: 50a4847b9836
Create Date: 2026-11-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from lib.db.migration_helpers import preserve_sqlite_indexes

# revision identifiers, used by Alembic.
revision: str = "c7e2a91d4b38"
down_revision: str | Sequence[str] | None = "50a4847b9836"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_client_column() -> bool:
    return "client" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("api_calls")}


def upgrade() -> None:
    """Upgrade schema."""
    # Idempotent: a database that already has the column (added by hand or by a branch of this fork) upgrades cleanly.
    # 幂等：已有该列的库（手工加过，或本 fork 的某个分支加过）照常升级。
    if _has_client_column():
        return
    with op.batch_alter_table("api_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("client", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    if not _has_client_column():
        return
    with preserve_sqlite_indexes("api_calls"), op.batch_alter_table("api_calls", schema=None) as batch_op:
        batch_op.drop_column("client")
