---
status: accepted
---

# Agent failure observations preserve evidence and do not infer root causes

> The presentation of the failure card's "default layer" in this ADR has been modified by "Default layer changed to a one-sentence conclusion (revision, 2026-10-04)" at the end; the rest remains in effect.

Agent startup errors in user feedback could lose their exception type because of an empty exception message, and SDK-synthesized turn errors were once rendered as agent answers because their structured marker was lost at the session event log's write point. We decided that agent startup failures and turn failures share a failure-observation contract of "minimal generic shell + masked raw payload": the shell carries only the stage, time, project, and session context that ArcReel knows for certain, and the raw payload preserves the exception chain, stderr, SDK messages / results, and upstream responses actually obtained; no enumeration of upstream error types is built, and upstream explanations are not promoted to ArcReel's root-cause judgement.

Turn failures are typed as system failure events at the session event log's write point and are no longer written as agent messages; startup failures are returned immediately via a structured API error and, together with turn failures, written to the existing diagnostic log. Turn failures are persisted via the session event log, while startup failures are not kept across a refresh; no failure table, dedicated file, or history query interface is added, and already materialized historical error events are not actively migrated or rewritten.

The raw payload covers only failure objects actually received or produced, and does not proactively copy prompts, normal streaming output, or the full transcript; context carried by the payload itself is still kept. Only secret values that could be directly used to impersonate an identity or incur charges are masked, such as API keys, auth tokens, cookies, passwords, and sensitive parameters of signed URLs; other paths, exception types, cause chains, stacks, and unknown fields are not truncated, translated, or polished.

The UI shares a two-layer failure card: the default layer shows the stage, the observed type / status, and the raw message with its source labelled, and makes clear that these facts are not the same as a root cause; the expanded layer presents the full masked payload and lets it be copied. Only actions unrelated to the root cause are offered — copy details, open agent settings, and, for startup failures, retry; the diagnostic log is still downloaded from the existing system settings entry and is not duplicated on the failure card. Turn failures do not automatically replay the original message, to avoid re-executing tool operations that already happened.

## Explicitly not adopted

- Classifying root causes or generating targeted fix suggestions based on error text.
- Maintaining an exhaustive mapping for unknown upstream errors, or degrading to an uninformative "unknown error". (The revised conclusion keys cover only values appearing in structured evidence; everything else falls back to a generic per-stage key; see the revision at the end.)
- Introducing transcript-association back-fill for low-value historical failed turns.
- Automatically replaying failed turns.

## Default layer changed to a one-sentence conclusion (revision, 2026-10-04)

Prompted by the agent message area section and backend data adjustment item 9 of "Spec: 前端基于 shadcn/ui 的重构与布局优化" (Spec: frontend refactor and layout optimization based on shadcn/ui) (#2991). In the original decision, the sentence "the default layer shows the stage, the observed type / status, and the raw message with its source labelled" is changed to: the default layer shows the stage title and a one-sentence conclusion, plus three actions — agent settings, copy diagnostic information, and retry for startup failures; the observed type, status, raw message with its source labelled, and the full masked payload all move into "details", which can be expanded and copied, with masking rules unchanged.

The one-sentence conclusion is localized from a `summary.key` newly added to the failure-observation shell. The key is derived only from structured evidence and maps fields already provided by the SDK and the runtime; it is not ArcReel's root-cause judgement:

- Turn failures take, in order, the value of the SDK's `AssistantMessageError` (except `unknown`), the result's `subtype` (`error_max_turns`, `error_max_budget_usd`), and the upstream HTTP status (mapped to the same set of `AssistantMessageError` values).
- Startup failures take the exception class, searching along the explicit cause chain: `CLINotFoundError`, `CLIConnectionError`, `ProcessError`, `TimeoutError`.
- When none of the above evidence is available, it falls back to `turn_failed` or `startup_failed`, and the conclusion states plainly "an error occurred at runtime / during startup; see details for the raw information".

The conclusion sentence only describes observations and gives no targeted fix suggestions; error text is not parsed. Historical failure events materialized before this revision have no key, and the UI displays them with the generic key, without migration. The decisions to preserve evidence, not infer root causes from text, and not auto-replay remain unchanged.
