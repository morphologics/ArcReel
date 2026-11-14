# Unified usage record rows: investigation of the linkage between generation tasks and provider calls

> Date: 2026-09-02. Corresponds to [#2287](https://github.com/ArcReel/ArcReel/issues/2287) (map [#2286](https://github.com/ArcReel/ArcReel/issues/2286)).
> Goal: usage records use "one call" per row, and in-progress generation tasks (`tasks`) and finished provider calls (`api_calls`) need to be merged into one timeline. This document answers the ticket's 5 questions one by one, with code locations for each conclusion (line numbers per main `1b6a0c2be`), and finally gives feasible ways to assemble a "unified row" and a list of gaps.
> Scope of investigation: `lib/db/models/{api_call,task}.py`, `lib/db/repositories/{usage_repo,task_repo,base}.py`, `lib/billing/ledger.py`, `lib/generation/media_generator.py`, `lib/backends/text_generator.py`, `lib/generation/generation_worker.py`, `server/agent_runtime/session_manager.py`, `lib/custom_provider/endpoint_test/trial_run.py`, `server/routers/{usage,tasks}.py`, `server/auth.py`, frontend `GlobalHeader` / `UsageDrawer` / `TaskHud` / `StudioLayout` / `useTaskRefresh` / `usage-store` / `tasks-store`, ADR 0021 / 0053. No code was run; all conclusions come from static reading; places marked "unverified" are inferences.

## Conclusions at a glance

| Question | One-line conclusion |
|---|---|
| 1 Back-reference | `api_calls` has no `task_id` / `resource_id` / `provider_job_id` columns; the only exact link is **`tasks.payload_json.api_call_id` → `api_calls.id`**, and only the video path writes it. `segment_id` / `output_path` can only be used for weak matching. A task has at most one call (retried downloads and resume both reuse the original row), but calls without a task exist in large numbers. |
| 2 Origin | All that text calls have to use is `project_name` (may be an empty string), `provider`, `model`, and the first 500 characters of `prompt`; the text task type (script/overview/style analysis), the assistant session_id and the trial-run endpoint identifier are not persisted. |
| 3 Visibility scope | Both tables have a NOT NULL `user_id` (always `"default"`); the read-side `_scope_query` is a no-op, and the `/usage/*` and `/tasks*` routes take no user parameter; the auth dependency maps any token to `DEFAULT_USER_ID`. Multi-user isolation is left to subclass overrides per ADR 0021, with the same policy for both tables. |
| 4 Demo fallback | `GlobalHeader.tsx:76-77` sets `usageProjectName` to null in demo → the drawer and the top bar cost fall back to **global** usage; on the task side, `StudioLayout.tsx:70-72` + `useTaskRefresh.ts:44-54` disable outright and clear the store. Hiding the entry points requires touching three buttons — cost badge, drawer, task radar — plus the effect that "closes already-open panels when switching into demo". |
| 5 Read API policies | `/usage/calls`: page_size default 20, max 100, by `started_at desc`; `/projects/{name}/tasks`: default 50, max 500, by `updated_at desc, queued_at desc`; `/tasks/stats` no pagination, counts by status. All three use offset pagination, different time keys, and no ordering tiebreak (usage). |

## 1. Can an `api_calls` row point back to a `tasks` row

### Current state

**Columns of the `api_calls` table usable for linkage** (`lib/db/models/api_call.py:14-50`): `project_name` (L18), `call_type` (L19), `model` (L20), `output_path` (L28, Text, nullable), `segment_id` (L29, String(20), nullable, indexed), `started_at` / `finished_at` (L30-31), `provider` (L36), `last_provider_response` (L44, JSON), `user_id` (`UserOwnedMixin`, `lib/db/base.py:33-42`). There are **no** `task_id`, `resource_id`, `provider_job_id`, `resource_type` or `task_type` columns.

**The `tasks` table** (`lib/db/models/task.py:13-69`): `resource_id` (L21, NOT NULL), `resource_type` (L24, written only by image_edit), `payload_json` (L26), `provider_id` / `provider_job_id` (L35-36), `queued_at` / `started_at` / `finished_at` / `updated_at` (L47-50). There is **no** `api_call_id` column.

How well each field matches up:

| Clue | Can it match | Basis |
|---|---|---|
| `tasks.payload_json["api_call_id"]` → `api_calls.id` | **Exact, but written only by video / reference_video tasks** | The only write point is inside the video bookkeeping bracket: `lib/generation/media_generator.py:1007-1014` (calls `persist_api_call_id` when `task_id is not None`), landing in `lib/db/repositories/task_repo.py:1049-1056` (`_merge_payload_field` writes into payload). Neither the image bookkeeping bracket (`media_generator.py:613-624`) nor the audio bookkeeping bracket (`media_generator.py:729-737`) writes it; the `task_id` of image/tts tasks is used only for the staging path (`media_generator.py:48-71`). |
| `tasks.resource_id` ↔ `api_calls.segment_id` | **Weak match, whitelisted resources only** | `segment_id_for` (`media_generator.py:141-154`): image writes `resource_id` into `segment_id` only for `storyboards/videos/grids`, video only for `storyboards/videos/reference_videos`; audio passes it through unconditionally. The `segment_id` for character/scene/prop/product images is NULL (ADR 0053 `docs/adr/0053-cost-attribution-ledger-key-primary.md:7`). And ADR 0053:14 states explicitly that IDs are not globally unique and are not guaranteed to match the episode they belong to; the same `resource_id` accumulates multiple calls with regeneration. |
| `tasks.provider_job_id` ↔ `api_calls.?` | **No match** | `api_calls` has no such column. `provider_job_id` is written only on the tasks side by `persist_provider_job_id` (`task_repo.py:966-992`). `last_provider_response` is an arbitrary JSON snapshot (limited to 64 KiB in `usage_repo.py:42-56`), which may contain the provider job id, but with no structural guarantee (unverified). |
| `api_calls.output_path` ↔ task artifact | **Weak match, successful rows only** | `output_path` is written only on successful settlement (`lib/billing/ledger.py:146` → `finish_call(output_path=...)` in `usage_repo.py:367-411`); it is NULL for failed / pending rows. On the tasks side the corresponding path is in `result_json` (`task_repo.py:551` `mark_succeeded(result)`, structure unverified), with no separate column. |
| `project_name` + `call_type` + time window | **Heuristic** | Both tables have `project_name`; `tasks.media_type` (`task.py:20`) ≈ `api_calls.call_type`; `api_calls.started_at` falls within `tasks.started_at..finished_at`. Gets crossed with concurrent tasks of the same kind. |

**Can one task produce multiple calls**: all paths investigated reuse the same `api_calls` row:

- Retried download: `task_repo.retry_artifact_download` (`task_repo.py:601-648`) flips the original `ApiCall` from failed/pending back to pending, with a comment stating "仍是同一条调用，不新增计费行" ("still the same call; no new billing row") (L623-625).
- Resume (continuing after restart / crash window): `generate_video_resume_async` doesn't open a new bookkeeping bracket (`media_generator.py:1135-1138`), and only flips the pending row precisely by `call_id` via `ledger.resume_success` / `resume_failed` (`lib/billing/ledger.py:152-162`, `media_generator.py:1229-1239`); declaring it dead before dispatch also flips the original row (`lib/generation/generation_worker.py:1337-1355`).
- The `api_calls.retry_count` column (`api_call.py:33`) has no write point at all; it is only read out in `_row_to_dict` (`usage_repo.py:144`) and is always 0.
- Unverified: whether `grid` / `reference_video` tasks internally issue multiple consecutive backend calls for the same task (e.g. multi-unit reference-to-video); if so, only the id of the last video call stays in `payload.api_call_id` (`_merge_payload_field` overwrites).

**Can a call have no task**: this exists in large numbers.

- All text calls: `TextGenerator.generate` (`lib/backends/text_generator.py:51-66`) takes no task; call sites include script generation (`lib/script/script_generator.py:262,1328`), episode planning (`lib/episode/episode_planner.py:412,659`), project overview (`lib/project/project_manager.py:3612-3625`), style analysis (`server/routers/files.py:1023-1024`), and `server/text_generation.py:850,1512,1659`.
- Assistant session backfill: `session_manager._record_assistant_usage` goes through `ledger.backfill` (`server/agent_runtime/session_manager.py:1184-1210`), writing a terminal-state row in one go.
- Custom endpoint trial run: `trial_run.py:375-386`, `project_name=""`, `call_type="video"`, no task.
- Any call site of the bookkeeping bracket leaves no task clue when `task_id=None` (the default `task_id: str | None = None` at `media_generator.py:487,532`).
- The reverse also holds: an image task is marked `restart_lost_image` after a process crash (`generation_worker.py:1163-1173`); its pending `ApiCall` has no `api_call_id` to flip and stays pending forever (inference: the bookkeeping bracket settles only on `CancelledError` / `Exception`, `ledger.py:133-150`; the repository layer has no pending sweep logic — grep for `stale|sweep|orphan` in `usage_repo.py` / `ledger.py` gets no hits).

### Feasible assembly

- **Primary link**: `tasks.payload_json.api_call_id` is the only existing exact foreign key, and video tasks can be joined directly; the task API already returns `payload` as is (`_task_to_dict` in `task_repo.py:96-125` includes `payload`; `_localize_task` in `server/routers/tasks.py:74-112` only strips `execution_checkpoint_json`), and the frontend `TaskItem.payload` (`frontend/src/types/task.ts:32`) can read it.
- **Weak-link fallback**: non-video tasks use the heuristic `(project_name, user_id, media_type≈call_type, segment_id==resource_id, started_at ∈ [task.started_at, task.finished_at])`; asset images (`segment_id` NULL) further degrade to matching the `output_path` prefix against the `resource_id` file name (`usage_repo._classify_asset_output_path` already infers the asset type from the path, `usage_repo.py:108-124`, reusable).
- **Unified row definition**: terminal states follow the `api_calls` row, in-progress states (queued/running/cancelling) follow the `tasks` row; a task with an `api_call_id` is merged into one row once its call row settles, to avoid double-counting the same event.

### Gaps

1. `api_calls` lacks a `task_id` column (nullable, indexed); `ledger.record` lacks a `task_id` parameter, and the image / audio bookkeeping brackets don't persist `api_call_id`.
2. `retry_count` has no writes, so "which execution of the same task this is" can't be expressed; task-side retries only refresh `started_at` (`task_repo.py:634-639`).
3. Image pending rows without a task have no reclamation path.

## 2. "Origin" fields displayable for text calls without a task

### Current state

There are only two write entry points: `ledger.record` (the parameter set of `lib/billing/ledger.py:95-111`) and `ledger.backfill` (`ledger.py:169-207`). Fields actually persisted for text rows:

| Field | Value | Basis |
|---|---|---|
| `project_name` | Passed by the caller, **may be an empty string** | `text_generator.py:58` `project_name or ""`; `trial_run.py:376` fixed `""`; session backfill uses `managed.project_name` (`session_manager.py:1197`) |
| `call_type` | `"text"` | `text_generator.py:59`, `session_manager.py:1198` |
| `provider` / `model` | The resolution layer's provider_id and the backend model | `text_generator.py:60-61`; the assistant is fixed to `PROVIDER_ANTHROPIC` (`session_manager.py:1201`) |
| `prompt` | First 500 characters | `text_generator.py:62`; `session_manager.py:1200` takes the last user prompt |
| `status` / `error_message` / times / tokens / cost | Written at settlement | `usage_repo.py:367-411` |
| `segment_id` / `output_path` | NULL | Not passed on the text path |

The text task type `TextTaskType` (SCRIPT / OVERVIEW / STYLE_ANALYSIS, see the factory at `text_generator.py:44-49`) is known when the backend is created, but **is not passed to the ledger**. The assistant session's `session_id` is available on the same call chain (`managed.session_id`, `session_manager.py:1179`) and likewise is not persisted. Trial runs have no endpoint identifier.

The frontend `UsageCall` type (`frontend/src/stores/usage-store.ts:21-40`) only declares `project_name / call_type / model / status / cost / provider / output_path / resolution / duration / error / times / tokens`, and does not declare `prompt` or `segment_id`; the drawer row rendering (`frontend/src/components/layout/UsageDrawer.tsx:235,290,321`) shows the artifact file name, model, tokens and time, but not `project_name` or `prompt`.

### Feasible assembly

- The "origin" that existing fields can assemble: `provider == anthropic && call_type == text` → assistant session; `project_name == ""` → trial run or text call without a project; the `prompt` prefix can serve as a hover hint. These are all inferences and can't distinguish script generation from episode planning.
- Short term: encoding `TextTaskType` into an existing parameter of `ledger.record` (e.g. a label prefix on `prompt`) is a hack and is not recommended.

### Gaps

4. `api_calls` lacks a "purpose / origin" column (suggested `purpose` or `origin`, with values such as `script_generation` / `episode_planning` / `overview` / `style_analysis` / `assistant_session` / `endpoint_trial` / `task:<task_type>`), and a nullable `session_id`. `ledger.record` / `backfill` need corresponding new parameters, and the 5 text call sites plus `session_manager` / `trial_run` need to pass them.
5. The frontend `UsageCall` and the `/usage/calls` response body already contain `segment_id` / `prompt` (`usage_repo.py:126-158`), but the frontend type doesn't declare them and needs to be updated.

## 3. Visibility scope of `user_id` and the tasks table in multi-user mode

### Current state

- Both tables carry a NOT NULL, `server_default="default"`, indexed `user_id` via `UserOwnedMixin` (`lib/db/base.py:33-42`; `api_call.py:14`; `task.py:13`).
- The read-side filter point is `BaseRepository._scope_query`, which is a no-op in the open-source edition (`lib/db/repositories/base.py:17-19`). `UsageRepository.get_calls` and `get_stats*` both apply it (`usage_repo.py:665,672`); so do `TaskRepository.get / list_tasks / get_stats` (`task_repo.py:1195,1224,1235,1252`).
- The routing layer takes no user parameter: neither `/usage/calls` (`server/routers/usage.py:56-81`) nor `/tasks*` (`server/routers/tasks.py:114-165`) has a `CurrentUser` dependency, and neither passes `user_id` to the repository.
- The auth layer returns `DEFAULT_USER_ID` regardless of token content (`_anonymous_user` in `server/auth.py:69-73`, `_payload_to_user` in `server/auth.py:426-432`).
- Write side: video/image/audio bookkeeping takes `user_id` from `MediaGenerator._user_id` (`media_generator.py:173,622,736,1001`); text calls don't pass `user_id` and use the `ledger.record` default `DEFAULT_USER_ID` (`ledger.py:107`, `text_generator.py:57-63`); assistant backfill uses `getattr(self, "_user_id", DEFAULT_USER_ID)` (`session_manager.py:1202`). The `user_id` for task enqueueing is passed by the caller (`lib/generation/generation_queue.py:398`; `server/tool_runtime.py:254-265`), and the dedup unique index includes `user_id` (`task.py:57-68`).
- ADR 0021 (`docs/adr/0021-multi-user-preembed-scope-query.md:7-12`): the commercial edition injects filtering by overriding `_scope_query` in a subclass; `claim_next` using raw SQL is a known exception.

### Feasible assembly

The visibility scope of the unified row can be left entirely to `_scope_query`: both tables share the same seam and the same `user_id` semantics, so the merged query just needs to go through the same `BaseRepository` subclass. No additional per-user filter parameter is needed.

### Gaps

6. Text calls don't pass `user_id`, so in the commercial edition they would all be attributed to `"default"`, diverging from the tasks table, which persists by the caller's user (`text_generator.py:57-63` vs `generation_queue.py:398`). If the unified row merges across tables, text rows would "disappear" or be misattributed in multi-user mode.

## 4. The fallback logic when projectName is null in the demo project, and hiding the entry points

### Current state

- **Usage-side fallback**: `frontend/src/components/layout/GlobalHeader.tsx:73-77`: when `demoMode || isDemoProject(currentProjectName)`, `usageProjectName = null` (comment: the demo project has no usage records in the backend, querying by project would 404, so fall back to global usage). This value feeds the top bar cost statistics (L88-99 `API.getUsageStats(usageProjectName ? {...} : {})`) and `UsageDrawer` (L339-344 `projectName={usageProjectName}`). Inside the drawer, `projectName ?? undefined` (`UsageDrawer.tsx:65,80-83`) → the request carries no `project_name` → the backend returns the **global** call list (`usage.py:59` optional parameter, `usage_repo.py:431-432` adds no filter for an empty value).
- **Task-side disabling** (not a fallback): `frontend/src/components/layout/StudioLayout.tsx:66-72` computes `isEffectivelyDemo`, `useTaskRefresh(sseProjectName, !isEffectivelyDemo)`; `frontend/src/hooks/useTaskRefresh.ts:44-54` clears `refreshScope / tasks / stats / connected` when `enabled=false`. The comments point out that `projectName=null` means "don't filter by project", not "disabled" (`useTaskRefresh.ts:28-30`, `tasks-store.ts:35-38`).
- **Demo detection**: `frontend/src/onboarding/demo-project.ts:34,45-47` (`DEMO_PROJECT_NAME = "onboarding_demo"`, case-insensitive comparison); `useDemoWorkbench` (`GlobalHeader.tsx:9,76`).
- **Current state of entry points**: the cost badge button (`GlobalHeader.tsx:287-296`) and the task radar button (`GlobalHeader.tsx:348-380`, `<TaskHud>` L380) are **still rendered and clickable** in demo; only the export button is disabled by `demoMode` (L398,408-409), and there is an effect that "closes the export dialog if demo is entered while it is open" (L79-85).

### Feasible assembly

The strategy for the unified row panel in demo should be one of two, fixed in code: (a) consistent with the task side — hide the entry points and clear the store; (b) consistent with the usage side — fall back to global. The two sides currently contradict each other (tasks cleared, usage falls back to global); after merging into one timeline, "in demo you can see historical calls but not tasks" would occur. Option (a) is recommended.

### Gaps / points to change

7. `GlobalHeader.tsx:287-296` cost badge, `339-344` drawer, `348-380` task radar + `TaskHud`: hide or disable by `demoMode || isDemoProject(currentProjectName)`; following L79-85, add an effect that closes an already-open drawer / HUD when switching into demo.
8. The null fallback in `GlobalHeader.tsx:76-77` can be deleted once the entry points are hidden; otherwise the top bar still issues one global usage request.
9. `TaskHud.tsx:609` reads `useTasksStore().tasks` directly and is unaware of demo; if rendering is kept, an empty state needs to be added inside the HUD.

## 5. Pagination and sorting policies of the three read APIs

| API | Pagination parameters and limits | Sorting | Filters | Other |
|---|---|---|---|---|
| `GET /usage/calls` (`server/routers/usage.py:56-81`) | `page`≥1 default 1; `page_size` default **20**, 1..**100** (L64-65) | `started_at DESC`, **no tiebreak** (`usage_repo.py:671`) | `call_id / project_name / call_type / status / start_date / end_date`; dates apply to `started_at` as whole UTC days (`usage_repo.py:439-444`); `_build_filters` supports `provider` but `get_calls` doesn't pass it through (`usage_repo.py:643-660`) | Offset pagination, returns `{items,total,page,page_size}` (`usage_repo.py:670-682`); the repo doesn't clamp, relying on the route's `le=100` |
| `GET /projects/{name}/tasks` (`server/routers/tasks.py:144-165`) | `page`≥1 default 1; `page_size` default **50**, 1..**500** (L151-152); the repo clamps again to 1..500 (`task_repo.py:1210-1211`) | `updated_at DESC, queued_at DESC` (`task_repo.py:1231`) | `status / task_type / source`; `project_name` comes from the path | Offset pagination, same-shaped response body (`task_repo.py:1239-1244`); each item has its error text localized via `_localize_task` (`tasks.py:74-112`); usage has no such step |
| `GET /tasks/stats` (`server/routers/tasks.py:114-119`) | No pagination | None | Optional `project_name` | GROUP BY `status` counts with zero-filling (`task_repo.py:1246-1274`), returns `{stats:{queued,running,cancelling,succeeded,failed,cancelled,total}}` |

Addendum: the frontend task store actually calls the global `GET /tasks` (`server/routers/tasks.py:121-141`, same policy) with `TASKS_PAGE_SIZE = 200` (`frontend/src/stores/tasks-store.ts:8,250-252`), not `/projects/{name}/tasks`; the usage drawer uses `page_size` 20 (`usage-store.ts:67`). `/usage/stats` (`usage.py:19-53`) is the usage side's counting API, with different dimensions from `/tasks/stats` (by provider / type / currency vs by task status).

### Feasible assembly

- Time key: call rows use `started_at` (`api_call.py:30`, NOT NULL), task rows use `queued_at` (`task.py:47`, NOT NULL); `tasks.updated_at` jumps around with status changes and is unsuitable as the timeline's primary key.
- Merging approach: a single UNION view (`api_calls` terminal rows ∪ `tasks` active rows, excluding tasks whose `payload.api_call_id` already matches a call row) with keyset pagination by `(ts DESC, id DESC)`, one query per page; or the frontend fetches a page from each and merges client-side, but the two APIs' page_size limits and total semantics differ (20/100 vs 50/500), so paging would get misaligned.

### Gaps

10. The two APIs have no common ordering tiebreak; when several calls fall within the same second, offset paging may duplicate / drop rows; the unified row needs a keyset cursor (timestamp + id).
11. The `project_name` filter semantics are consistent across the three APIs, but the `status` value sets differ (usage: `pending/success/failed`; tasks: `queued/running/cancelling/succeeded/failed/cancelled`); the unified row needs a mapping table.
12. The tasks route localizes error text, the usage route doesn't (`error_message` is returned as is, `usage_repo.py:138`); after merging, the same column has two policies.

## Overall recommendation for the unified row

**Recommended assembly**: add a new server-side timeline read API (e.g. `GET /usage/timeline`), implemented by one `UsageRepository` method:

1. Take all `api_calls` rows as the primary rows for "finished / in-progress calls" (including pending);
2. Take tasks in `tasks` with `status IN (queued, running, cancelling)` that have **no** matching call row as supplementary "queued" rows (video tasks are judged by `payload.api_call_id`, other tasks by the weak-link heuristic in section 1);
3. Keyset pagination by `(ts, id)`, with `ts` taken from `api_calls.started_at` / `tasks.queued_at`;
4. Multi-user filtering through the same `_scope_query`;
5. Each row carries `origin`, `task_id` (nullable) and the mapped unified `status`.

**Gaps to fill first** (by priority):

| # | Gap | Suggested change |
|---|---|---|
| 1 | `api_calls` has no `task_id`; image / audio don't persist `api_call_id` | Add a nullable indexed column `api_calls.task_id`; add a `task_id` parameter to `ledger.record` and pass it through in all three media bookkeeping brackets; backfill in reverse from `tasks.payload_json.api_call_id` during migration |
| 4 | Text / trial run / assistant calls have no origin field | Add `api_calls.purpose` (enum string) and a nullable `session_id`; `TextGenerator.create` already knows `TextTaskType`, so carry it into `generate`'s bookkeeping while at it |
| 6 | Text call `user_id` is always default | `TextGenerator` takes `user_id` and passes it through to `ledger.record` |
| 10/11/12 | Pagination tiebreak, status mapping, localization policy | Handle uniformly in the new API; don't change the existing three APIs |
| 7/8/9 | Demo entry points | Frontend handles per the checklist in section 4 |
| 2/3 | `retry_count` has no writes, orphaned image pending rows have no reclamation | Can be deferred; the unified row first falls back to "pending for more than N hours is shown as unknown" (policy undecided, needs a product decision) |
