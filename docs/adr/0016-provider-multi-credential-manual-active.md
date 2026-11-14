---
status: accepted
---

# Provider multi-credential support uses a dedicated credential table + manual switching of the active credential, with no automatic rotation

A credential is a structured entity (name / key / URL / active flag), with responsibilities different from the shared KV configuration. We decided each provider supports multiple credentials, stored in a dedicated `provider_credential` table (separate from shared configuration), with at most one `is_active` per provider (backed by a DB partial unique index), switched manually by the user in the UI and effective globally; deleting the active credential automatically reselects the other one with the earliest `created_at`; we explicitly **do not do** automatic rotation / load balancing / encrypted storage / usage statistics — KV prefix modelling has fragile naming, a single JSON field has complex concurrent read-modify-write, while a dedicated table has clear responsibilities and predictable behaviour.

## Consequences

- Mutual exclusion of the active flag must be guaranteed by the application layer within a transaction; switching/deleting must trigger backend cache invalidation.
- Agent Anthropic credentials follow the same "multiple credentials + single active" model but in their own table (see `docs/adr/0017`); their deletion behaviour is stricter — the active credential cannot be deleted directly, you must first switch to another one; there is no "automatically reselect the earliest".
