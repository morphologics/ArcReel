---
status: accepted
---

# Runtime state merged into a single async ORM database

The original 3 independent synchronous SQLite databases (task queue / API usage / agent sessions) could not be switched to PostgreSQL with a single `DATABASE_URL`, could not use foreign keys/transactions across databases, and the synchronous drivers blocked the event loop in async FastAPI routes. We decided on a hard cut-over, merging them into a single SQLAlchemy async database, with one `DATABASE_URL` determining SQLite (development) or PostgreSQL (production), providing a one-time migration and not keeping the old hand-written SQL path — the hard cut-over avoids an intermediate dual-write state, in exchange for unified transaction semantics and a switchable database.

## Consequences

- The migration cannot be rolled back; once all Repositories/Workers are async and depend on single-database transaction semantics, going back to multiple databases or synchronous drivers would require rewriting the whole data access layer.
- The storage boundary is clearly layered: runtime state goes into the DB; project data (project.json / scripts / media files) stays on the file system.
