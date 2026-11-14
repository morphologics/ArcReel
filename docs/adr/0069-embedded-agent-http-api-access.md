---
status: accepted
---

# The embedded agent calls the same-origin HTTP API with a short-lived JWT and open network access

Adapting custom call endpoints requires the embedded agent to both read arbitrary provider documentation and call ArcReel's
existing validate, endpoint test, and save HTTP APIs. The original fixed sandbox `allowedDomains` list cannot cover the
provider domains users choose; wrapping these interfaces again as resident MCP / SDK tools would create a second agent surface.

We decided to change the embedded agent sandbox's network configuration to `allowedDomains: ["*"]` plus `allowLocalBinding: true`; file
`denyRead` / `denyWrite`, the ban on unsandboxed fallback, and the Windows command allow-list stay unchanged. `WebFetch` is added to the
allowed tools; `WebSearch` is not. Each time session options are built, if authentication is on, a 15-minute session JWT is issued into
`ARCREEL_API_TOKEN`, and `ARCREEL_API_BASE` is injected (default `http://127.0.0.1:1241/api/v1`); when authentication is off
the token is empty. The dedicated token name is reserved for skill scripts inside the sandbox to read, and other provider / secret-like environment variables
continue to be stripped from Bash subprocesses.

Both network switches must be written explicitly; neither can be omitted. `allowedDomains` is a pre-approval list, not a restriction list: omitting the `network`
key means zero pre-approvals, and in unattended sessions approval requests for new domains are rejected outright, so outbound traffic is cut off entirely. `allowLocalBinding` separately
controls loopback, which `allowedDomains` doesn't cover whether it says `*` or `127.0.0.1`, and skill scripts calling the same-origin
ArcReel API must go through loopback.

This choice widens the trust boundary in two directions. First, a prompt injection in provider documentation or a hijacked agent can, within the token's
validity period, exfiltrate it to any domain, and can also call every ArcReel API reachable with that administrator session JWT — including the configuration interfaces for provider and
agent credentials (keys in responses are all masked); therefore stripping secret-like environment variables from Bash subprocesses only hides key values from the process
environment, and doesn't stop an agent that has the token from using stored credentials via the API.
The JWT is stateless and cannot be revoked early once issued: the only way to invalidate it is to rotate `AUTH_TOKEN_SECRET`, at the cost
of kicking out all web sessions at once. The 15-minute lifetime only shortens the window and doesn't reduce privileges; it is also the usable
window for API calls within a single session — the token isn't renewed, so when authentication is on and a session runs longer than 15 minutes, skill scripts' API calls will fail with 401, and the session must be reopened
to get a new token. Second, `allowLocalBinding` exposes the loopback of the whole
host, not just ArcReel's own port — databases on the same machine, other applications' dev servers, and the user's private local services are all
reachable from Bash inside the sandbox, and the file fence doesn't cover this path.

These two risks are accepted in exchange for the same HTTP API serving both external and embedded agents. Billed connection tests and destructive overwrite operations are still
confirmed by the skill asking back before the call, but that is a constraint on agent behavior, not security isolation.
