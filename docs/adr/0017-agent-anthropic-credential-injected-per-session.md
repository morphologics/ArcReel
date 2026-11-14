---
status: accepted
---

# Agent Anthropic credentials are stored independently and injected into env per session from the DB, rather than written to the global os.environ

The global env is a process-level single value, which conflicts with "multiple credentials + each session may use a different active one", and provider keys are already fully banned from `os.environ`. We decided the Claude Agent SDK's Anthropic gateway credentials are stored in a dedicated table (separate from custom provider credentials, not in `ENDPOINT_REGISTRY`, not involved in media generation); they take effect by having `build_anthropic_env_dict` (whose input is a DB session) read the active credential from the DB each time a new agent session is created and return a dict injected into `ClaudeAgentOptions.env`, **without writing the global os.environ**; the activate endpoint only does set_active and does no env sync.

## Consequences

- Already-running sessions still hold the env from spawn time; switching the active credential only takes effect for **new** sessions (only a toast notice, no forced termination).
- Complementary to `docs/adr/0008` (custom provider credential model): together they make clear the boundaries of "credential storage not polluted by protocol shape/process globals" and "determined at runtime vs declared at startup".
