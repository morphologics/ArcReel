---
status: accepted
---

# The open-source edition pre-embeds the multi-user data model and the `_scope_query` seam, but does not implement multi-tenancy

Adding NOT NULL foreign-key columns to in-use tables later + backfilling historical rows costs far more than pre-embedding them with a `server_default` from the start, and would make the open-source/commercial schemas diverge. We decided that the open-source edition already adds `user_id` (always `"default"`) to Task/ApiCall/ApiKey/AgentSession, creates a users table (default admin), and the Repository base class provides a no-op `_scope_query` rewrite point; the commercial edition implements visibility isolation by extending fields via migration + overriding `_scope_query` in a subclass to inject a user_id filter. No directory isolation, login flow, quotas or admin back office.

## Consequences

- The open-source edition carries a batch of seemingly useless no-ops and a constant `"default"`; **readers should not delete them as dead code**.
- `claim_next` uses raw SQL, which `_scope_query` cannot intercept; it is a known special case that the commercial edition needs to override.
