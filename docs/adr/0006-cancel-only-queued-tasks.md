---
status: accepted
---

# Cancellation is only available for queued tasks; running tasks always run to a terminal state

Cancellation of generation tasks is only available for `queued`. Once a task has been claimed by a worker and entered `running`, it cannot be cancelled: provider calls already sent run to completion as usual, and the results become outputs as usual. Cancellation requests for running tasks are always rejected, and the task state is unchanged. This is consistent with the glossary definition (`GLOSSARY.md` "取消" (Cancel)).

Our reason for this is the cost model. ArcReel's media generation is billed per **provider API call**, and once a call is sent, the provider side keeps generating and charging. Interrupting the connection locally can only throw away the result and saves no cost; the user paid for the call but gets no output. A queued task has not sent any call yet, so cancelling it is what actually saves money. After a running task finishes, its output stays in the version history; if unsatisfied, the user can switch back to an older version or regenerate.

## "Cancelling running tasks" was once implemented, then withdrawn

The earlier implementation allowed cancelling running tasks: the cancel API wrote the task into a `cancelling` intermediate state, called `cancel()` on the running coroutine via the worker's in-memory "task id → asyncio.Task" map, and when the worker wrapped up it pushed it from `cancelling` to `cancelled`. Three supporting mechanisms grew around this path:

- **The `cancelling` intermediate state**: task state, batch member state and statistics, and the dedup partial index all gained an extra active state.
- **The "0 rows means switch to cancelled" protocol**: `mark_task_succeeded` / `mark_task_failed` are guarded by `WHERE status='running'`; when the task had already been changed to `cancelling`, the write affected 0 rows, and each worker exit used that to fall back to calling `mark_task_cancelled`.
- **Cancellation receipts**: when the coroutine was cancelled, the output might already be half-written or fully written, so a "receipt" was threaded all the way from output registration and the actual write to the task execution entry point, and when the task's terminal state landed as `cancelled` the already-written outputs were rolled back based on it.

Reasons for withdrawal:

- **It contradicts the cost model.** Cancelling a running task doesn't save the cost of calls already sent, yet rolls back outputs the user has already paid for.
- **Maintenance cost is disproportionate.** The receipt had to be threaded along every output write path; any new write path that missed the receipt would leave half a set of outputs after cancellation; every new terminal-state exit also had to remember to honour the 0-rows protocol. These constraints relied on callers' discipline and were hard to cover exhaustively with tests.
- **The need itself is narrow.** What users want to stop is usually tasks that haven't started yet, and cancel-all only ever clears queued tasks anyway.

## Consequences

- The state machine is `queued → running → succeeded | failed`, plus `queued → cancelled` (user cancellation, `cancelled_by='user'`; queued downstream tasks that depend on it are cancelled along with it, `cancelled_by='cascade'`). The only active states are `queued` and `running`, and the WHERE of the dedup partial index is likewise these two values. The upgrade migration converges leftover `cancelling` rows in the database to `cancelled`, and their queued downstream tasks are cancelled along with them as `cascade`.
- Behaviour of the cancellation entry points:
  - Single-task cancel and cancel preview: when the task is running, rejected with 409, returning a trilingual error message.
  - Project-level cancel-all: only takes `queued`; running tasks are unaffected and not counted; `skipped_running_count` only counts queued tasks that were claimed by a worker during the request and could not be cancelled.
  - Batch cancel (creative agent tools and remote MCP): cancels members still queued; running members run to completion as usual and are put under `skipped_running`.
  - Frontend: only shows a cancel button for queued tasks.
- Cascade cancellation only involves queued downstream tasks: a downstream task won't be claimed before its upstream finishes, so when a queued task is cancelled, its downstream is necessarily also queued.
- Service shutdown does not cancel running tasks: `worker.stop()` waits for running tasks to finish before exiting the main loop.
- **The fallback for process-level interruption is kept.** If a running coroutine receives `CancelledError` for process-level reasons (event loop teardown etc.), the worker uses `asyncio.shield` to call `mark_task_interrupted`, landing the task directly from `running` to `cancelled`. This is not a user cancellation; it just keeps the task from being stuck in `running` and being handled as an orphan on the next restart (see `docs/adr/0007`). The cancellation source it currently writes is `user`, which does not match the glossary's "internal system reclamation does not count as cancellation"; whether to change it to a separate source will be decided separately.
- Terminal-state writes are still guarded by `WHERE status=...` to prevent unauthorised transitions, but a write affecting 0 rows is only logged and is no longer reinterpreted as a cancellation.
- When emitting the success event fails, outputs are not rolled back: written outputs remain valid and the exception is raised as usual. Compensation on commit failure (not cancellation) is kept.
- Timeouts for running tasks are still backstopped by each video backend in `poll_with_retry` according to its own `max_wait`; on timeout it raises `TimeoutError` and the task is marked `failed`; the worker does not wrap an extra outer timeout.
- Do not reopen cancellation of running tasks in order to "let users stop expensive generations". If that need really arises, first solve provider-side cancellation and refund semantics rather than interrupting the connection locally.
