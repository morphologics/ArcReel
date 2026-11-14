---
status: accepted
---

# Sessions are created on send; the external identity is unified as sdk_session_id

Pre-creating session rows requires generating our own ID and then maintaining a mapping once the SDK returns a session_id, and produces orphan rows that were "created but never spoke". We decided not to pre-create: a new session first starts an actor in a temporary in-memory state and sends the first message; only when the SDK init message returns the session_id is the DB session row written, and the in-memory key is replaced from the temporary ID to the real ID (key swap); from then on query/update/resume uniformly use `sdk_session_id` (UNIQUE) as the sole external identity.

## Consequences

- Until the SDK init for the first message returns, the session is not addressable (not in the list, cannot be subscribed to/resumed by ID).
- Key swap is a sensitive path: any code that looks up a session by ID within the "sent, init not yet returned" window must account for the temporary-ID phase.
