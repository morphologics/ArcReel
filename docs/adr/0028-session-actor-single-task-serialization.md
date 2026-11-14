---
status: accepted
---

# SessionActor: a single asyncio task per session serialises all ClaudeSDKClient calls; message scheduling is left to the CLI

ClaudeSDKClient cannot safely be called concurrently, and internally it runs on anyio; calling across tasks while holding a lock easily deadlocks and breaks the SDK's internal state machine assumptions. We decided to give each agent session a dedicated actor task that exclusively owns the session's ClaudeSDKClient: query/interrupt/withdraw queued message/disconnect are all delivered via a command queue and executed serially inside the actor task; the message stream is read continuously for the lifetime of the session, interleaved with the command queue using `asyncio.wait` (an interrupt doesn't have to wait for the whole turn's streaming to finish); when the message stream terminates (CLI exits) the actor exits with it, and the session layer wraps up according to the terminal state; externally messages are pushed only through the `on_message` callback. "Each caller adds its own lock" and "call concurrently directly" were rejected.

There is only one layer of message scheduling: the CLI. The CLI has its own message queue: user messages received while a turn is in progress are merged into the current turn at a tool boundary, or merged with other queued messages into a new turn after this turn ends; completion notifications from background tasks go through the same queue and can open autonomous turns without a query. Therefore:

- **User messages are always handed to the CLI immediately**, not blocked or held back because "a turn is running"; each message carries a server-assigned uuid, and "send now" is delivered with `now` priority, with the CLI interrupting the current turn to handle it first.
- **The actor does not match queries with results**: a result only means a turn ended, and does not correspond to any particular query.
- **Session state follows the session state reported by the CLI**: it becomes running as soon as a message is delivered, and only leaves running when the CLI reports `idle` (only emitted after the queue drains and background waits end) or the actor exits; a result does not switch the state.
- **The fate of queued messages follows the CLI's `command_lifecycle` frames** (`started` means accepted, `cancelled` means discarded). The Python SDK's message parsing discards these frames, so the actor reads the raw frames from `client._query.receive_messages()` directly, handles `command_lifecycle` itself, and passes the remaining frames to the SDK's `parse_message`.

## Considered Options

- **ArcReel stacking another layer of scheduling on top of the CLI** (one user message corresponds to one turn and one result; rejecting new messages while a turn is running; switching state and clearing echo registration by query/result): the CLI doesn't guarantee this one-to-one correspondence; when queued messages are merged into the current turn or combined into one turn, or autonomous turns are interleaved, results get attributed to the wrong query, echo registration is cleared early causing duplicate persistence, and state gets overwritten. The two layers of scheduling don't line up, and race conditions can only be patched one by one.

## Consequences

- Any new SDK operation must go through the command queue into the actor; the client must not be operated directly from an external task.
- The actor is the main resident-memory body of a session; its lifecycle (eviction/sweep/resume) is managed by SessionManager (see `docs/adr/0029`).
- The actor depends on three private SDK entry points: `client._query.receive_messages()`, `parse_message` and `_query._send_control_request` (`cancel_async_message`, used to withdraw queued messages, is not public in the SDK). These three are locked down by tests when upgrading the SDK.
