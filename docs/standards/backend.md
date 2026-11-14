---
paths:
  - "lib/**"
  - "server/**"
  - "scripts/**"
  - "alembic/**"
  - "packages/**"
---

# Backend Python

## Concurrency

For the sources of cancellation and interruption in generation tasks and request handling, see "取消与中断的来源" (Sources of cancellation and interruption) (`#cancellation-sources`) in `website/docs/dev/architecture.md`.

### After an await, re-check the state's preconditions; if a precondition is stale, write the current state back or skip the side effect

In the window across an await, another path may already have changed the same state: the session re-enters running while idle settlement is being persisted; an epoch incremented after an acceptance failure makes an already-queued idle be judged stale; between the connection lock being released and the waiter waking up, the lock has already been removed. After the await returns, first re-check the epoch, lock identity or meta state, and write only if the precondition still holds. Preconditions also go stale when changing the state machine itself: after opening up an entry point in some state (e.g. also accepting sends while running), the state assumed by that entry point's existing failure branches has changed, so re-check them one by one; every read point of a counting invariant is separated by awaits, so first list all the async timings that read the count, then decide on the fix. In synchronous code, checking inside a lock, releasing the lock to do a time-consuming step (such as running project migrations), then re-acquiring the lock to write is the same window: re-verify the precondition within the lock acquisition that does the write, otherwise two concurrent operations both land on the old precondition, for example the same deletion record being restored twice, leaving an object that points to a nonexistent file.

### The event loop thread only does scheduling

Synchronous code along an `async def` call chain that does file I/O, hashing, large parsing or waiting on locks goes into `asyncio.to_thread` as a whole; where a lock already has an async version, switch to the async version directly. One blocking call on the event loop thread makes every request in the same process queue up: media files are sent in chunks, a single video passes through the event loop dozens of times, and the stall is multiplied. ruff `ASYNC` only checks direct calls in the body of an `async def` function and can't see through function calls; the whole call chain is guarded by the blockbuster guard in the tests (`tests/blocking_guard`), so passing lint does not mean there is no blocking.

Allowed when: one-off blocking on the application startup and shutdown paths, registered in `tests/blocking_guard/exemptions.toml` with the reason stated.

### Use `run_sync_transaction` when writing real files or creating temporary artifacts in a thread

In a request or task context, when putting synchronous code into a thread that writes, replaces or deletes real files, or creates temporary artifacts that must be handed back to the caller for cleanup, call `run_sync_transaction` from `lib/infra/async_thread.py`. It lets a thread transaction that has already started finish and its result become definite before propagating the cancellation. When a bare `asyncio.to_thread` is cancelled, `await` raises `CancelledError` immediately while the thread keeps running, and the caller's cleanup logic runs concurrently with the thread, with two kinds of consequences:

- Leaked temporary artifacts: temporary directories, zips and other artifacts the thread creates only after the caller's cleanup are never reclaimed.
- Succeeded externally, reported as failed internally: the cleanup in `finally` races with the `replace` in the thread; the file has already landed on disk, yet the caller treats it as a failure.

If the artifacts a thread produces can be used up within the thread, create and delete them within the thread. If they must be handed back to the async side, register each one in a list held by the caller as soon as the thread creates it, and the caller reclaims them from the list in `finally`: after `run_sync_transaction` is cancelled it waits for the thread to finish before raising the cancellation, and the return value is discarded along with it, so registration is the only way the caller sees the artifacts.

Allowed when: only reading files, or producing disposable intermediate results; use bare `asyncio.to_thread`.

### The bookkeeping section after a provider returns cannot be interrupted

"取消" (Cancel) in `GLOSSARY.md` requires that provider calls already sent run to completion as usual and their results become artifacts as usual. In code: from the provider returning or the artifact being committed until the executor returns, every thread call in this section uses `run_noninterruptible_sync` from `lib/infra/async_thread.py`, which waits for the thread to finish and hands the result back to the caller, and this cancellation disappears with it. Two incident shapes that have already occurred: the cleanup of the compressed copy of a reference image running concurrently with the thread and deleting the image that was just paid for along with it; the completion callback and the failure compensation executing at the same time.

It swallows cancellation, so the call site must be outside `asyncio.timeout` blocks and `asyncio.wait_for`, otherwise the timeout stops working.

Image, video and audio backends divide the work of writing to disk as follows:

- When the bytes returned by the provider are already in memory (`b64_json`, an SDK response, a fully read response body), decoding, writing to disk and transcoding all go into `run_noninterruptible_sync`. The b64 path of `lib/backends/image_backends/base.py::save_image_from_response_item`, shared by the image backends, already satisfies this rule; backends just reuse it and need not work around it themselves.
- In downloading from an artifact URL (`stream_to_file`), chunk writes stay in `run_sync_transaction`: at that point the artifact has not taken shape yet, cancellation rolls back the download as a whole (the `.part` is cleared), and there is no formed artifact to lose. Network fetching keeps interruptible semantics; channels using `with_artifact_retry` are constrained as a whole by a wall-clock budget (`asyncio.timeout`), and putting them in a non-interruptible section would disable the budget, so a download that trickles out data would never end. The step where the artifact takes shape, renaming `.part` to the finished file, is an atomic operation with negligible duration and uses `run_noninterruptible_sync`, so cancellation cannot land in the gap where the finished file is in place but the caller treats it as cancelled. Thread steps after the download returns (converting to PNG, writing to disk) are outside the budget and likewise use `run_noninterruptible_sync`.

This rule only protects thread calls within the section. Async writes within the section only guarantee that the write itself is not interrupted; cancellation propagates outward as usual and the subsequent steps are skipped with it: when the settlement write of `Ledger.record()` is cancelled, it waits for the write to land and then re-raises, and the artifact commit no longer runs; a bare `asyncio.shield` raises immediately when cancelled, and the write runs concurrently with the caller's subsequent handling. The timeout blocks of the generation pipeline are all on the provider-call side; at present only event-loop teardown can strike into this section. Before adding `cancel()` to running tasks, first use `run_noninterruptible_async` to enclose the whole section within a non-interruptible async boundary.

## Paths and deletion

### Before recursively deleting a directory that comes from arguments or configuration, confirm ownership

When the target of `shutil.rmtree` comes from a command-line argument, environment variable or configuration (such as a script's `--data-root`), confirm before deleting that the directory is owned by this tool: accept only an empty directory, or a directory carrying the sentinel file this tool writes when creating it; otherwise exit with an error. An argument can point anywhere, and if it mistakenly points at the real data directory, all user projects are deleted. See the `.arcreel-perf-bench` sentinel in `scripts/perf/harness.py`.

Allowed when: the target is a directory this process created with `tempfile`.

### IDs spliced into `:path` route parameters reject dot segments on the server

When IDs from user input or returned by third parties (such as provider model IDs) are spliced into routes like `/{id:path}`, the server-side entry points for adding, renaming and importing split on `/` and reject any segment equal to `.` or `..`; checking length alone is not enough. WHATWG URL normalizes `.`, `..` and their percent-encodings as dot segments, so `a/../b` has already become `b` by the time it reaches the server; frontend encoding can't block it, and an ID containing dot segments would read, modify or delete another object in the same scope. See `has_dot_segment` in `lib/catalog/model_id.py`.

## Deletion and restore

Every deletion within a project becomes a deletion record in the project's trash; restore only adds, never overwrites; see `docs/adr/0107-deletion-goes-to-trash-restore-only-adds.md`. Violating any of the rules below has the same consequence: content the user thinks can be recovered actually cannot.

### A deletion record captures the object itself and the metadata needed for it to be usable after restore; it captures only files the object exclusively owns

Create a deletion record whenever the object exists in the structure file, without requiring a non-empty list of attached files: an episode or storyboard with no media or source text still carries the title, hook and prompts the user wrote, and skipping it loses them permanently. The record must carry all fields needed for the object to be usable as usual after restore, not just those that identify it, for example the generation basis of historical versions; without them, a restored version can no longer be selected again. Before moving files into the record, exclude those still referenced by other existing objects, otherwise deleting one object makes another object's files disappear.

### Consume the deletion record only after the restored object satisfies the reader's preconditions and the associations are committed as a whole

Before landing, check the reader's preconditions inside the lock: the owning episode or project still exists, the name passes the reader's validation, and reference fields point to existing objects. When preconditions are not met, degrade per ADR 0107, or fail this restore and keep the record. Putting files back, writing the structure back and consuming the record happen in the same lock and write transaction; if any step fails, withdraw the files and keep the record; the structure write-back is not placed in the lock's exit section or in a callback outside the lock. If nothing can be added back after degrading, likewise return failure and keep the record. On violation, the restore appears to succeed, but the object can't be opened or isn't reconnected, the record is already gone, and the user cannot retry.

### When one deletion or restore spans directories, the database and multiple structure files, compensation for failure and cancellation covers every change

When moving a directory first and then committing the database record, a failed commit must move the directory back; see `move_back` in `lib/project/deleted_projects.py`. On cancellation the destination must not be lost either: register it in a list held by the caller (see "Use `run_sync_transaction` when writing real files or creating temporary artifacts in a thread"), or use `run_noninterruptible_async` to enclose the whole section within a non-interruptible boundary. The rollback transaction for structure files covers every file rewritten in this operation, including the version history `versions/versions.json`. Missing any one place leaves directories out of sync with task and session records, a new project with the same name inherits the old records, or the version history is permanently lost when registration fails.

### After files are moved into a deletion record, move away only the files in the record; do not `rmtree` the original directory

Between enumerating files and the final deletion, newly written content may appear in the original directory, such as project memory written by the agent or a generation result that just landed on disk. They are not in the record, and `rmtree` would hard-delete them irrecoverably. At the end, move away only the files in the record, and remove only directories that have become empty, using `rmdir`. The reason given in a `# hard-delete` annotation must hold even when concurrent writes still occur after enumeration.

Allowed when: all writers hold the same lock as the enumeration and deletion.

## Identity and references

### When changing an object's identity, rewrite the records keyed by or referencing the old identity along with it

An object's name, entry number, model ID and the absolute path of its directory are all identities. Write paths that change identity, such as restore with a new name, restore with a new number, or changing a model ID, besides changing the object itself, must also move records with keys derived from the old identity (the `project_key` the SDK derives for sessions from the project directory's absolute path, the artifact manifest's registrations by entry number) and settings referencing the old identity (project and global model settings keys, `model_settings`) to the new identity: first search for every holder by the old identity and list them in the PR. Records that are missed stay under the old identity: the agent reports it can't find the conversation when resuming chat, existing artifacts read as missing and the UI prompts the user to regenerate, and dangling model references are silently taken over when a new model with the same ID later appears. The old identity may already be taken by a new object with the same name (a newly created project with the same name shares a key with the old path), so the rewrite is scoped by the object's own records, for example by session_id, rather than replacing the whole key. When the identity changes outside the process (the data root moved to a different absolute path), align at startup. See `lib/agent/agent_session_store/rekey.py`, `lib/agent/agent_session_store/key_alignment.py` and `rename_project_model_refs` in `server/routers/custom_providers.py`.

## Windows compatibility

The main development platforms are macOS / Linux; the server (together with the workspace subpackages it depends on) must also complete project creation and the basic flows in a native Windows environment. CI only runs Linux, and the problems below all pass silently on Linux and appear only on users' Windows machines.

### When `O_NOFOLLOW` is unavailable for an untrusted path, verify the file's identity after opening

POSIX constants such as `O_NOFOLLOW` are obtained via `getattr(os, "<constant name>", 0)` (enforced by ruff TID251), which yields 0 on Windows, so `os.open()` no longer rejects symbolic links. Then only an `is_symlink()` pre-check is left, but there is a TOCTOU window between the pre-check and the subsequent `os.open()`: if the path is swapped within the window for a link pointing outside the data root, it results in unauthorized reads and writes, which is a security boundary problem.

Untrusted paths (paths whose directory entries the user or the agent can influence) are verified in two steps. Before opening, reject symbolic links level by level and constrain the path within the data root, because `os.lstat` follows links in intermediate directories; after opening, compare `st_dev` / `st_ino` using `os.fstat` and `os.lstat`. See `_resolve_business_file` and `_read_business_file` in `server/tool_runtime.py`, and the parent-directory identity check in `lib/artifacts/artifact_manifest.py`. Where this isn't possible, refuse the operation on that platform.

Allowed when: the path is generated by the server itself and no externally written symbolic links will appear in the directory, for example the project lock in `lib/agent/profile_manifest.py`, or artifacts the server atomically writes itself. For such paths a pre-check alone is enough.
