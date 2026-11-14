---
status: accepted
---

# Remote MCP trusts existing API keys directly, without a separate fine-grained authorization layer

Remote MCP always requires and accepts only existing `arc-` API keys; even when the WebUI is configured with authentication off, it does not accept anonymous calls, session JWTs, or download tokens. In the current single-user product, an API key continues to represent a fully trusted, high-privilege automation principal: no project allowlist, capability scopes, key-level task ownership, quotas, or separate confirmation credentials are added; tool calls use `DEFAULT_USER_ID`, and the task source is recorded as `mcp`.

The remote tool list excludes provider, agent, API key, and system credential configuration as well as project deletion, and does not return secrets in tool results, but this is only MCP's creative-capability boundary, not a security permission boundary of the API key. The same key still holds the broad permissions of the existing REST API and should only be given to trusted external agents; if fine-grained API key permissions become a requirement, they will be designed separately. Remote tools must continue to reuse existing server-side invariants such as project parameter validation, transactional write entry points, revision concurrency checks, and migration gates.

Review, billing tiers, and lossy content operations keep the same confirmation experience as the built-in agent: the skill manual is responsible for showing the impact and obtaining the user's consent, and when the user explicitly authorizes full autonomy it may proceed; the server uses the existing confirmation parameters and does not verify additional human credentials. Any valid API key can query or cancel tasks of any project; task tools with `project` only verify that the task really belongs to that project, and use the existing global queue, concurrency admission, and cost records.
