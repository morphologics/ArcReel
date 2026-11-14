---
status: accepted
---

# The generation task queue stays in-house for now; no third-party task queue library is introduced

Persistence, claiming, concurrency and restart self-healing of generation tasks are implemented by ArcReel itself (`lib/generation/generation_queue.py`, `lib/generation/generation_worker.py`, `lib/db/repositories/task_repo.py`), not built on a task queue library such as Celery, dramatiq, arq, taskiq, procrastinate or APScheduler. We evaluated switching to a library and decided not to for now, for two reasons; without either one, the conclusion would be different.

First, there is no mature candidate for the supported deployment forms. ArcReel must be able to run in a single-machine form of "SQLite by default, no dependency on any additional service, runs natively on Windows", with queue state and business state in the same database pointed to by `DATABASE_URL` (ADR 0020), supporting both SQLite and PostgreSQL dialects. The candidates have different blocking conditions, but they all land in the same place: either another messaging service has to run outside the application, or task state cannot be persisted into the same database as business state with both dialects supported:

- Celery: the only stable transports are RabbitMQ, Redis and Amazon SQS, all messaging services outside the application; the database transport is experimental and officially recommended only for local development; Windows has been officially unsupported since 4.x.
- dramatiq: the bundled brokers are RabbitMQ and Redis.
- arq: supports only Redis.
- taskiq: the in-process InMemoryBroker targets local development and does not persist tasks; the ZeroMQ broker only works with a single worker process and likewise does not persist; the remaining brokers (RabbitMQ, Redis, NATS, plus third-party-maintained PostgreSQL and SQS) are either external services or cover only the PostgreSQL dialect.
- procrastinate: supports only PostgreSQL.
- APScheduler 4: the only one whose form matches (SQLAlchemy async data store, both SQLite and PostgreSQL, runs in process); the blocking condition is that the official documentation states 4.x is pre-release and must not be used in production. It limits concurrency per task id, the same function can be configured with multiple task ids, and the limit can be adjusted at runtime, so "capacity per provider and media channel" can be mapped onto a set of task ids; but that mapping, and the semantics of "reloading limits does not interrupt in-flight tasks", would have to be implemented ourselves, which is integration complexity rather than a blocking condition.

Second, the part a queue library could replace is small. Of the three files, the generic queue substrate (claiming, leases, state-transition guards, the occupancy ledger) is only a few hundred lines; the rest is domain logic that a queue library would not provide and that we would still have to write after switching: capacity reloaded at runtime with the user's provider configuration without interrupting in-flight tasks (ADR 0042, 0043); orphaned tasks are never re-enqueued but resumed from the provider task id and execution checkpoint to avoid double charging (ADR 0007); the execution entry checks the rate-limit routing key against the current project and returns the task to the queue in place on mismatch (ADR 0055, 0056); finalisation of provider call accounting; whole-batch admission (ADR 0061). The defects caused by the in-house substrate were concentrated early on and have converged; since then, defects have almost all been in these domain semantics, and switching libraries would not make them disappear.

## Consequences

- This is a "for now" conclusion, not a permanent exclusion. It should be re-evaluated when any of the following changes: a decision to support multi-process or multi-instance deployment (restart self-healing's determination of orphaned tasks assumes a single process; see ADR 0007); the queue only needs to run on PostgreSQL (at which point PostgreSQL-native queue libraries like procrastinate become candidates); a queue library appears that supports both SQLite and PostgreSQL, can run in process, and has a stable release.
- When re-evaluating, the thing to compare is the maintenance cost of those few hundred lines of generic substrate, not the size of the whole queue module; the domain logic stays in ArcReel regardless.
- Provider RPM limits are not part of the queue; each invocation channel throttles itself.
