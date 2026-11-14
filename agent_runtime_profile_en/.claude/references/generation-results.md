# Generation result contract

All generation tools (storyboard images, grids, videos, sheets, narration audio, image edits) return the same structure.
Read the structure, not the text: the text is only a human-readable projection of that structure.

## Selection: explicit IDs vs fill-missing

| How it is passed | Meaning |
|---|---|
| Pass an ID list (`target.ids` / `segment_ids` / `scene_ids` / `names` / `edits[].id`) | Explicit selection; videos additionally need `force: true` to force a redo, for the other tools naming an item forces it |
| Pass no IDs | Fill-missing only (missing-only): only units whose artifact is missing are made |
| Pass an empty array `[]` | Invalid: it does not mean "all" and the request is rejected; to fill missing, omit the parameter |

`generate_videos(target.scope="episode")` is the whole-episode fill-missing entry point; it never forces a redo of existing clips;
to redo named shots, use the `scene` / `selected` scope and pass the IDs explicitly with `force: true`.

**Outdated but still usable old artifacts (stale) are reused, not automatically regenerated** — they can still be viewed and exported.
Only redo them by naming explicit IDs when the user asks for an update. Units whose artifact status cannot be read (blocked) are reported as a separate gap
and are NEVER treated as missing and regenerated, because that would turn one corruption into one duplicate charge.

## Results: accounted per ID

```
requested = succeeded ∪ failed ∪ blocked   (mutually exclusive)
skipped                                     (reused old artifact, not in requested)
```

- **succeeded** — done
- **failed** — was enqueued and executed (possibly already charged) and then failed; also includes targets whose enqueue itself did not go through — their
  `task_state` is `not_queued`, no task was ever created and nothing was ever charged
- **blocked** — the request never reached enqueueing (not charged), e.g. missing dependency, invalid request, unreadable artifact status

When a batch enqueue is interrupted midway, **tasks already created are not withdrawn**: they are complete paid units that passed admission, and they run
as usual and produce output as usual. Targets that had not been reached are reported as `failed` with an "enqueue interrupted" problem code; the next fill-missing run does exactly those —
do not suggest the user rerun the whole batch, that would pay twice for units already running.

Every `failed` / `blocked` entry in `items` carries:

- `problem.code` — stable problem code
- `problem.action` — the next action from a closed set (`retry` / `fix_input` / `generate_dependency` /
  `replan_unit` / `configure_provider` / `confirm_request_duration` / …)
- `unit_id` and (if present) `artifact_key`, `artifact_path`

Grids are accounted by **shot ID**: shots in the same group share one grid composite, and that image's enqueue and task results are projected onto
every shot it covers. Grid generation only produces the composite: a succeeded shot's `artifact_path` is `grids/<grid_id>.png`;
storyboard images are written only after the user has reviewed and approved it and the grid is split via `split_grids`.

**Decide the next step by `code` and `action`; do not parse text to judge whether a retry is possible.**

## Read the four status axes separately

Workflow step state (`plan.steps[].state`), `task_state` (queue task),
`provider_checkpoint` (whether the provider has submitted), `artifact_status` (artifact current/stale/missing/blocked)
are independent of each other — **state them separately, do not translate one into another**:

- task succeeded ≠ artifact matches the current basis
- artifact missing ≠ task failed: it may never have been enqueued (`blocked`, not charged)
- `provider_checkpoint.submitted` true means it has been submitted on the provider side and was very likely charged; `task_state`
  `interrupted` means there is no provider verdict and a blind retry may charge twice — always decide by `problem.action`;
  in that case it usually hands back to `wait_for_task` (the task may still be running and land normally); do not change it to `retry` on your own
- when writing an artifact to disk fails, that ID is recorded as `failed`, the old paid artifact is kept as is, and it is NEVER marked in place before the file has actually landed

Artifact history is a separate axis: `current` is the currently selected artifact, `stale` is an old artifact whose basis has changed but which still exists, and historical versions are other versions
produced (and paid for) earlier. The Agent MUST NOT automatically delete, overwrite or regenerate any paid artifact or historical version — redoing is the user's explicit decision.

When the user asks "is it done yet", answer along these four axes rather than collapsing it into "it succeeded". For how to read the plan as a whole see
[workflow-plan.md](workflow-plan.md).
