---
status: accepted
---

# The usage record is a projection of provider calls; tasks and calls are one-to-many

The "Usage records" page needs to put queued / in-progress generation tasks and completed provider calls on the same timeline. The intuitive approach is to have both share one storage entity: either open an `api_calls` row as soon as a task is enqueued, or join the two tables on the read side into unified rows with a composite primary key. We decided to do neither: `api_calls` records only outbound requests that actually happened (the smallest unit of cost and usage, pointing via the nullable `task_id` to the task it served, with `purpose` describing its origin), `tasks` is responsible only for intent and scheduling, and "usage records" is a read model, not an entity. The in-progress section is supplied by tasks and terminal records by calls; the two are stitched into one list on the frontend, each keeping its own status vocabulary and refresh mechanism.

Rationale: money is spent per request, not per intent; a task will in future map to multiple calls (real retries, multiple units for reference-to-video), so welding records to tasks amounts to asserting one-to-one, and would put "queued then cancelled" rows into the ledger for requests that never reached a provider. Unified rows on the read side would need a composite primary key and an "in-progress section special case", which is patching over a write-side defect.

Consequences: `api_calls` needs `task_id`, `purpose` and `session_id` added, and text calls need to pass `user_id` through; `tasks.payload_json.api_call_id` is replaced by `api_calls.task_id`; call status gains `cancelled` so that user cancellations are not counted as failures.
