---
status: accepted
---

# Agent cost is computed per request from tokens and the model catalog, not taken from the cost the SDK reports

The embedded agent's cost uses the same rules as generation cost: a reference cost computed from token usage according to the model catalog's price tables and multipliers. The `total_cost_usd` and per-model `costUSD` reported by the SDK are estimated locally by the CLI from its built-in Claude price table, applying Claude prices even to non-Claude models; under a persistent `ClaudeSDKClient` they are also cumulative session values. ArcReel's agent mostly calls non-Claude models via third-party gateways, so this number matches neither the model nor the turn.

## Rules

- Price per request: each API request's usage is taken from `AssistantMessage.usage` (deduplicated by `message_id`, including subagents), each bucketed into its own context tier, with cache writes priced separately for the 5-minute and 1-hour tiers. Calls not visible in the stream (such as compaction) are derived as the difference between this turn's `model_usage` delta and the in-stream total, and the difference is priced as one request.
- Account per turn: one provider call record is written per model per turn, with cost being the sum of that model's request costs in the turn. This is an exception to provider calls being accounted per request; pricing is still per request; models with no usage in the turn get no record. Cache reads and the two tiers of cache writes are recorded in separate columns, and the definition of total input is unchanged.
- Usage is always interpreted per Anthropic's definition (`input_tokens` excludes cache), with no per-gateway correction. Deviations from gateways with different definitions are accepted given the reference-cost positioning.
- Channel price differences for agent calls are expressed only by the agent credential multiplier, a single level, default 1, a user preference; there is no judging from presets whether it is a subscription. The multiplier and entry binding come from the agent credential injected when the session started, not the active credential at accounting time (see `docs/adr/0017`).
- When a model name cannot be matched to a model entry, it is recorded as unpriced (see `docs/adr/0100`). The cost reported by the SDK is not stored, nor used as a comparison value.

## Considered Options

- **Use `total_cost_usd` or the sum of per-model `costUSD` (the old approach)**: non-Claude models get Claude prices; cumulative values also need differencing to attribute to each turn.
- **Have the CLI compute correctly directly via `modelPricing`**: only effective in managed settings or host mode, and the Python SDK exposes no corresponding parameter; cache writes can only have a single price, and differencing is still needed.
- **Price per turn using the `model_usage` delta**: simple to implement, but input size differs across requests in a turn, so the context tier cannot be determined and models tiered by input length would be underestimated; cache writes also cannot be split into the two tiers.
- **Have presets declare each gateway's usage definition and correct per vendor**: there are no real credentials to measure against, and the cost is only a reference, so maintaining these declarations is not worth it.

## Consequences

- Each session must maintain a `model_usage` delta baseline: when resuming a session, the first result after resumption is the starting point; it is reset to zero on a session reset; when the cumulative value drops, the current turn's value is treated as a single-turn value.
- A background subagent's spending is recorded to the turn in which it finishes, not the turn in which it started.
- Pricing calls not visible in the stream by difference is an approximation: when a turn has several such calls, the context tier is bucketed by their total, which may be too high; the first turn after resuming a session has no delta baseline, and such calls are not counted.
