---
status: accepted
---

# The agent toolset is declared from a single source; host differences are allowed on only two axes

Each tool in the agent toolset has exactly one host-independent declaration: name, full Chinese description (parameter descriptions are written on the request model fields), the schema derived from the pydantic request model, the migration-blocking policy (required, no default), the domain key, and the handler. The two hosts, the ArcReel agent and external agents, each hold a thin adapter that only projects this declaration into its own registration form and result envelope. The only differences allowed between the two hosts are two axes: how the project is determined (only for tools that act on an existing project: for the ArcReel agent it is determined by the session and the schema does not expose `project`; external agents specify it explicitly each time. `list_projects` and `create_project` have no target project, and neither host carries `project` for them) and whether long tasks wait for results (the ArcReel agent waits until the terminal state; external agents immediately get a batch handle, and the polling instructions in the description are derived from the "long task" marker). Everything else shares one source: adapters do not rewrite problem codes, do not create their own domain keys, and do not add or remove result fields; the same `ToolOutcome` encodes to identical JSON on both hosts, and the ArcReel agent receives a summary text plus this JSON, rather than only the summary. Long tasks are the exception due to the second axis: the two hosts receive `ToolOutcome`s from different stages (for the ArcReel agent, the terminal generation result; for external agents, initially a batch handle), and consistency lies in the batch query result that the external agent gets when polling reaches the terminal state, where the attached generation result has the same shape as what the ArcReel agent receives.

When each host previously had its own declaration, drift appeared within a month: error codes were rewritten to `internal_error`, the same tool had two domain keys, migration refusals came in three shapes, four entry points failed to declare migration blocking, and domain tests were attached to an envelope whose fields the SDK would discard.

## Considered Options

- **Take the best of each host** (short English descriptions for external agents to save tokens, summary only for the ArcReel agent to save context): rejected. Every "host-specific" point becomes the entry for the next drift, and both hosts serve the same kind of creative agent, so there is no reason for the contracts to differ.
- **Keep the hand-written `MIGRATION_BLOCKED_TOOL_IDS` set and have the external agent adapter read it too**: rejected. The set has no enforcement over new tools; making the blocking policy a required field of the declaration is how ADR 0062's "blocking follows the entry-point declaration" is actually implemented.

## Consequences

- Domain behaviour is tested once at the handler (`ToolOutcome`) layer; adapters carry no domain assertions, only a dual-host consistency test driven by the declaration table plus a few envelope tests.
- `ARCREEL_MCP_TOOL_IDS` and the migration-blocking set are both derived from the declarations; the names are kept for existing consumers (profile lint, the frontend tool display-name i18n test).
- For tools whose migration-blocking policy is "block", the migration refusal note at the end of the description is likewise derived from the policy and not written in the declaration text; the descriptions both hosts read come from the same derivation function, so the declaration's description field is not equal to the final description.
