---
status: accepted
---

# SDK transcript mirrored into our own DB, eager flush by default; idle sessions are evicted from memory and can be resumed without losing history

The SDK's built-in jsonl transcript is tied to the local file system, which does not match the deployment shape (multi-user/PostgreSQL) of "runtime state unified into a single async ORM DB" (`docs/adr/0020`). We decided to implement a custom SessionStore that mirrors the transcript entry by entry into the DB (`agent_session_entries` + a session summary table), controlled by `ARCREEL_SDK_SESSION_STORE` (default `db`; `off` falls back to SDK jsonl), with a startup hook doing a one-time migration of local historical jsonl into the database; the flush mode defaults to eager — writing to the database entry by entry, in exchange for SDK resume not losing context after a crash. The transcript serves only SDK resume; the read source for the UI timeline is the session event log (see `docs/adr/0048`).

## Consequences

- Idle sessions are evicted from memory after a delay (`agent_session_cleanup_delay_seconds`, default 300 seconds, with a periodic sweep as a backstop), closing the actor/SDK subprocess, to bound resident memory. Sessions that are not idle are not evicted (closing the subprocess would abort the in-flight turn, also killing in-flight subagents, and the autonomous turn after completion would not happen); when capacity is insufficient, new sessions are rejected instead; the timer restarts after the CLI reports idle. The criterion is simply the session state: a session is running until the CLI reports `idle` (the CLI keeps reporting running while subagents are alive, or while one more turn is still owed after they finish), consistent with how the SDK decides whether a run has ended. Likewise there is no backstop cap: if the CLI never reports idle, the session is kept until the process restarts. Precisely because the transcript is already in the database, eviction loses no history: on the next access, the actor is rebuilt by SDK resume via sdk_session_id to continue the conversation.
- Eager flush has write amplification; under a slow store the SDK merges frames on its own. `off` mode is only suitable for single-machine development.
