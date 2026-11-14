# Lessons Library

Records mistakes worth improving the environment over; maintained, counted and promoted by periodic sweeps; candidates are submitted via `lesson-candidate` comments on PRs. For entry format, counting, thresholds and exit rules see [`docs/agents/lessons.md`](agents/lessons.md).

<!-- reviewed-through: 740cd892c3052000d4b2900464be8643c1927ea0 2026-11-01 -->

## Results and exceptions of async tasks are picked up on every path

### Actor commands are completed by `SessionActor._dispatch` or `_closed_error()` on every exit path, with cancellation turned into `_ActorClosed`
Pattern: writing a separate complete logic in an actor handler or exit path, missing the branches for cancellation, commands dequeued in the same round, or commands enqueued after exit; or handing `CancelledError` to the caller as-is
Cost: callers waiting on `cmd.done` hang forever, or `CancelledError` slips past `except Exception` and escapes the HTTP request, leaving queued messages and idempotency keys behind (severity: routine)
Detection: judgement required
- #3139 · 2026-10-13 · Found in retrospective (Spec #3101): when the actor was cancelled, in-flight, queued and post-exit-enqueued commands got CancelledError or a false success; review of the same change (#3138) also found that commands dequeued in the same round as a message stream exception got the wrong completion value; not caught: no corresponding rule
- Demoted · 2026-10-19 · from docs/standards/agent-runtime.md "Completion of actor commands is funnelled only through `SessionActor._dispatch`"

### Shared tasks detached from their callers retrieve their own exceptions
Pattern: starting a shared computation with `create_task`, with callers only waiting on it via `asyncio.shield`; after all requests are cancelled (client disconnects) the computation errors, and nobody retrieves this task's exception any more
Cost: at every peak, misleading unhandled exceptions such as "Task exception was never retrieved" are left in the logs (severity: routine)
Detection: judgement required
- #3304 · 2026-10-19 · Post-merge fix: `RequestCoalescer.run` in `server/routers/_coalesce.py` errored after all requests disconnected, and the exception was never retrieved; detected by #3321's performance benchmark (#3320), 2b887a16 added `add_done_callback` to retrieve the exception; not caught: no corresponding rule

### When a dependency swallows cancellation, the background loop restores cancellation at its own checkpoint rather than relying on the stopper to resend repeatedly
Pattern: the background loop receives a native cancellation only once, the dependency (e.g. an anyio task group) merges it with its own cancellation and swallows it, and the loop goes on into a long sleep; the stopper switches to repeatedly resending `Task.cancel()`, which in turn interrupts the dependency's resource teardown inside `shield`
Cost: application shutdown hangs until the timeout backstop, tests occasionally get stuck; or the stopper returns before the connection is returned (severity: routine)
Detection: judgement required
- #3346 · 2026-10-25 · Post-merge fix: cancellation of the model catalogue's official sync arrived in the same round as anyio's scope cancellation during connection setup and was swallowed by the task group, so the sync slept into its one-day interval and `TestAuthIntegration` shutdown occasionally hung for 120 seconds (#3420, fixed by #3454's 187e1a5a8; #3557's c0232c7e0 stopped the lifespan tests from starting the official sync, #3479); the first version of that PR switched to repeatedly resending the cancellation, which interrupted the SQLAlchemy session's teardown inside shield; after review pointed this out it was changed to restoring cancellation at a checkpoint inside the task; not caught: no corresponding rule

### When adding a cancel entry point for running tasks, reject cancellation of tasks that have already passed the provider return point
Pattern: the cancel entry point only checks whether the task is still registered, not whether it has already received the provider result; the wrap-up phase (terminal-state marking, writing to disk, releasing the slot) can be interrupted, and the result is deleted as an in-flight cancellation; when the backend swallows a cancellation during the write to disk and wraps up as usual, the canceller doesn't re-check the terminal state after the task finishes either
Cost: billed generation results are lost; the ledger records success but there is no output (severity: data · security · real spend)
Detection: judgement required
- #2185 · 2026-10-25 · Post-merge fix: an endpoint trial run received a cancellation within the write-to-disk window, the result directory was deleted, and the ledger still showed success (#3442, fixed by #3494's e546d674a); integration review then reproduced that after the backend swallowed a cancellation during the write to disk, cancel and shutdown still deleted the successful result (ef29215c0); not caught: no corresponding rule (the cancel entry point predates the requirement in backend.md "The accounting phase after the provider returns is non-interruptible" covering cancel entry points; when #3181 added the rule, existing entry points weren't re-checked)
- Reviewed, not promoted · 2026-10-27 · backend.md "The accounting phase after the provider returns is non-interruptible" already requires the whole phase to be wrapped in `run_noninterruptible_async` before adding `cancel()`; this was an existing entry point predating the rule and has been brought into line with it. The trial run is the only cancel entry point for running tasks in lib and server, so adding another rule would have no change to hit during review
- #3494 · 2026-10-26 · Post-merge fix: after the trial run rejected a late cancellation it returned 404, and the frontend still cleared its state per "404 means the run no longer exists" and showed it as cancelled, so the user couldn't see the billed result the server had kept (#3498, fixed by #3530's d251541dd; 11ab19aac added non-terminal resume polling and generation checks); not caught: no corresponding rule
- Reviewed, not promoted · 2026-11-01 · The new record is that the cancel interface gave 404 the meaning "too late, the result is still kept" and the frontend caller didn't follow; this falls under general.md "A value's format or range is defined in only one place, and producers, validators and consumers all reference it" (promoted 2026-10-30, later than #3494); the trial run is still the only cancel entry point for running tasks in lib and server

## Changes don't cover all sites of the same kind

### Consistency checks enumerate from the members themselves, so new members missing both the marker and the registration are also found
Pattern: the check only compares two lists, "members with the marker" and "the registry", so when a new member misses both the marker and the registration, neither side sees it
Cost: enqueue methods bypass the action layer; there is no busy marker on submit and no dedup hint for duplicate submissions (severity: routine)
Detection: mechanical (proposed `api.enqueue-methods.test.ts`, identifying enqueue methods by request shape rather than only by the `@enqueue` marker)
- #2872 · 2026-10-22 · Post-merge fix: `submitAssetSheetBatch` had neither `@enqueue` nor an entry in `ENQUEUE_METHODS`, and `AssetSheetBatchDialog` called it directly, bypassing the action layer (introduced by 721d2bd5, found in #3356 review, followed up in #3357, fixed by #3372); not caught: check not wired up

### An index that decides a file is "unreferenced" enumerates from every entry point that reads files, and compares paths by file identity
Pattern: when building a new reference index, only the formal output registry is enumerated, missing workflow drafts and quarantined files, queued task records and legacy compatibility paths; or paths are compared by request path string, so on case-insensitive or Unicode-normalising file systems an alias of the same file is judged unreferenced
Cost: the generic delete entry point moves in-use files into the project recycle bin, project references immediately break, and later tasks report file not found (severity: routine)
Detection: judgement required
- #3418 · 2026-10-25 · Found in review: the file view's reference index successively missed four categories — NFC/NFD and case aliases, planning drafts written by `write_quarantine`, queued `grids/<id>.json`, and legacy `script_plan_*.md` — patched over several rounds (d554c9657, c55bf3e96, 61f63de53); the alias regression tests are skipped on case-sensitive, non-normalising file systems and don't run on Linux CI; not caught: no corresponding rule

### Resume replays the submit-time context, and the layers stacked on the client work from the same context
Pattern: resume switches the client's base back to the submit-time value, but the path override layer stacked on the transport, which recognises requests by the current base prefix, isn't kept in sync and sends requests it doesn't recognise unchanged
Cost: when only the base path was changed while a task was in flight, after restart-resume the request goes to the original path and is judged expired, and the paid video generation can't be retrieved (severity: data · security · real spend)
Detection: judgement required
- #3189 · 2026-10-25 · Post-merge fix: after a custom provider changed only its base path, resume requests didn't hit the path override, returned 404 and were judged expired (#3443, fixed by #3494's c45b2aa95); not caught: no corresponding rule
- Reviewed, not promoted · 2026-10-27 · Only one layer, `lib/custom_provider/path_overrides.py`, rewrites requests by base; after the fix it matches on the submit-time base and has a resume test; it requires all four conditions — custom provider, task in flight, only the base path changed, resume happening — so a standard written for it would hardly have any change to hit during review

### When the same flow has a separate page implementation per mode, adding or changing one side is checked against the other side's interactions
Pattern: the reference-to-video confirmation page and the drama content confirmation page are implemented separately, each with its own editable fields and post-confirmation destination, and interactions added on one side are missing on the other
Cost: on the reference mode confirmation page the source text is read-only, so when the source text isn't acceptable the user can only re-plan; after confirming the split it automatically expands the agent panel and prefills a continuation message, the opposite of the web full-flow design (severity: routine)
Detection: judgement required
- #1546 · 2026-10-25 · Post-merge fix: the reference script confirmation page rendered `source_text` as read-only, while drama's `ScriptReviewGate` allows editing the source text (#3448, fixed by #3492's 3308325ea); not caught: no corresponding rule
- #2872 · 2026-10-26 · Post-merge fix: the reference script confirmation page's `handleConfirmed` unconditionally called `prefillAssistant`, while `ScriptReviewGate` only prefills on the "交给 Agent" (Hand to agent) button (#3496, fixed by #3512's dfae750b6); not caught: no corresponding rule

## Scope of exception catching and propagation

### Structured exceptions carrying user guidance are let through separately before existing broad catches, and propagated to every entry point
Pattern: adding an exception carrying user guidance (e.g. `ModelNotInCatalogError`, inheriting `ValueError`) and handling it only at some entry points, while existing `except Exception` clauses on the other call chains swallow it into a generic error
Cost: the user doesn't see guidance like "go to provider settings and reselect the model" (severity: routine)
Detection: judgement required
- #3248 · 2026-10-19 · Post-merge fix: four paths — agent tool video capability lookup, draft promotion, post-upload generation overview, and image-to-image check — swallowed ModelNotInCatalogError (#3300, fixed by #3314's c6040abf); not caught: no corresponding rule

### Unexpected exceptions caught by a broad catch-all are reported as task failures, not wrapped in an exception type meaning an expected rejection
Pattern: at the end of a handler, `except Exception` wraps all exceptions in an "expected rejection" class, and downstream maps rejections to "modify the input"; internal errors and genuine provider rejections take the same exit
Cost: users and external agents are directed to modify their input when a retry would actually succeed (severity: routine)
Detection: judgement required
- #2085 · 2026-10-30 · Post-merge fix: the text task handler wrapped any exception as `TextGenerationError`, the problem code landed as `generation_refused`, the read side then completed it as `fix_input` (the completion added by #2109), and even backend construction failures prompted "please modify the input" (#3483, fixed by #3548's eb8d5bda1); not caught: no corresponding rule

### A shared resolver expresses "not found" with only the one agreed exception, and the fix goes in the resolver
Pattern: when adding a new resource branch to a shared resolver, a lower-level tool throws a different exception type (e.g. `ValueError`) for malformed input while callers only catch the agreed `KeyError`; once found, the catch is added in only one caller, and the other callers are still blown through
Cost: one bad ID turns the whole batch into `internal_error`, and the legitimate entries in the same batch fail along with it (severity: routine)
Detection: judgement required
- #2361 · 2026-10-30 · Post-merge fix: the derivative branch threw `ValueError` via `split_derivative_artifact_id`, `edit_images` only caught `KeyError`, and the whole batch landed as internal_error; #3487 only added the catch in the `inspect_images` caller, #3488 was opened separately, and #3548's 448f71cb6 changed the shared resolver to convert it to `KeyError`; not caught: no corresponding rule

## Repository conventions and automation not wired up to checks

### Regression tests bundled with skill scripts are wired into the gate and CI
Pattern: script tests such as `.agents/skills/*/scripts/test_*.sh` don't belong to any domain of `scripts/gate.py` or CI; after changing the script under test, only the one directly related test was run, other tests extracting the same function were already broken, and the gate and CI passed anyway
Cost: skill script regression tests silently stop working and this is only discovered at the next change (severity: routine)
Detection: mechanical (proposed: add a domain to `scripts/gate.py` covering `.agents/skills/**/scripts/test_*.sh`, and wire it into CI)
- #3393 · 2026-10-22 · Self-check: 02445565 added a `cr_review_skipped_body` dependency to `cr_walkthrough_rest`, and when `test_quota_alerts.sh` extracted that function jq reported it undefined (exit code 3); `gate.py --changed` only hit the tests and conventions domains, CI was all green, and only 0efd7d2b fixed it; not caught: check not wired up

### External skills keep a single master copy only in `.agents/skills/`; `.claude/skills/` holds only symlinks pointing to it
Pattern: when installing a skill with the tool's own install command (e.g. `react-doctor install`, `skills add -a claude-code`), the tool writes a physical directory copy into `.claude/skills/`, overwriting the existing symlink, or installs another copy in the subdirectory the command runs from (e.g. `frontend/`), so the same skill exists with multiple contents
Cost: master and copies diverge, the skill version the agent reads is indeterminate, and updates are missed when following upstream (severity: routine)
Detection: mechanical (proposed `scripts/audit_conventions.py`: `.claude/skills/*` are symlinks pointing to `../../.agents/skills/<same name>`, with skills used only in development allowlisted; `.agents/skills/` appears only at the repository root)
- #3407 · 2026-10-24 · Self-check: `.claude/skills/react-doctor` was a physical copy and `frontend/.agents/skills/react-doctor` a redundant copy; after reinstalling as recommended, the CLI again overwrote the symlink with a copy; root cause: the install tool writes copies by default and is unaware of this repository's conventions; not caught: check not wired up — `audit_conventions.py` doesn't check the shape of skill directories

### When adding a write step to a workflow-generated PR, update the integrity check for that PR at the same time, and rehearse it once with data that produces changes
Pattern: adding a step that commits new files to a workflow-generated PR such as the Release PR, verifying only the step itself, without updating that PR's change allowlist or having that PR run through CI with real changes; the problem only surfaced the first time it actually produced changes (see #3411)
Cost: the release is blocked and a fix PR has to be opened ad hoc (severity: routine)
Detection: mechanical (proposed workflow static check: paths that the workflow `git add`s on the release branch must be covered by the `release-integrity` allowlist)
- #3346 · 2026-10-24 · Post-merge fix: the catalogue snapshot automatically refreshed by the release PR wasn't in the `release-integrity` allowlist, and the new snapshot delisted an Ark model hard-coded in tests, so two checks failed on Release PR #3114 (#3409 allowed the snapshot path, #3410 switched to taking the default model from the catalogue); root cause: when the snapshot refresh step was merged, it wasn't rehearsed on a Release PR that would produce snapshot changes; not caught: no corresponding rule

### When a shared contract sample changes, trigger the validation domains of all readers
Pattern: frontend and backend tests share one input sample, and the gate, selecting domains by path, only covers the directory the sample lives in; when only the sample is changed, only one side runs, and the other side's contract tests that read it are skipped
Cost: when the frontend and backend contracts diverge, the gate and CI still let it through (severity: routine)
Detection: mechanical (proposed `tests/unit/scripts/test_gate.py`: shared sample paths select both the frontend and backend domains)
- #3492 · 2026-10-26 · Found in integration review: the newly added `frontend/src/test/contracts/reference-unit-add-requests.json` only triggered the frontend domain, and the backend tests reading it didn't run when only the sample was changed; 2700f4a31 added the path to the domain-filter of `scripts/gate.py` and CI; not caught: check not wired up

### After bulk-rewriting test files with a script, verify that every deleted test was deleted intentionally
Pattern: when replacing whole stretches of test code by anchor slicing, the range captured test classes beyond the insertion point; the deleted tests are no longer collected, and the full test run is still all green
Cost: existing regression tests silently disappear, and the drop in coverage is invisible (severity: routine)
Detection: mechanical (proposed: the gate compares `pytest --collect-only` node IDs between base and HEAD and lists the disappeared nodes, with moved or intentionally deleted ones explained in the PR; comparing only counts would let "delete one, add one" through)
- #3543 · 2026-10-30 · Self-check: slice replacement accidentally deleted three classes in `tests/unit/server/test_auth.py` — TestCreateAndVerifyToken, TestCheckCredentials, TestEnsureAuthPassword — restored after comparing each file's collection results (the final version 249b7e622 has all three classes); not caught: no corresponding rule (the gate doesn't compare collection counts)

## Unclear ownership of frontend async state under scope switches and overlapping operations

### When two pieces of data are invalidated at different times, each uses its own AbortController
Pattern: requests attached to the wrong cancellation scope: project-level data (session list, skill list) and session-level loading share one controller, so one session operation discards slow-responding project-level data as stale; or project-level requests aren't included in the project cancellation scope, and after a project switch late responses overwrite the new project's data
Cost: the UI stays on the old list, or shows another project's data (severity: routine)
Detection: judgement required
- #1280 · 2026-07-24 · Post-merge fix: the terminal-state session list refresh wasn't included in the project cancellation scope, and after a project switch a late response overwrote the new project's session list; not caught: no corresponding rule
- Demoted · 2026-10-19 · from docs/standards/frontend-async.md "Cancellation scopes are divided by data lifecycle"

### In list UIs, write-operation occupancy lives in a layer that isn't unmounted by view switches, and successful results are written back to the local list immediately
Pattern: list-type UIs record write-operation occupancy in component-local state and only trigger a refetch after a successful write; with overlapping operations, slow refetches or switching sections unmounting the component, occupancy is released early or lost, and records that are completed or in flight become operable again
Cost: restore or delete requests are sent again, and a failure is reported after a success message (severity: routine)
Detection: judgement required
- #3418 · 2026-10-24 · Found in review: the project recycle bin's occupancy was changed over 5 rounds: bucketed per record (d554c9657), clearing disabled while restoring (f62c8fa84), removed from the local list first after success and then occupancy released (ffb419671, 325a6921d), switching sections doesn't unmount (bb1f14414); root cause: occupancy and write results had no unified owning layer and were patched item by item; not caught: the first two rounds had rules that weren't followed (frontend.md "Protection state is bucketed by full scope; stackable ones use counts", "Controls disabled with resource occupancy check occupancy on open and on submit, and disable sibling controls at the same time"); the loss of occupancy on component unmount had no corresponding rule

## Determining and assigning object identity

### When deriving file names from names and claiming files on that basis, the claiming rule doesn't overlap with the derived names of other valid names
Pattern: files are named by derivation as `<name>_<n>`, while the name itself may end in `_digits`; when claiming, files whose "stem equals the name" are also counted as one's own, so another object's sequence-numbered file is treated as one's own and renamed, deleted or restored
Cost: another object's original image is carried off and becomes an orphan file, references dangle, and existing damage doesn't self-repair (severity: data · security · real spend)
Detection: judgement required
- #1730 · 2026-10-26 · Post-merge fix: with both products `Cup` and `Cup_1` present, renaming, deleting or restoring `Cup_1` would take `Cup`'s `Cup_1.png` with it (#3431, fixed by #3472's 914ee4f11: `renamed_file_stem` only recognises `name_<n>` when `allow_sequence`); not caught: no corresponding rule
- Reviewed, not promoted · 2026-10-26 · Claiming of sequence-numbered files is now funnelled through the single point `renamed_file_stem`, which rename, delete and restore all go through, and unit tests cover names ending in `_digits`; there is no second piece of logic in the repository that derives file names from names and then claims them, so a standard written for it would have no change to hit during review

### Entry numbers are only assigned, never reused; "used" is determined by a persistent high-water mark
Pattern: when taking a new number, only existing entries are avoided, or whether a number has been used is judged from records that are normally cleaned up, such as the recycle bin or version history; after deletion or cleanup an old number is reassigned, and edit clips, crops and history attached by number get hooked onto new content
Cost: old edit clips show as ready again, and old crops are silently applied to the new video (severity: routine)
Detection: judgement required
- #2876 · 2026-10-26 · Post-merge fix: `new_item_id` only avoided existing entries, so three paths — adding after deleting the last unit, the agent inserting a segment, and overwriting the formal script — would all get an old number back (#3430, fixed by #3471's b50f34c1a); within that PR, whether a planned number could be kept was then decided by cleanable deletion records, and review found that confirming again after emptying the recycle bin still reused the old number; not caught: no corresponding rule

### Before deciding path ownership by field value, normalise the value into a project-relative path first
Pattern: each time review pointed out one form of writing (dot segments, absolute paths, Windows drive letters, case), one more was patched, without first normalising the field value into a project-relative path and then deciding; or a defence that only arises when project files are hand-edited was accepted first and withdrawn in the next round
Cost: the same decision function was changed for 4 rounds in a row, each round requiring the full gate run, exhausting the review budget (severity: routine)
Detection: judgement required
- #3500 · 2026-10-26 · Self-check: the ownership decision for shared reference copying patched one path form per round for 4 rounds in a row, accepting and then withdrawing a directory symlink check along the way, and finally in 2497ed391 changed to normalising first and then deciding; not caught: there was a rule but it wasn't followed (general.md "Defend only against normally reachable scenarios")

## Tests don't reach the real contract

### The frontend and backend's interpretation of the same request is constrained by one shared sample, not fixed separately by each
Pattern: frontend tests only assert "a request was sent" or pin the frontend's own restriction, while backend tests pin a different interpretation; each side goes green on its own, but the contracts are opposite
Cost: a user-visible feature is wholly unusable on main, or the frontend blocks an operation that both the backend and the Spec allow (severity: routine)
Detection: judgement required
- #1956 · 2026-10-25 · Post-merge fix: adding a unit in reference-to-video always sent an empty body, the backend rejected it with 409 needs_replan, and an episode started blank couldn't create its first unit (#3437, fixed by #3492's 4c50d24ae; the request body samples were moved to `frontend/src/test/contracts/`, shared by frontend and backend); not caught: no corresponding rule
- #2535 · 2026-10-25 · Post-merge fix: the frontend disabled deletion when only one storyboard frame was left, pinned by a frontend test, while the backend allowed deleting down to zero, covered by an integration test (#3438, fixed by #3492's bfd1a695c); not caught: no corresponding rule

### When changing the thread or context in which backends are constructed, cover the construction of every built-in backend with real SDK clients
Pattern: moving construction into another execution context such as a worker thread, while the existing construction tests all replace the SDK client factory, so they can't detect the SDK construction's own requirements on the thread or event loop
Cost: all generation for some provider fails before the request is sent, only discovered at acceptance (severity: routine)
Detection: mechanical (proposed pytest: via `assemble_backend` without replacing SDK clients, parameterised construction of all built-in backends)
- #3170 · 2026-10-26 · Post-merge fix: from 4aae99f2e on, built-in backends were constructed in `asyncio.to_thread`, `xai_sdk.AsyncClient` couldn't get an event loop, and Grok text, image and video all failed (#3482, fixed by #3494's ce46d4f44, which also added the parameterised construction test); not caught: check not wired up (the Grok tests all replaced `create_grok_client`)

### Read-only tests of preview endpoints compare the bytes and file set of project files
Pattern: preview reuses the submit path's loader, which performs a compatibility migration and writes back when reading; the test only asserts that no task was enqueued, so it can't detect that the preview wrote formal files
Cost: merely opening or cancelling the confirmation dialog causes project writes the user didn't confirm (severity: routine)
Detection: mechanical (proposed test: all files of a temporary project are byte-identical before and after preview)
- #3512 · 2026-10-27 · Found in review: the batch preview of reference mode "补齐视频" (Fill in videos) wrote back a duration compatibility migration via `load_script` (#3503's implementation, fixed by c04716750); not caught: no corresponding rule

### Every branch of an async entry point has at least one test that runs through with real files, so the blocking guard can cover it
Pattern: some branch of the entry point relies entirely on test doubles in tests or has no test, and the synchronous file I/O and digest computation in the branch stay on the event loop; neither lint nor the blocking guard reports it
Cost: during batch rendering the event loop is occupied, the endpoint takes tens of seconds, and the frontend gets 502 (severity: routine)
Detection: judgement required (the I/O part is judged mechanically by the blocking guard, provided the branch has a real-file test; the guard can't judge pure-CPU stretches)
- #1892 · 2026-10-29 · Post-merge fix: the reference-to-video branch of `WorkflowPlanner._video_admission` synchronously read the output inventory and computed digests on the event loop, which together with per-unit parsing in cost estimation (#1786) and per-frame decoding of the contact sheet (#2911) occupied the event loop, and the endpoint took 27–88 seconds (#3520, #3532's e0c4887b8 moved it into a worker thread, 985c4bf5f added a real-file test); not caught: check not wired up (the blocking guard only intercepts call chains the tests reach, and this branch had no real-file test)

### The mirror layer and input layer of an overlay editor share one set of typography classes, and tests assert both layers' typography match
Pattern: the coloured mirror layer (`pre`) and the transparent input layer (`textarea`) each have their own typography classes; after removing one of the classes, browser default styles or preflight make the two layers' fonts diverge; the test only asserts the absence of a certain class
Cost: the caret and selection drift from the visible text, more and more towards the end of the line (severity: routine)
Detection: mechanical (proposed: vitest asserts both layers share the same typography constant, or compare computed styles in a page-level test)
- #3067 · 2026-11-01 · Post-merge fix: after removing `font-mono` from the reference-to-video prompt editor, `pre` picked up preflight's monospace font while `textarea` inherited a proportional font, misaligning the caret with the coloured text (fixed by #3516's 740cd892c); not caught: check not wired up (the test only asserted that neither layer had `font-mono`)

## Project migrations treat input as already stable and validated

### Migration steps that rewrite inventory registrations also validate the import path with an enveloped archive exported by the previous release
Pattern: when a migration step shifts the basis of outputs, it is only validated on projects upgraded in place; on archive import the migration chain runs on a staging directory with no inventory, registrations in the envelope are restored as-is, and the change in basis interpretation makes existing outputs stale immediately on import; after the import path was added, it was then patched item by item for key changes, image moves and identical bytes
Cost: after importing an archive exported by an old version, storyboard images and grids referencing assets are all judged stale; the same fix went back and forth for 3 rounds (severity: routine)
Detection: mechanical (proposed migration test convention: a migration step returning `ArtifactBackfillOutcome` must have a freshness assertion "enveloped archive exported with the previous release's schema → imported by current code")
- #3472 · 2026-10-26 · Found in review: v18→v19 rewrote the basis for storyboard images and grids, and after importing a schema 18 enveloped archive the grids and storyboard images were all stale; changed to materialise the envelope registrations into a staging inventory before import and shift it along with the migration chain; afterwards there was one more round of rework each for the chain-head normalisation's key change losing content digests and for identical-byte derivatives being impossible to pair uniquely (41581966a); not caught: no corresponding rule (the migration test requirements in `docs/agents/project-migrations.md` at the time only covered in-place upgrade via `migrate_project_dir`)

### Freeze all source content before migration copying, and roll back source files together with references when the commit fails
Pattern: multiple objects cross-reference each other's files, the migration copies and overwrites targets in object order, then reads the next source from a path that has already been overwritten; when freezing happens only in memory and there's no rollback for overwrites on failure, retrying after a commit failure still cross-contaminates content
Cost: after the upgrade, objects point to someone else's content, and the original content survives only as a retained copy (severity: data · security · real spend)
Detection: judgement required
- #3500 · 2026-10-26 · Found in review: when Alice and Bob shared reference files bidirectionally, after migration Bob got its own original content rather than Alice's content it originally referenced; after injecting an OSError at commit time, a retry cross-contaminated again (#3428's implementation, fixed by 2497ed391, which also added full migration chain and failure-retry tests); after merge, when #3530 extended the same copying to asset images, integration review found again that the copy destination wasn't in the transaction snapshot, so after a commit failure the destination had been overwritten by the copy and the original content survived only as a retained copy (11ab19aac brought the destination and retained copy into the transaction and moved them back on failure); not caught: no corresponding rule (the original regression only asserted rollback of the project and inventory)
- Reviewed, not promoted · 2026-10-27 · This was a one-off splitting of shared files in the v5 namespace migration, which already has full migration chain regression tests; no other migration step copies and overwrites in object order, so a standard written for it would have no change to hit during review

### Before a migration uses names or paths from project files to build write targets, constrain them within the project directory as untrusted input
Pattern: the migration builds copy and move targets from asset names or field values in `project.json`, assuming these values have already been validated; the import package runs the migration chain before validation, and `..` segments in names carry writes out of the project directory. The source path had an out-of-bounds check; the target path didn't
Cost: importing a crafted project package can write or move files outside the project directory (severity: data · security · real spend)
Detection: judgement required
- #3500 · 2026-10-26 · Found in review: chain-head shared reference copying built targets from unvalidated asset names, and both the schema 5 staging tree and chain-head normalisation paths could write outside the project directory (#3428's implementation, fixed by 2497ed391: assets whose names contain path separators are no longer copied); after merge, #3530's integration review found again that shared file copying resolved the real path before checking whether it was a link, so source links were copied as ordinary files, and when the target directory was a link the copy was written outside the project (11ab19aac changed to checking links segment by segment and rejecting when the target directory is a link); not caught: no corresponding rule (backend.md "When an untrusted path lacks `O_NOFOLLOW`, verify file identity after opening" governs how files are opened, not targets built from field values)
- Reviewed, not promoted · 2026-10-27 · This only occurs in this one migration step, which now rejects names containing path separators at the entry point and has tests; all other migration steps' write targets are generated by the server itself. Keep counting first; if it occurs again, consider moving name validation of import packages ahead of the migration chain to eliminate it architecturally

## Tests unstable under full-load complete runs

### Tests depending on lazy-loaded modules finish loading the module separately first, then wait for the rendered result
Pattern: when verifying number formatting via a lazy-loaded chart, the cold module's transform, loading and data rendering are all squeezed into the query's default wait; a single file passes, but under full parallel runs the first assertion times out
Cost: the full frontend suite fails intermittently, blocking delivery of unrelated changes and steering investigation towards language or store leaks (severity: routine)
Detection: judgement required
- #2418 · 2026-10-26 · Reproduced on main baseline: the trend value assertion in `UsageRecordsSection.trend.test.tsx` timed out twice in the stage full gate; on failure the data, language and timing were all normal, and the table was still waiting for the lazy-loaded chart (#3500's 2497ed391 preloads the real module in `beforeAll`); not caught: no corresponding rule


### Tests that create concurrency with real threads assert only results that don't vary with scheduling, and confirm timing under full-load complete runs
Pattern: a test with real busy threads plus real stack sampling used a deduplicated count to indirectly prove the stack was captured every time, and the result split depending on where scheduling landed; the scale was only validated alone or in targeted loops, and timed out under full parallel runs; when review pointed it out per the timing rule, it was dismissed on the grounds that "the assertion is determined by a fake clock"
Cost: the full gate fails intermittently, blocking later stages; the same test was changed over 3 rounds (severity: routine)
Detection: judgement required
- #3532 · 2026-10-30 · Post-merge fix: the busy-thread stack sampling test failed intermittently under full parallel runs; #3544's 549c5e617 switched to per-iteration assertions not depending on the same stack, which then timed out under full load, and #3545's bf6b83566 shrank it to 2 threads × 20 rounds; within #3532 it had already been changed one round for only sampling the stack once (bad1ebd28); not caught: there was a rule but it wasn't followed (testing.md "Waits, retries and timeouts are driven through a clock seam or an event handshake", dismissed after review cited it)

## Ungrouped

### When rewriting flow criteria, confirm for each cross case that the conclusion is unique, and that cases blocked by old conditions still have somewhere to land
Pattern: when tightening or replacing a set of criteria (triage tables, inclusion and exclusion conditions, timeout rules), each is read on its own wording, and new conditions contradict existing conditions, table order or downstream fields in cross cases; or after removing a condition from the old criteria, the cases it used to block have nowhere to go
Cost: unattended flows reach different conclusions about the same object or hang indefinitely; review–fix goes back and forth for several rounds (severity: routine)
Detection: judgement required
- #3336 · 2026-10-21 · Found in review: the exclusion sentence of the wrap-up self-check conflicted with the "data · security · real spend" item, and the fix in turn made the values of "not caught" contradict the field name; the same issue was changed over 3 rounds (e8a8e977, 9ef829ba, 02ac08e0); after the zero-reference exit removed "the rule has existed for a full 90 days", PRs from before the rule was added were counted in the statistics window (fixed by 175d0af9); not caught: no corresponding rule
- #3342 · 2026-10-21 · Found in review: the `needs-info` timeout rule in the triage table contradicted, in cross cases, the first-match table order, the pre-check path, the activity determination and the status label replacement; the same timeout rule was changed over 5 rounds (71e1b126, e7014af5, aa38cf9c, b2346830, 3a513b24); not caught: no corresponding rule

### To judge whether CodeRabbit has covered the current HEAD, go by this HEAD's review-completed signal, not just the walkthrough
Pattern: when an incremental review only replies under existing discussion threads and doesn't post a new review body, CodeRabbit has already set this HEAD's commit status to "Review completed", but the walkthrough is stuck at "Currently processing"; poll.sh only looks at the walkthrough and keeps reporting `is_in_progress=true`
Cost: the review loop waits idly until the timeout threshold, and decidable coverage is only obtained by a manual `@coderabbitai review` (severity: routine)
Detection: mechanical (proposed: poll.sh also reads CodeRabbit's commit status on HEAD, with testdata cases added)
- #3359 · 2026-10-22 · Review loop self-check: after pushing 38bb81b0, the CodeRabbit status was already success / "Review completed", and the walkthrough only updated after a manual trigger; not caught: check not wired up

### Derived results recomputed from current state on high-frequency paths are reused when inputs haven't changed
Pattern: high-frequency paths such as claiming, requests and polling recompute derived results from current state (content hashes, inferred providers, rebuilt snapshots) without first comparing cheap identifiers of the inputs (file stat, revision number), so the cost multiplies with call frequency and input size
Cost: the event loop is saturated, all HTTP requests in the same process queue up, and media download stalls are amplified (severity: routine)
Detection: judgement required
- #3147 · 2026-10-13 · Incident retrospective: on every claim cycle the worker inferred the provider from current state and recomputed asset hashes for queued reference-to-video tasks, with cost growing linearly with the queue length; not caught: no corresponding rule
- Demoted · 2026-10-19 · from docs/standards/backend.md "Derived results are reused when inputs haven't changed"

### Function-type options of custom hooks go into the dependency array, not exempted via ref
Pattern: when a custom hook accepts function-type options such as selectors and callbacks, it stores the latest value in a ref to get around exhaustive-deps, silently absorbing unstable references passed in by callers
Cost: callbacks may capture stale state, with no way of noticing (severity: routine)
Detection: judgement required
- #1648 · 2026-08-05 · Batch retrospective: a hook's function-type options were stored in a ref to exempt them from dependencies; not caught: no corresponding rule
- Demoted · 2026-10-19 · from docs/standards/frontend-async.md "Hook function-type options go into dependencies, not into refs"

### Requests that the model declaration shows can't be sent or can't be used are rejected before the billed request
Pattern: a newly integrated backend only checks against the model catalogue declaration (reference image count bounds, output format) when constructing the request or writing the output to disk; over the limit it truncates, and when the output can't be converted to a bitmap it bills first and then deletes; the orchestration layer still numbers "图N" (image N) by the endpoint's fixed cap, and the prompt points at images that weren't sent
Cost: the user pays for results that are bound to deviate or bound to be unusable (severity: data · security · real spend)
Detection: judgement required
- #3361 · 2026-10-24 · Found in review: `openrouter-images` reference images over the limit went back and forth between truncation and erroring over 3 rounds (d2a6e389 errors, 3cc3210d changed to the orchestration layer trimming by model cap while keeping backend truncation as a fallback, 8ea8f80e changed the fallback to erroring); the reference image lower bound and "included but no reference images declared" took two more rounds (3943af66, 3cc3210d); models that only output SVG were billed first and the output deleted after transcoding failed (cd9a20b9, 106308b0), and a505a825 changed it to rejecting before submit; not caught: no corresponding rule
- Reviewed, not promoted · 2026-10-24 · Pending #3405's ruling: whether the orchestration layer's "reference image trimming" stays a silent behaviour decides the scope of this rule

### i18n interpolation parameter names match the copy placeholders
Pattern: calling `t(key, { count })` while the copy placeholder is written as `{{total}}`; neither typecheck nor lint reports it, and the page displays the unreplaced placeholder text directly
Cost: unreplaced placeholders appear in the UI (severity: routine)
Detection: mechanical (proposed: a vitest or ESLint check comparing the parameter names of `t()` literal keys with each language's copy placeholders)
- #3418 · 2026-10-24 · Self-check: the parameter name `count` of the recycle bin summary copy didn't match the placeholder `{{total}}`, found when a page-level test asserted the text; not caught: no corresponding rule

### When running page-level suites in parallel worktrees, the preview server doesn't reuse someone else's process
Pattern: with multiple worktrees running in parallel on the same machine, `pnpm e2e` uses the default port, and Playwright's `reuseExistingServer` reuses a preview server already started by another worktree, testing a different build
Cost: the page-level suite gives a false green for this branch, and UI regressions are missed (severity: routine)
Detection: mechanical (proposed: make `frontend/playwright.config.ts` reuse an existing server only when an environment variable is explicitly set, and error directly when the port is taken)
- #3421 · 2026-10-25 · Self-check: all 55 lobby cases passed on the reused old build, and only failed after scenarios depending on the new button were added (ticket #3389); root cause: `E2E_PORT` wasn't set, so another worktree's 4173 was reused; not caught: there was a rule but it wasn't followed (CONTRIBUTING "Frontend page-level tests" requires staggering ports via environment variables when running in parallel, and the AFK implementer contract requires ports isolated from other worktrees)

### When reusing a security check's observation within a request, subsequent reads go through the constrained descriptor rather than reopening by path
Pattern: to reduce repeated opens, the result of a check done via `O_NOFOLLOW` or identity verification is shared within the request, but later steps stat or read by path; when the path is deleted or replaced with a symlink between the check and the read, the read follows the link and the decision reuses the old observation
Cost: formal files that should be judged missing or blocked are judged current or stale, and reads also follow links to files outside the project (severity: data · security · real spend)
Detection: judgement required
- #3502 · 2026-10-27 · Found in review: after sharing file observations within the request, video content proof still did stat and fingerprinting by path, so a file deleted after admission was judged STALE and one replaced by a symlink outside the project was judged CURRENT; it took two rounds of review to bring it back to descriptor reads (b8f9f85f4); not caught: no corresponding rule (tests only covered the normal path and the number of opens)
- Reviewed, not promoted · 2026-10-27 · Sharing observations within a request only occurs in the freshness decisions of `lib/artifacts/`, and after the fix tests for deletion and replacement with a symlink were added; backend.md "When an untrusted path lacks `O_NOFOLLOW`, verify file identity after opening" already requires identity verification after opening, and another rule would have no second change to hit during review

### Before adding an allow entry to the sandbox, check, per each platform's compilation method, the result of stacking it with existing deny entries
Pattern: adding `allowWrite` for a subdirectory under a directory that is wholly `denyRead`, thinking it only opens up writes; bwrap compiles it into a writable bind mount that overrides the upper-level read deny, while Seatbelt denies reads and allows writes; neither platform's result matches the intent
Cost: the agent's Bash crosses the kernel sandbox's read boundary, inconsistent with the system prompt (severity: data · security · real spend)
Detection: judgement required (unit tests can only assert the compiled result; the stacking semantics must be tested on each platform)
- #2348 · 2026-10-26 · Post-merge fix: in Linux deployments the agent's Bash could read and write the current user's memory directory, and on macOS it could write blind (#3481, fixed by #3505's c4b3c6e1a); not caught: no corresponding rule
- Reviewed, not promoted · 2026-10-27 · Sandbox settings are assembled in only one place, `server/agent_runtime/agent_access_policy.py`, and the fix removed this allow entry; changes to sandbox allow entries are rare, so a standard written for it would hardly have any change to hit during review

### When reminding to exclude files containing secrets from version control, also handle files already tracked by Git
Pattern: only using `git check-ignore` to decide and remind about adding an ignore rule; tracked files aren't reported by it, and an ignore rule doesn't stop their changes from being committed; only doing `git rm --cached` without adding a rule means the next `git add .` adds it back
Cost: plaintext API keys may still be committed with the code (severity: data · security · real spend)
Detection: judgement required
- #3505 · 2026-10-27 · Found in review: the setup skill's reminder only checked ignore rules, missing the tracked `.mcp.json` and `.arcreel/settings.json`, and the fix in turn missed the ignore rule after untracking; it took two rounds to converge (#3485's implementation, 10965b85d); not caught: no corresponding rule
- Reviewed, not promoted · 2026-10-27 · This only occurs in this one passage of explanatory text in the setup skill, now changed to distinguish tracking state; there is no second flow in the repository guiding users to handle secret files

### Pre-check failures of generation tasks explain the cause using the names shown in the UI, and problem codes are classified by way out
Pattern: a task fails before calling the model because the input is unacceptable, the message keeps internal prefixes and internal numbers, and the problem code lands as `internal_error` / `retry`; a no-op submission that only fills gaps gives no feedback at all
Cost: users can't find which item has the problem, retrying won't help, or they think AI writing is broken (severity: routine)
Detection: judgement required
- #2531 · 2026-10-30 · Post-merge fix: the prompt-writing pre-check, draft promotion and content confirmation for reference units threw bare `ValueError` for unacceptable units, with messages carrying the prefix "generate_episode_script 失败" (generate_episode_script failed) and internal unit IDs (called "视频单元 N" (video unit N) in the UI), and problem codes landing as `internal_error` / `draft_promote_failed` / the generic `conversion_refused`; no-ops only showed "已提交" (Submitted). Fixed successively via #3497 (#3512's 93bb56e07), #3508 and #3509 (#3547's 4b11617be, ae82d9e89), #3551 (#3556's 8b418ab5f, 288b407e9); not caught: no corresponding rule (general.md "Failure messages state the cause and next step first, with the raw text collapsed" was only added on 2026-10-08, after this was introduced)

### Diagnostic probes resident in production only use stack-sampling methods that hold the GIL and are thread-safe
Pattern: using a dump interface officially labelled "best effort" (faulthandler's delayed or all-threads dump) as an online watchdog; it traverses other threads' frames without holding the GIL, and crashes when it reads a half-updated frame while another thread is busy
Cost: a stall that should only have written a diagnostic log makes the process exit with a segfault, restarting repeatedly and clearing in-process caches (severity: routine)
Detection: mechanical (proposed: ruff banned-api forbids `server/` and `lib/` from calling `faulthandler.dump_traceback_later`, with the test plugin exempted)
- #3163 · 2026-10-29 · Post-merge fix: the stall probe's faulthandler delayed dump segfaulted while other threads were busy, and the production container restarted 11 times in two days (#3520, #3532's e0c4887b8 changed to a daemon thread holding the GIL that takes only the event loop thread's stack via `sys._current_frames`); not caught: no corresponding rule

### When adding a model to an existing series, declare each capability according to that model's own official documentation
Pattern: a new model inherits capabilities (reference images, last frame, etc.) wholesale from the series prefix or sibling models, and capabilities the official documentation states only for other models are declared too
Cost: the model enters buckets it can't serve, all tasks fail once it's selected, and quotes are also given based on it (severity: routine)
Detection: judgement required
- #2989 · 2026-10-30 · Post-merge fix: `grok-imagine-video-1.5-lite` inherited the series' reference image cap of 7, and after being selected into the reference-to-video bucket all real renders were rejected with INVALID_ARGUMENT (#3495, fixed by #3542's 0017df8f3); not caught: no corresponding rule

### When deciding whether a config item is set, treat an empty string in the process environment as unset; auto-generated credentials are persisted and then reused
Pattern: the container injects keys left blank in the template as empty strings, dotenv by default doesn't override existing variables, so values written later in the mounted file don't get in; startup logic treats the empty string as missing, regenerates and writes back, or randomly generates the signing key on every startup
Cost: after a restart the password is rewritten, all logged-in sessions are invalidated, and users are locked out (severity: routine)
Detection: judgement required
- #28 · 2026-10-29 · Post-merge fix: compose's `env_file` injected `AUTH_PASSWORD` as an empty string, so after restart the password was regenerated and `.env` rewritten; without `AUTH_TOKEN_SECRET` set, everyone was logged out on every restart (#3524, fixed by #3543's 249b7e622); not caught: no corresponding rule

### Consistency proofs have a landing for every version state reachable by normal operations, and only reject registrations that genuinely can't prove themselves
Pattern: proofs for export or import assume derived registrations (subtitles, presentation) always follow the currently selected media version; after the user uploads, regenerates or switches versions, legitimate registrations left by the system itself are judged unprovable, and the exception becomes a 500 via the catch-all
Cost: whole-package export and Jianying (CapCut) draft download for the episode keep failing, and the user can't recover on their own (severity: routine)
Detection: mechanical (proposed round-trip test: after performing upload, regenerate and version switch through the real entry points, run the export token, whole-package export and import preservation)
- #1866 · 2026-10-29 · Post-merge fix: after a unit switched its selected video, the subtitle and presentation registrations materialised from the old video were judged unprovable formal claims, and the export token and whole-package export returned 500 (community report #3521, fixed by #3541's f3e54ef45); the similar 500 from only regenerating the voice-over is left as follow-up; not caught: no corresponding rule

### Paths in external results use project-relative paths at the point of production, treating success values and errors alike
Pattern: rewriting only error messages in the exit adapter layer, while cause text in success values is carried out as-is; when doing string replacement after the fact, matching literal spellings misses the escaped form of `OSError` quoting file names via `repr`, forward-slash forms and boundaries on both sides of paths, patching forms round by round
Cost: remote hosts see the server's file system layout, or persisted location information gets corrupted; the fix is repeatedly reworked (severity: routine)
Detection: mechanical (proposed remote MCP integration test: scan every tool's success and error returns and assert they contain no absolute path of the data root)
- #2085 · 2026-10-30 · Post-merge fix: remote MCP error messages (#3484) and the migration failure cause in `get_workflow_plan`'s success value (#3507) carried server absolute paths; #3505 only rewrote errors, #3548's fa0e58c79 changed to relativising at the point where failures are recorded, and within the PR two more rounds were added for Windows `repr` escaping, single-quote escaping and the left boundary (69e34f99d); not caught: no corresponding rule

### When adding a type label to entries by ID, only add it for types that can be recognised
Pattern: when adding a type prefix to a field value, not checking all the entry types the field may hold, and the classification function defaults unrecognised values into some category
Cost: the UI calls grids storyboards (severity: routine)
Detection: judgement required
- #2418 · 2026-10-30 · Post-merge fix: the usage-record filter chips added the "分镜" (Storyboard) prefix to every segment, and grids displayed as "分镜 E3G01" (Storyboard E3G01); when #3489 distinguished video units, the remaining IDs were still classified as storyboard by default (#3491, fixed by #3549's c8a2ae950); not caught: no corresponding rule

## Promoted

### Web write routes mark writes as webui, and write exits called from the web send a hint by context source immediately after writing to disk
Destination: `tests/unit/server/routers/test_web_write_change_source_marking.py` (landed with #3548): checks via AST that every web write route sets `project_change_source("webui")` and doesn't hard-code another source; the hint timing of write exits is covered by integration tests
Pattern: the source relies on each route hand-writing the context, and new or changed write routes miss the marking or hard-code another source; write exits only write to disk without sending a hint, and the change is settled by polling or a catch-up scan as `filesystem`
Cost: the user's own saves trigger false notifications, focusing and automatic navigation (severity: routine)
Detection: mechanical
- #216 · 2026-10-30 · Post-merge fix: the old draft save endpoint hard-coded `source="worker"`; saving and discarding an episode draft (#2872) and the unregistered source file and non-whole-book source text branches (#2876, #2904) weren't marked webui, and draft and recycle bin writes sent no hint; #3512's integration review fixed content confirmation and asset application on the spot during inspection (c04716750), the rest were opened as #3510 and fixed by #3548's 287bfd9b6 and 69e34f99d, which also added the route source check; not caught: no corresponding rule

### The pre-operation impact preview and the actual decision are computed from the same basis
Destination: the comparison test in `tests/integration/lib/project/test_asset_merge.py`: on the same project, take the merge preview first, then perform the merge, and assert that the preview counts match the actual per-item freshness after the merge, covering the subclasses of names, grid-frozen reference images, derivative migration, and manually uploaded storyboard images and videos (landed with #3544, #3556; with 487ca1110's fix removed, `test_a_selected_uploaded_video_is_not_counted_as_outdated[True]` fails)
Pattern: the decision basis changes its interpretation (e.g. dropping names), but the pre-operation impact preview still estimates by the old interpretation; or the new interpretation is applied only to ordinary entries, missing subclasses whose basis comes from a different source
Cost: the stale count reported by the preview doesn't match reality, and users misjudge the operation's impact based on it (severity: routine)
Detection: mechanical
- #3472 · 2026-10-26 · Found in review: the asset merge preview estimated by name, so with the same image under different names it reported stale while it actually stayed current; after changing to count by actual freshness, it then missed cut-cell storyboards being judged by the grid's frozen reference image path, and derivative images becoming stale once moved away, requiring another round (41581966a); not caught: no corresponding rule

- #2904 · 2026-10-30 · Post-merge fix: the asset merge preview counted selected manually uploaded storyboard images and videos as stale, but after the merge they actually stayed current (the upload basis contains only the uploaded bytes); #3544's review only fixed the storyboard image branch (c29a3dc28), the video branch leaked onto main, followed up by #3550 and fixed by #3556's 487ca1110; not caught: no corresponding rule

### Static guards written by name or path cover every way of reaching the same target
Destination: docs/standards/general.md "Static guards that match by name or path have tests covering every way of reaching the same target" (the guard is itself the check, and whether it covers every form can't be decided by another check, so it is written as a review rule; guards are spread across the ESLint config, `scripts/audit_conventions.py` and their respective tests, so it goes in the general standard)
Pattern: lint guards or audit checks identify blocked modules or functions by source text and only recognise one form: imports only match the `@/` alias, while relative paths, `..` segments and re-exports still get there; delete calls only recognise `os.remove`, and `from os import remove as …`, `import os as fs` and `aiofiles.os` are not reported
Cost: the guard is useless in practice and also gives the illusion that it's already blocked (severity: routine)
Detection: mechanical
- #3310 · 2026-10-19 · Found in review: the toast guard and the primitives directory dependency boundary only matched the `@/` alias, and relative paths, `..` segments and re-exports were pointed out over three consecutive rounds (bd66ff6a, 29ff3121, ec553f95); not caught: no corresponding rule
- #3418 · 2026-10-24 · Found in review: the HARD-DELETE audit first missed function alias imports, then missed the `import os as fs` and `aiofiles.os` module aliases, completed over two rounds (051004b5e); not caught: no corresponding rule

### The agent write protection table is enumerated from the single definition of media
Destination: `tests/unit/server/agent_runtime/test_agent_access_policy.py` adds `test_protected_media_covers_every_public_media_location`: media directories readable by public endpoints and media files at the project root are all in `AGENT_PROTECTED_MEDIA_DIRS` / `AGENT_PROTECTED_MEDIA_FILES`
Pattern: the agent write protection table is hand-written per media directory, missing formal media outside those directories, and the hook and sandbox leave a bypass together
Cost: the agent can directly overwrite or delete referenced style images, bypassing the project recycle bin (severity: data · security · real spend)
Detection: mechanical
- #3421 · 2026-10-25 · Found in integration review: the four formats of root-level `style_reference.*` still allowed Write/Edit, and the sandbox's denyWrite didn't cover them either; 829e07d02 added `AGENT_PROTECTED_MEDIA_FILES` and tests (ticket #3390); root cause: the protection scope only enumerated directories without checking against actual upload paths; not caught: no corresponding rule

### When verified under a lock, then the lock is released and re-acquired for writing, verify again inside the writing lock
Destination: docs/standards/backend.md "After an await, re-check the state preconditions; if a precondition is outdated, write the current state or skip the side effect" gained the synchronous form of releasing and re-acquiring a lock; the restore pipeline now re-checks uniformly inside the lock before landing (`_require_unchanged`), funnelling all object kinds architecturally
Pattern: the check and the execution are split across two lock acquisitions, the lock is released in between to run a slow step (such as a project migration), the precondition isn't reconfirmed before writing, and a concurrent operation changes the precondition between the two acquisitions
Cost: the project is left with objects pointing to non-existent files, or duplicated inserted entries (severity: data · security · real spend)
Detection: judgement required
- #3421 · 2026-10-25 · Post-merge fix: two concurrent restores of the same deletion record, with the later one landing under a new name, leaving a ghost asset or storyboard frame (introduced by ticket #3383, followed up in #3423, fixed by #3426's 6b993016a); within the same PR, checking the owning episode before restoring an edit timeline was also first checked at retrieval (f5a7c91e9) and then added inside the landing lock (c6de50f1d); not caught: there was a rule but it wasn't followed (backend.md "After an await, re-check the state preconditions…", which literally only mentions await)

### Deletion records take in the object itself and the metadata needed for it to be usable after restore; files only those exclusive to the object
Destination: docs/standards/backend.md "Deletion records take in the object itself and the metadata needed for it to be usable after restore; files only those exclusive to the object"
Pattern: on deletion, a non-empty attached file list is made a precondition for creating a record, or only a few fields identifying the object are written into the record, or files still referenced by other existing objects are moved away too
Cost: titles, hooks and prompts of episodes and storyboard frames without media are permanently lost; restored historical versions can no longer be selected back; deleting one character makes another character's reference audio disappear (severity: data · security · real spend)
Detection: judgement required
- #3421 · 2026-10-25 · Found in review: deletion records for historical versions only stored the prompt, so after restore video and audio versions couldn't be selected; 829e07d02 changed it to save the original version metadata (ticket #3381); not caught: no corresponding rule
- #3426 · 2026-10-25 · Found in review: after deleting a newly created episode with no source text and no generated content, the project recycle bin was empty (ticket #3384); when overwriting the formal script, old entries without media got no record; deleting a character moved files still used by other characters into the record; fixed by ba1e82f7f; the target of shared reference images after renaming is tracked by the open #3428; not caught: no corresponding rule (ADR 0107 requires every deletion to create a record, but the standards had no rule carrying that into implementation)

### Only consume the deletion record after the restored object meets readers' preconditions and its associations are committed as a whole
Destination: docs/standards/backend.md "Only consume the deletion record after the restored object meets readers' preconditions and its associations are committed as a whole"; "block restore while the owning object is still in the recycle bin" is tracked by the open #3427
Pattern: restore only checks the object's own identity conflicts, not whether the owning episode or project still exists; or after the file lands, the record is consumed first, and object associations are written back in the lock's exit section or a callback outside the lock; or after degrading, nothing was added back but the record was still consumed
Cost: restore "succeeds" but the object can't be opened or isn't reconnected, the record is already gone, and the user can't retry (severity: data · security · real spend)
Detection: judgement required
- #3421 · 2026-10-25 · Found in review: an edit timeline whose owning episode had been deleted reported `episode_not_found` after restore, and the record had already been consumed (ticket #3382); not caught: no corresponding rule
- #3426 · 2026-10-25 · Found in review: when restoring a last frame hit a failure writing the formal file, the record was already gone and the storyboard frame's last-frame field was still empty; reference audio had the same ordering (ticket #3388); when a structural record with no file degraded, it consumed the record but added nothing back; ba1e82f7f changed it to place the file and write back within the landing transaction; not caught: no corresponding rule

### When one deletion or restore spans directories, the database and multiple structural files, compensation for failure and cancellation covers all changes
Destination: docs/standards/backend.md "When one deletion or restore spans directories, the database and multiple structural files, compensation for failure and cancellation covers all changes"
Pattern: moving the directory first, then committing or rewriting database records; on failure only the database is rolled back and the directory stays in its new location; the thread's return value is discarded due to cancellation, so compensation can't get the landing location; the rollback transaction for structural files misses some file rewritten this time
Cost: the project directory and task and session records don't match, and a new project with the same name inherits the old records; when registration fails, version history is permanently lost (severity: data · security · real spend)
Detection: judgement required
- #3421 · 2026-10-25 · Found in review: when the record commit for a project deletion failed, the directory wasn't moved back, and 829e07d02 added `move_back`; when restoring a deleted project, cancelling move_out lost the landing location, and 23d212624 put it inside a non-interruptible boundary with `run_noninterruptible_async` (ticket #3389); not caught: commit failure had no corresponding rule; for the cancellation half there was a rule but it wasn't followed (backend.md "When writing formal files or creating temporary outputs in a thread, use `run_sync_transaction`")
- #3426 · 2026-10-25 · Found in review: the rollback transaction for deleting an episode didn't include `versions/versions.json`, so when registration failed the version history was permanently lost; fixed by ba1e82f7f; not caught: no corresponding rule

### After moving files into a deletion record, move only the files in the record; don't `rmtree` the original directory
Destination: docs/standards/backend.md "After moving files into a deletion record, move only the files in the record; don't `rmtree` the original directory"
Pattern: first enumerate files and move them into the recycle bin, then recursively delete the original directory; files newly written after enumeration don't enter the record but are hard-deleted along with it
Cost: concurrently written project memory is irrecoverably lost (severity: data · security · real spend)
Detection: judgement required
- #3421 · 2026-10-25 · Found in integration review: after project memory was cleared and recycled, `rmtree` would hard-delete newly written files; 829e07d02 changed it to no longer delete the original directory and added a boundary test with real writes (ticket #3381); not caught: there was a rule but it wasn't followed (the HARD-DELETE check requires an annotation, and the `# hard-delete` claim "only empty directories and symlinks remain" doesn't hold under concurrent writes)

### Writing to disk and transcoding after the provider returns go in a non-interruptible section, and shared helpers are no exception
Destination: promoted, see #3413 (make the shared write-to-disk helper satisfy the non-interruptible requirement by default, eliminating it architecturally; the written rule in the standards already existed but wasn't followed)
Pattern: a backend reuses a shared write-to-disk helper such as `save_image_from_response_item`, or transcodes itself with `run_sync_transaction`; the disk write itself completes, cancellation propagates outward as usual, and the subsequent transcoding and output commit are skipped
Cost: billed outputs are lost on cancellation or event loop teardown (severity: data · security · real spend)
Detection: judgement required
- #3361 · 2026-10-24 · Found in review: the first version of `openrouter-images` wrote to disk via `save_image_from_response_item` and converted to PNG with `run_sync_transaction`; 248abe7e changed it to decode, write to disk and transcode inside `run_noninterruptible_sync`; the shared helper itself still uses `run_sync_transaction`; not caught: there was a rule but it wasn't followed (docs/standards/backend.md "The accounting phase after the provider returns is non-interruptible")

### When waiting for an async process to complete, use its last step as the signal; don't poll state that is already visible midway
Destination: docs/standards/testing.md "When waiting for an async process, use its last step as the readiness signal"
Pattern: an async process runs in multiple steps (switch memory → persist → broadcast, or render → effect subscription, or data arrives → debounced refresh), and the test continues as soon as the first step is visible, interleaving with later steps; only the test that has failed is fixed, and similar tests keep the same waiting style
Cost: CI goes red intermittently, and the same root cause recurs across issues (severity: routine)
Detection: judgement required
- #3330 · 2026-10-21 · main CI failure: in the episode page scenario, the plan refresh only happened via debounce after `waitForEntrance` returned, and axe sampled colours while the button was fading in and reported color-contrast (fixed by #3347's 0202c760); not caught: no corresponding rule
- #3331 · 2026-10-21 · main CI failure: TestQueuedMessages built the stream after polling in-memory completed, while the persistence and broadcast of the CLI idle settlement hadn't finished yet, receiving one extra late completed frame (fixed by #3359's d97c9a6b); not caught: no corresponding rule
- #3352 · 2026-10-22 · main CI failure: when the source text box was found via findByRole, the effect subscribing to focus requests hadn't committed yet, and the request delivered right after was dropped (fixed by #3359's 2a79befe); not caught: no corresponding rule
- #3353 · 2026-10-22 · main CI failure: TestSendQueuedMessageNow read the tray snapshot after polling log entries, and read a jump-the-queue message that was already in the log but not yet dequeued (fixed by #3359's f7d1ca13); not caught: no corresponding rule

### To judge whether CodeRabbit has covered the current HEAD, exclude walkthroughs that skipped review
Destination: `.agents/skills/pr-ai-review-loop/scripts/poll.sh` adds `is_review_skipped`, and skip notices don't count towards `reviewed_current_head`; `test_walkthrough_head.sh` adds a case with a real skip notice; reviewers.md states that `@coderabbitai review` is sent when skipped
Pattern: when a PR is created as a draft or its author is a bot, CodeRabbit posts a walkthrough that skips review; poll.sh still reports `reviewed_current_head=true`, `is_in_progress=false` for it, and without checking the body, unreviewed is recorded as passed
Cost: one reviewer's coverage may be missed (severity: routine)
Detection: mechanical (poll.sh recognises the skipped-review marker)
- #3347 · 2026-10-21 · Review loop self-check: after switching to ready, poll showed CodeRabbit as having covered it; only reading the body revealed it was a draft skip notice, and real review only happened after a manual trigger; not caught: check not wired up
- #3359 · 2026-10-22 · Review loop self-check: during the draft period poll reported CodeRabbit `reviewed_current_head=true`, there was no review at all, `walkthrough_head` was empty, and the body was a draft skip notice; not caught: check not wired up

### When changing the source or handling interpretation of the same capability dimension across backends or paths, list all read points first, then change
Destination: docs/standards/general.md "When changing the source or handling interpretation of a capability dimension, list all read points and change them together" (the read points span the applicable paths of several standards — `lib/backends`, `lib/generation`, `server/services`, etc. — so it goes in the general standard)
Pattern: when switching the capability source to the effective capability with user overrides layered on, or uniformly changing silent discarding to erroring, only the conspicuous few places are changed, while other read points of the same dimension (the payload construction of other backends, test connection, pre-submit estimation, settlement metering) still work by the old interpretation
Cost: missing first frames or reference images are skipped and rendering is billed as usual; capabilities the user narrowed are let through in test connection and real charges incurred (severity: data · security · real spend)
Detection: judgement required
- #3264 · 2026-10-19 · Post-merge fix: capability overrides for user-added models under built-in providers were only wired into the planning layer, while the execution gate (#3302, fixed by #3312), DashScope and Kling request construction and first-frame gate (#3323), and metering, estimation and the Veo audio switch (#3324, fixed by #3327) still read the backend's own profile; not caught: there was a rule but it wasn't followed (ADR 0104, docs/standards/providers.md "Each capability dimension recognises only one source; don't write another copy of capability values elsewhere")
- #3312 · 2026-10-19 · Post-merge fix: "inputs the backend can't send now error" missed DashScope's silent first-frame discard, OpenAI/Grok skipping missing files via `exists()`, and Sora over-sending (#3313, fixed by #3314); the execution gate layered user overrides on, but test connection `trial_run` wasn't synchronised (#3326, fixed by #3327); not caught: no corresponding rule

### Before recursively deleting a directory that comes from a parameter or configuration, confirm the directory's ownership first
Destination: docs/standards/backend.md "Before recursively deleting a directory that comes from a parameter or configuration, confirm ownership first"
Pattern: a script or tool accepts a path parameter such as `--data-root`, and when rebuilding directly does `rmtree` on a subdirectory of it, without checking whether it created that directory itself
Cost: when mistakenly pointed at a real data directory, all user projects are deleted (severity: data · security · real spend)
Detection: judgement required
- #3304 · 2026-10-19 · Found in review: the performance benchmark `scripts/perf/harness.py` did `rmtree(data_root()/"projects")` on `--rebuild` or when the build marker was missing, and `--data-root` could point anywhere; 22f796f6 changed it to only accept an empty directory or one with the `.arcreel-perf-bench` sentinel; not caught: no corresponding rule

### Prefilled or default values are taken from the valid values in the context where they actually take effect
Destination: docs/standards/general.md "Prefilled and default values take valid values from the context where they actually take effect"
Pattern: a default value only holds for a certain provider or model, while the value it depends on (the selected model, the current provider) may be inferred or changed to another vendor; the default is given globally or from a fixed set, not validated against the context where it actually takes effect
Cost: new projects are written with combinations that are bound to fail synthesis; submissions with a manually entered model are bound to fail and leave orphan entries, and same-named models from other providers are billed at its price (severity: data · security · real spend)
Detection: judgement required
- #3283 · 2026-10-19 · Found in review: the first version of the fix corrected the prefilled voice by the global default speech model rather than by the explicitly specified model; cf672f7b changed it to use the model actually written; not caught: no corresponding rule
- #3264 · 2026-10-19 · Post-merge fix: ManualEntryDialog fixed 4 media types with text as the default, so submissions for providers without text, such as Kling and Vidu, were bound to fail and leave orphan entries (#3301, fixed by #3314's 9f0a77e4); not caught: no corresponding rule

### IDs spliced into `:path` route parameters have `.` and `..` segments rejected on the server
Destination: docs/standards/backend.md "IDs spliced into `:path` route parameters have dot segments rejected on the server"
Pattern: splicing IDs from user input or third-party responses into a `/{id:path}` route, validating only length; the WHATWG URL spec also normalises `%2e` as a dot segment, so frontend encoding can't stop it
Cost: `a/../b` reads, modifies or deletes another model under the same provider (severity: data · security · real spend)
Detection: judgement required
- #3264 · 2026-10-19 · Post-merge fix: adding and renaming provider_models only validated length, and model IDs containing `..` segments operated on other models (#3299, fixed by #3314's 7095518b); not caught: no corresponding rule

### When changing an object's identifier, rewrite records keyed by or referencing the old identifier together
Destination: docs/standards/backend.md "When changing an object's identifier, rewrite records keyed by or referencing the old identifier together" (the four occurrences are spread across `lib/agent`, `lib/project`, `server/routers` and `server/services`, all within backend.md's applicable paths)
Pattern: paths that change an object's identifier — restore under a new name, restore under a new number, changing a model ID, relocating the data root — only change the object itself and one or two conspicuous references; records with keys derived from the old identifier (the SDK session's `project_key`, output inventory registrations) or settings referencing the old identifier (project model settings) stay under the old value
Cost: agent conversation resume reports the conversation can't be found; existing outputs read as missing, and the UI guides the user to regenerate; the project dangles a reference to the old model ID, and later a new model with the same ID silently takes over (severity: routine)
Detection: judgement required
- #451 · 2026-10-26 · Post-merge fix: the `project_key` of SDK session records was derived from the absolute path of the project directory, so after the data root's absolute path changed (desktop app on a new machine, white-label rename, changing `ARCREEL_DATA_DIR`) all session resumes failed, and keys weren't changed at startup (#3436, fixed by #3473's 247db4556); not caught: no corresponding rule
- #3264 · 2026-10-26 · Post-merge fix: when a custom provider's model changed ID or was deleted, only global settings were handled, and the 10 model setting keys and `model_settings` in projects referencing it weren't synchronised (#3433, fixed by #3476's 9ff036906); not caught: no corresponding rule
- #3421 · 2026-10-26 · Post-merge fix: after a deleted project was restored under the new name `demo-2`, session ownership changed but the SDK transcript stayed under the old path's key, and resume reported `No conversation found` (#3435, fixed by #3473's f9016f61d); not caught: no corresponding rule
- #3471 · 2026-10-26 · Self-check: restoring storyboard frames and video units under new numbers only moved media files and version history, and output inventory registrations weren't moved to the new numbers, so restored outputs read as missing; before merge it was changed to re-register along with the new numbers (b50f34c1a); not caught: no corresponding rule

### Cross-scope interaction state is gathered into a single session with a generation token, and async writes only land while the token is still valid
Destination: docs/standards/frontend.md "When switching scope, the old scope's interaction state is invalidated as a whole, and async writes only land while the token is still valid" (all three were judgement-required omissions, occurring respectively in a hook's confirmation state, a store's pagination state and session errors, with no check able to decide them uniformly)
Pattern: a hook's or store's interaction state (confirmation request, in-flight, failure reason, pagination cursor and total) is scattered across multiple states, and scope switches rely only on aborting one request or clearing part of it; abort can't reach other async writes that return after the switch, several parallel request sources each invalidating on their own can't stop each other, and results from the old scope are written into the new scope, or old state lingers into the new scope
Cost: clicking confirm in one project actually cancels another project's task, or the UI shows another project's failure message (severity: routine)
Detection: judgement required
- #3412 · 2026-10-24 · Found in review: `useTaskCancellation` successively added scope ownership (45e77af90) and render-time clearing (bf9e5c5c6), and only in the third round (31270f1ca) gathered the confirmation state into a single session with `gen`; root cause: each round only fixed the write path the reviewer pointed out, without first listing all async writes that would still land after a scope switch; not caught: no corresponding rule
- #3258 · 2026-10-25 · Post-merge fix: the two usage-record views each used their own refresh coordinator while jointly writing `total`, `nextCursor` and `pageIndex`; after switching views, the old view's request arrived late and rewrote the total, and the next page sent the generation-records cursor to the session endpoint, returning 422 (#3439, fixed by #3492's 77b54be96, changed to share one coordinator); not caught: no corresponding rule
- #3441 · 2026-10-25 · Post-merge fix: switching projects and creating a new session only reset the timeline without clearing errors, so the previous session's error message lingered in the new project or new session; only `switchSession` cleared it (fixed by #3492's e0e3b0dc0, 2700f4a31); not caught: no corresponding rule

### A value's format or range is defined in only one place, and producers, validators and consumers all reference it
Destination: docs/standards/general.md "A value's format or range is defined in only one place, and producers, validators and consumers all reference it" (the four occurrences concern numbering, definition values, entry types and revision respectively, each now with round-trip tests added; no single check can uniformly decide the interpretation of different values, so it is written as a review rule requiring the same round-trip tests to be added when changing; it spans the frontend, backend and subpackages, so it goes in the general standard)
Pattern: a producer loosens or changes a value's format or range (number of sequence digits, numeric type, entry type, algorithm prefix), while validators or consumers still hard-code the old interpretation, so valid values produced by the system itself are rejected or misread by itself
Cost: episodes numbered past 100 can't complete content confirmation; valid endpoint definitions can't generate video; marketplace entries can't be installed; legitimate edits by external agents are judged as concurrency conflicts (severity: routine)
Detection: mechanical (proposed round-trip test: the producer's real output at the boundary drives every validator and consumer)
- #3471 · 2026-10-26 · Found in review: `format_item_id` could emit `E1S100`, while `NarrationScriptPlanSegment.segment_id` and `collect_narration_violations` only accepted two-digit sequence numbers; c54fc1698 changed it to `\d{2,}`; not caught: no corresponding rule
- #2163 · 2026-10-25 · Post-merge fix: a declarative endpoint definition's `max_reference_images: 1.0` passed the JSON Schema, but the capability consistency check only accepted `int` and wrongly rejected it (#3447, fixed by #3494's 0ea7b2976); the first version of the fix only changed the check, and integration review then found that video capability resolution threw ValueError and the image reference image cap read as 0 (ef29215c0); not caught: no corresponding rule
- #2778 · 2026-10-25 · Post-merge fix: from 828502274 on, ComfyUI endpoints could be shared to the official marketplace, but the install dialog still required the definition to have `submit` and `poll`, so valid ComfyUI entries couldn't be installed or updated (#3440, fixed by #3492's 0de8c34f9); not caught: no corresponding rule
- #3484 · 2026-10-26 · Found in acceptance: remote MCP's `get_script_plan_content` returned a revision with the `sha256-v1:` prefix, while `open_draft` and `get_workflow_plan` returned bare hex; the first version of the fix was only compatible with the formal draft baseline and stale rebuild, and integration review found that the remaining round-trip entry points and the overwrite receipt still only accepted one form (fixed by #3505's 757d94853, 10965b85d); not caught: no corresponding rule
