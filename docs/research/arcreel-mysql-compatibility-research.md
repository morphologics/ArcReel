# MySQL 8 Compatibility Research

> Status: research phase. The gap list is a working list, not a completion report.
> Related: [#1983](https://github.com/ArcReel/ArcReel/issues/1983) (proposal), [#1985](https://github.com/ArcReel/ArcReel/pull/1985) (first batch of migration/deployment/docs implementation)

## Background

ArcReel currently supports two dialects: SQLite and PostgreSQL. This research evaluates the gaps and the path to adding MySQL 8.0+ as an optional self-hosted backend.

## Current status

- [x] Research complete, compatibility gap list compiled (this document)
- [ ] Engine-layer adaptation (`lib/db/engine.py`)
- [ ] alembic migration fixes
- [ ] CI: mysql-compat job
- [ ] Documentation updates (`.env.example` + docs)

## Compatibility gap list

The items below are labelled by strength of evidence: **Reproduced** = actually observed on SQLite/PG; **To be verified** = inferred from the MySQL documentation, not yet tested on a real MySQL 8.

### 1. Expression indexes (reproduced on SQLite only)

- SQLite loses the expression index when a table is rebuilt during rollback (see [#1807](https://github.com/ArcReel/ArcReel/issues/1807))
- Whether MySQL 8 has the same problem: **to be verified**. The evidence in #1807 covers SQLite only and does not constitute evidence of a MySQL failure. When verifying, record: MySQL version, index definition, migration revision, expected index state, and actual result.

### 2. Partial unique indexes (known to be unsupported in MySQL; replacement still to be decided)

Two places in the project rely on partial unique indexes via `sqlite_where` / `postgresql_where`:

- `idx_tasks_dedupe_active` in `lib/db/models/task.py` (active-task deduplication: `COALESCE` expression column + where condition)
- `uq_provider_credential_one_active` in `alembic/versions/2c57c41eca74_add_provider_credential_table.py` (only one active credential per provider)

MySQL 8 does not support partial indexes: a straight migration produces an **unconditional unique index** — for the former this breaks deduplication or makes the migration fail; for the latter it prevents existing data with "multiple inactive credentials" from getting through the migration. Candidate replacement: a generated column (virtual generated column) + an ordinary unique index. Each of the two needs its own design and tests.

### 3. Runtime DML dialect branches (to be added)

The bulk insert in `lib/agent/agent_session_store/store.py` distinguishes only two branches, postgresql / sqlite; every other dialect (including MySQL) falls through to SQLite's `on_conflict_do_nothing()`, which fails to compile on MySQL. The engine-layer adaptation needs to add a MySQL insert strategy (`INSERT IGNORE` or `ON DUPLICATE KEY`) plus compatibility tests.

### 4. JSON type (to be verified)

SQLite stores text / PostgreSQL has native JSON / MySQL 8 has native JSON; the read and write paths for all three need to be verified one by one.

### 5. server_default (no difference found in initial check; systematic check pending)

The initial check found no `text("NOW()")` usage; existing migrations consistently use `sa.func.now()`. What needs a systematic check is whether the actual DDL that `func.now()` generates across the three dialects (`CURRENT_TIMESTAMP` variants) and the time precision (MySQL `DATETIME` defaults to seconds vs. the microseconds the project requires) are consistent, recorded migration by migration.

## Migration verification plan

Each affected migration must pass both the offline and the online criteria, and each revision must independently run a round trip and an idempotent upgrade.

> `URL_ENCODED_PASSWORD` in the commands below is a placeholder (it contains no shell metacharacters such as `<>`, so the whole URL, wrapped in single quotes, can be copied as is). Replace it with the real password before running; if the password contains URL-reserved characters (such as `@ : / ?`), URL-encode it first. Never write a real password into this document.

### offline (no database connection, generate SQL per revision)

```text
# Generate and inspect SQL for each revision separately
# Syntax: alembic upgrade <from_revision>:<to_revision> --sql
DATABASE_URL='mysql+aiomysql://arcreel:URL_ENCODED_PASSWORD@mysql:3306/arcreel?charset=utf8mb4' \
  alembic upgrade f6a41746c0de:7a8b9c0d1e2f --sql > upgrade_7a8b9c0d1e2f.sql
DATABASE_URL='mysql+aiomysql://arcreel:URL_ENCODED_PASSWORD@mysql:3306/arcreel?charset=utf8mb4' \
  alembic upgrade f6a41746c0de:bcaaa615ff38 --sql > upgrade_bcaaa615ff38.sql
DATABASE_URL='mysql+aiomysql://arcreel:URL_ENCODED_PASSWORD@mysql:3306/arcreel?charset=utf8mb4' \
  alembic downgrade 7a8b9c0d1e2f:f6a41746c0de --sql > downgrade_7a8b9c0d1e2f.sql
DATABASE_URL='mysql+aiomysql://arcreel:URL_ENCODED_PASSWORD@mysql:3306/arcreel?charset=utf8mb4' \
  alembic downgrade bcaaa615ff38:7a8b9c0d1e2f --sql > downgrade_bcaaa615ff38.sql
# Check: dialect differences, type conversion logic, whether the downgrade/rollback path is sound
```

### online (real MySQL, each revision independently round-tripped + idempotent)

```text
export DATABASE_URL='mysql+aiomysql://arcreel:URL_ENCODED_PASSWORD@mysql:3306/arcreel?charset=utf8mb4'

# 7a8b9c0d1e2f independent round trip
alembic upgrade 7a8b9c0d1e2f
alembic downgrade 7a8b9c0d1e2f~1      # back to this migration's down_revision
alembic upgrade 7a8b9c0d1e2f          # idempotent upgrade: schema matches the one after the first upgrade

# bcaaa615ff38 independent round trip
alembic upgrade bcaaa615ff38
alembic downgrade bcaaa615ff38~1      # back to this migration's down_revision
alembic upgrade bcaaa615ff38          # idempotent upgrade

# head-level idempotency: running upgrade head again when the DB is already at head must have no side effects
alembic upgrade head                  # first time
alembic upgrade head                  # second time (should be a no-op)
# Compare: schema is identical after the two upgrade head runs (suggest alembic current + DDL dump diff)
```

> `f6a41746c0de` above is a placeholder for the parent revision at the time of this research; `b3f9c07ae214` is the current head of the migration chain in PR #1985. When landing this, replace the two downgrade commands with each migration file's actual `down_revision` (confirm with `grep down_revision alembic/versions/<file>.py`).

| Migration | Revision | What it affects | Verification scope |
|---|---|---|---|
| 7a8b9c0d1e2f | unify_partial_indexes_and_string_lengths | Generated columns + field lengths | MySQL: generated column syntax is correct, unique index semantics are equivalent; PG/SQLite: partial indexes are not broken |
| bcaaa615ff38 | mysql_varchar_to_datetime | VARCHAR→DATETIME(6) | MySQL: data conversion loses no precision; PG/SQLite: no-op |

| Mode | Definition | Verification criteria |
|---|---|---|
|| offline | Renders SQL only, does not connect to a database | upgrade/downgrade scripts are syntactically correct, downgrade path is reversible |
|| online | Runs against a real MySQL instance | Each revision independently runs the `upgrade → downgrade → upgrade` round trip without errors, and the final schema matches the first upgrade; after all revisions are round-tripped, run `upgrade head` twice in a row — the second is a no-op and the final schema is identical both times |
|| online (data) | Runs after loading representative legacy data | Existing data remains semantically equivalent after migration (e.g. multiple inactive provider_credential records can still coexist) |


## Next steps

Once every item on the gap list has been turned into implementation, proceed in separate PRs in the order engine layer → migrations → CI → docs; the first batch (VARCHAR→DATETIME migration / deployment templates / ops docs) is in #1985.
