---
status: accepted
---

# Session event log: the single read source for the UI timeline, with responsibilities separated from the SDK transcript

If the UI truth of agent conversations is scattered across several sources (the SDK transcript, an in-memory buffer, the streaming preview) and the sources share no message identity, consistency can only be stitched together by content-comparison heuristics, and message loss/duplication and rendering inconsistencies keep leaking through edge cases. We decided to add a **session event log** (a persisted, independent table with a monotonic seq per session) as the single read source for the UI timeline:

- Entries are **typed at the write point** — semantic recognition such as task notifications, interrupts, skill invocations, and subagent attribution happens only at the single point of writing into the log and is persisted; skill entries record only the name and input arguments, and the injected full text does not enter the log.
- **A user message's identity is assigned at send time, and it enters the log when accepted by the agent**: on POST the server assigns a message uuid and hands it to the CLI with the message; the message first becomes a **queued message** — like a draft, it is server-side in-memory state, cleaned up with the session, not in the log, and delivered via the POST response and the entry stream; only when the CLI reports that it has started processing it (`started` of `command_lifecycle`) is it written to the log and assigned a seq, so its position is the moment it actually entered the conversation. The CLI's replay of user messages only associates the transcript identity by uuid and produces no entry. The frontend renders no locally synthesized messages, and retries rely on a request-side idempotency key (borne by the in-memory table while queued, and by the log once in the log). The first message of a new session is an exception: it is written at seq 0 when the CLI reports `init`, without queueing, reusing session creation's idempotency and the log fallback of seq 0 carrying the idempotency key; a new session has no output before it, so its write position coincides with its acceptance position.
- **SSE pushes the entry stream** (the SSE event's `id` field is the seq; on disconnect it resumes by cursor), and the stream's lifecycle follows the session rather than the turn: status changes push only a `status` event and do not close the stream; turn-structure assembly (merging consecutive assistant messages, back-filling tool_result, updating tasks in place) is a pure function of the frontend projection layer.
- **The streaming preview (draft) is server-side in-memory state**, identified by the message's `message_id`, and when the message completes it is replaced exactly by the log entry with the same `message_id`; it does not enter the log and is lost on crash (consistent with agent memory).
- **Subagent messages are recorded in full** as entries carrying parent_tool_use_id; the frontend groups them by parent into collapsible sub-task cards, and the main timeline shows only the cards.
- The log is a **materialized view** of the transcript: it can be rebuilt by replaying the transcript, and is generated lazily on first access for old sessions; the remedy for drift is rebuilding, not two-way sync.
- **The SDK transcript's responsibility is limited to SDK resume** (agent memory), and UI-specific entries must not be mixed into it (they would be fed back to the agent by resume).

## Explicitly not adopted

- **Consolidating dedup logic without unifying message identity**: edge-case leakage from content comparison cannot be eliminated.
- **Writing to the log at POST time, positioned by send time**: the CLI defers messages delivered mid-turn to a tool boundary to merge them in, or queues them after the current turn; positioning by send time would place agent output produced after sending but before acceptance after this message, reading as if it were responding to it, and would also disagree with the position rebuilt from the transcript.
- **Using the CLI replay as the acceptance moment**: the first message of a turn is only replayed just before the agent's first output, so a message sent to an idle session would sit in the queued state through the whole startup and first-token latency.
- **Using the transcript directly as the UI log**: write timing is controlled by the SDK, so an identity cannot be assigned when the user sends a message; UI-specific entries would pollute resume; subagents' independent subpath is not isomorphic with UI ordering.
- **Dual-track old/new protocols behind a feature flag**: the frontend would have to maintain two sets of store logic in parallel.

## Consequences

- New UI event types must be typed at the write point; the rendering and reading sides do no semantic sniffing and no deduplication — the remedy for UI consistency problems is fixing the typing logic at the write point or rebuilding the log by replay.
- The coexistence of the event log table and the transcript table is intentional double writing: the former is the UI read model, the latter is agent memory; their content overlaps but their protocols and timing are not isomorphic, and merging the tables would bring back exactly the coupling this ADR removes.
- The transcript's eager flush (`docs/adr/0029`) serves only to keep SDK resume from losing context after a crash; UI reconnect snapshots are borne by the event log.
