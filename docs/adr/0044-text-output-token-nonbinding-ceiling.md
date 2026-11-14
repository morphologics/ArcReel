---
status: accepted
---

# The text output token limit is a non-binding safety valve; structured truncation is an actionable hard error

> Two parts of this ADR — "do not build a per-model output-limit capability table" and the manual tuning path for episode planning (lowering `planning_window_chars` / `planning_max_episodes`) — have been superseded by "Models register their output limit; episode planning sizes batches by it (revision)" at the end; the rest remains in effect.

Episode planning, script generation, and drama script_plan normalization each lowered `max_output_tokens` to 16000 / 32000 as a **functional budget** to "save money / limit time"; but the output size of these tasks is naturally constrained by the schema and content, and lowering it only cuts off normal output — whole-batch planning of a long drama (up to about 20 episodes including episode outlines) reliably hit the ceiling and was truncated, and instructor's Pydantic structured path threw `IncompleteOutputException`, which bubbled up as an undiagnosable error. We decided to reposition `max_output_tokens` as a **non-binding safety valve** (only guarding against degenerate model runaway), consolidating the three places into a single shared constant of 64000 in `lib/backends/text_backends/base.py` — a value high enough that normal planning/script output never triggers it; it triggers only for pathological oversized batches or when the user configured a model with low output capability. Alongside this, "truncation" is promoted to a first-class error: when **structured output** (the request carries `response_schema`) is truncated by the output limit, a typed `TextOutputTruncatedError` is raised (explicitly naming the current text model and suggesting switching to a model with higher output capability), and instructor's `IncompleteOutputException` is also normalized to this error; **free text** (no `response_schema`) keeps the log-only behavior of `warn_if_truncated` unchanged. The error is raised from `generate()` and naturally short-circuits the planner's validation retry loop — resending the same request that will certainly be truncated again is pointless. On top of this, episode planning appends its own manual tuning hint (lower `planning_window_chars` / `planning_max_episodes`, see `docs/adr/0032`).

## Explicitly not adopted

- **Pass `None` and leave it to the provider's default**: providers' defaults when `max_tokens` is omitted are not uniform and not guaranteed to be high (especially with relay proxies), and the logic is backwards — to make a validating provider error out for "can't reach it", you must actively ask for a large number so it refuses; `None` means "ask for nothing" and will silently get a possibly very low default, truncating just the same with no signal to switch models. With an Anthropic-compatible surface (`max_tokens` required), `None` would crash outright. An explicit high value is better in all three cases.
- **Adaptively shrink the window/episode count and retry on truncation**: implicit degradation by silently shrinking the batch contradicts "fail explicitly, let the user decide"; shrinking the window might also cut story arcs at worse boundaries. Instead the choice is handed back to the user (switch to a stronger model or lower the settings themselves).
- **Build a per-model output-limit capability table and set the cap dynamically per model**: the best fit, but it would need external data for every built-in and custom provider and is costly to maintain, far beyond the scope of this decision; the system currently has no such registry, and an explicit high constant is enough.

## Consequences

- The output limit is consolidated into a single shared source of truth; planning / script / normalize all reference the same constant, and adjustment changes only one place.
- Structured-output truncation is a hard typed error, free-text truncation stays a soft warning — the two behaviors are separated, and the blast radius is limited to calls carrying `response_schema`.
- For the planner's manual tuning mechanism (`planning_window_chars` / `planning_max_episodes`) to really be usable, settings writes must accept these values; together they form the user's two self-service paths after a truncation error (switch model / shrink the batch).
- 64000 is a non-binding safety valve, not a precise budget: when the user configures a model with output capability below it, truncation errors will occur more often; this is a deliberate "fail explicitly, switch model", not a defect.

## Models register their output limit; episode planning sizes batches by it (revision)

The batch size for episode planning is decided by the system and is not adjustable by the creator (see `docs/adr/0032`), so the system sets it according to the model's output capability. Output capability is a property of the model and is registered with the model: the model configuration carries an optional output limit — built-in models maintain it in the registry, and models of custom providers have it filled in by the user; episode planning decides the number of episodes per batch from it.
