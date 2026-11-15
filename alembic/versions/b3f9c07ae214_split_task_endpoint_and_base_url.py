"""split task provider_endpoint (protocol id) from submitted_base_url (request domain)

`tasks.provider_endpoint` 只承载自定义供应商的协议标识，请求域名一律落
`tasks.submitted_base_url`。存量行按真实语义回填：`provider_endpoint` 中 http(s) 形态的值是
域名，搬去 `submitted_base_url`。

Revision ID: b3f9c07ae214
Revises: f6a41746c0de
Create Date: 2026-08-18 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3f9c07ae214"
down_revision: str | Sequence[str] | None = "f6a41746c0de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Domain criterion: this column can only hold a protocol identifier or a request domain, and only values with an http(s) prefix are the latter. Protocol identifiers come from a
# closed endpoint registry (all slugs, no scheme); the domain side never lacks a scheme either -- persistence happens after the HTTP call that creates a
# provider task succeeds, and a base_url without a scheme is rejected by httpx when the request is sent, never reaching
# persistence. So the http(s) prefix is a complete criterion.
# lower(...) LIKE has the same semantics on SQLite and PostgreSQL, so no dialect branch is needed.
# 域名判据：该列只可能出现协议标识或请求域名，只有 http(s) 前缀的是后者。协议标识取自一份
# 闭合的 endpoint 注册表（全是 slug，无 scheme）；域名一侧也不会缺 scheme——落库发生在创建
# 供应商任务的 HTTP 调用成功之后，而无 scheme 的 base_url 在发请求时就被 httpx 拒绝，根本走
# 不到持久化。故 http(s) 前缀是完备判据。
# lower(...) LIKE 在 SQLite 与 PostgreSQL 上语义一致，无需方言分支。
_HAS_DOMAIN = (
    "provider_endpoint IS NOT NULL "
    "AND (lower(provider_endpoint) LIKE 'http://%' OR lower(provider_endpoint) LIKE 'https://%')"
)

# Move first, then clear, and fill only empty columns: for rows where the domain is present in both places, the dedicated column wins and is not overwritten.
# 先搬后清，且只填空列：域名两处都有值的行以专列为准，不覆盖。
_MOVE_DOMAIN = (
    f"UPDATE tasks SET submitted_base_url = provider_endpoint WHERE submitted_base_url IS NULL AND {_HAS_DOMAIN}"
)
_CLEAR_DOMAIN = f"UPDATE tasks SET provider_endpoint = NULL WHERE {_HAS_DOMAIN}"

# The verification criterion is deliberately not derived from the backfill criterion: self-checking with the same predicate necessarily yields 0 and asks nothing. Ask instead "are there still rows
# containing a scheme separator", which both rechecks that http(s) is cleared and catches lower()/LIKE behaving unexpectedly or other schemes lying in existing data
# -- protocol identifiers never contain `://`, so a hit means the backfill was not clean.
# 校验判据刻意不与回填判据同源：拿同一个谓词自查必然得 0，问不出任何东西。改问「还有没有行
# 带 scheme 分隔符」，既复核 http(s) 已清空，也能撞见 lower()/LIKE 行为不符预期或存量里躺着
# 其他 scheme 的情形——协议标识里不会出现 `://`，命中即回填没做干净。
_COUNT_SCHEME_LIKE = "SELECT COUNT(*) FROM tasks WHERE provider_endpoint LIKE '%://%'"

# After the backfill, built-in provider rows have only the domain column set, from which the reverse restores; custom provider rows have both columns and are left alone.
# 回填后内置供应商的行只剩域名一列有值，据此反向还原；自定义供应商的行两列俱在，不动。
_RESTORE_DOMAIN = (
    "UPDATE tasks SET provider_endpoint = submitted_base_url, submitted_base_url = NULL "
    "WHERE provider_endpoint IS NULL AND submitted_base_url IS NOT NULL"
)


def upgrade() -> None:
    """Backfill data: move request domains out of provider_endpoint into submitted_base_url."""
    bind = op.get_bind()
    bind.execute(sa.text(_MOVE_DOMAIN))
    bind.execute(sa.text(_CLEAR_DOMAIN))
    remaining = bind.execute(sa.text(_COUNT_SCHEME_LIKE)).scalar()
    if remaining:
        raise RuntimeError(f"{remaining} 行的 tasks.provider_endpoint 仍存放请求域名，回填未完成")


def downgrade() -> None:
    """Move builtin-provider domains back into provider_endpoint."""
    bind = op.get_bind()
    bind.execute(sa.text(_RESTORE_DOMAIN))
