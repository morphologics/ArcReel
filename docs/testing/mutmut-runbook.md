# mutmut mutation testing runbook

The steps for running a batch of mutation tests, reviewing the results, and accepting the result after reworking tests. Configuration is in `[tool.mutmut]` in `pyproject.toml`; the reasoning behind each setting is in the comments there.

For where mutation scores stand, see the last paragraph of "Each test case protects one observable contract" in `docs/standards/testing.md`. Mutation testing is not part of CI; it is run manually in batches, by filling this batch's modules into `[tool.mutmut] only_mutate`.

**Survival only puts a test up for review; it is not grounds for deletion.** When a surviving mutant lands on code already covered by tests, decide on each one following the handling advice in "Each test case protects one observable contract" in `docs/standards/testing.md`. The following cases are not acted on:

- Mutants that cannot be killed by only strengthening assertions without changing an existing test's inputs, i.e. that can only be killed by changing the inputs (that counts as coverage compensation).
- Equivalent mutants (the source changed but the behaviour did not).
- Mutants that land on code with no test coverage.

## 1. Environment

mutmut is in a separate dependency group `mutation` and is not installed by the default `uv sync`:

```bash
uv sync --group mutation
```

Mutated modules import mutmut's trampoline at the top, so it must be installed in `.venv`; `uv run --with mutmut` does not cover the later review steps. Run `uv sync` again when you want to clear it after a batch.

## 2. Running a batch

### 2.1 Choosing modules

Each batch takes about 8 modules that have not been run before, picked from outside the "Modules already covered" table in section 8; open an issue for each batch recording the module list, decision results and research branch, and add the modules to that table after merging.

- **Admission**: don't pick anything in the `server/routers` directory; for other directories go file by file, and don't pick files where lines skipped due to decorators make up 50% or more. mutmut 3.7.0 generates no mutants for the whole subtree of a decorated function (only a single `@staticmethod` / `@classmethod` is exempt; decorated classes are skipped entirely; not configurable), so forms such as route functions and `@tool` closures produce almost no mutants even when run. Run a census to list files that fail admission:

  ```bash
  uv run python scripts/decorator_census.py --per-file --min-skip 50
  ```

  Without arguments it summarizes the skipped-line share by directory, for seeing which directories are worth picking as a whole.

- **Priority**: pick by "not run yet, many tests, lots of code"; `server/services`, `lib/backends/video_backends`, `lib/custom_provider`, `lib/script/reference_video` come first. Don't look for rules that predict candidate rate or actionable rate: test-double density and the share of weak-assertion tests have both been tried and have no predictive power. Test speed is not an admission criterion either; timeouts and parallel subprocess problems have been eliminated in `tests/conftest.py` and `tests/mutmut_plugin.py`, and DB-tier integration tests also run cleanly.
- **End point**: none set. After each batch, compute the actionable rate (surviving mutants judged ③ worth protecting / surviving mutants); stop when it drops markedly for two batches in a row.

### 2.2 Running a round

1. After `git fetch`, branch from `origin/main`, and check that the tree already contains the PRs merged for the previous batch. If the baseline is cut from a stale local `main`, the mutants targeted by the previous batch's rework will "come back to life" in this batch, wasting a round of review.
2. Fill this batch's modules into `only_mutate` in `[tool.mutmut]`. Leaving it empty mutates all of `source_paths`, and one round takes hours.
3. Run:

   ```bash
   uv run mutmut run
   ```

   Parallelism defaults to the CPU count; no need to lower it. Each session's temp directory is made private by `tests/mutmut_plugin.py` (loaded via `[tool.mutmut] pytest_add_cli_args`), temp DB cleanup is guarded by creator process in `tests/conftest.py`, and parallel subprocesses do not share cleanup actions.
4. When done, copy `mutants/**/*.meta` somewhere the next `mutmut run` will not overwrite; this is the baseline used for acceptance in section 5. For the first batch it is under `baseline/` on the `research/mutmut-batch-1` branch.

Remember to turn `only_mutate` back into a comment after the run; `mutants/` is already in `.gitignore`.

**Any test that reads source text rather than importing the module must be `--deselect`ed by nodeid in `pytest_add_cli_args`; do not `--ignore` the whole file.** Source files in `mutants/` contain all mutant variants at once (every string literal gets two extra copies, `XXfooXX` / `FOO`), so any test that scans source literals in `lib/` `server/` and compares them against a registry reports "not registered" in the stats phase, and the round cannot start. `--ignore` works at file granularity: behavioural tests in the same file disappear along with it, and the mutants only they cover are recorded as survivors (this is what happened to the 100-plus tests in `test_task_failure_capability.py` other than its three scanning tests). Already excluded are that file's `test_capability_codes_registered_no_drift` / `test_no_unscannable_capability_construction_sites` / `test_capability_construction_sites_supply_every_template_param`; when adding a new exclusion, note in the `[tool.mutmut]` comments what it alone can kill and why excluding it does not miss kills (criterion: an exclusion may only add false survivors, never hide false kills).

## 3. Reading the results

**First check the order of magnitude of each module's mutant count.** There are about 1000 mutants per thousand lines of source; a module with markedly fewer (say less than a tenth) is not well tested — rather, its functions are not linked to any test: mutmut silently zeroes such modules without reporting it in the terminal. Drop it from this batch without judging it; a misspelled module name in `only_mutate` also gives 0 mutants, so check the spelling first.

**Compute from the `.meta` files, don't read the terminal summary.** Each source module has a `<module>.py.meta` in `mutants/`, in which `exit_code_by_key` is "mutant name → pytest exit code". mutmut's terminal summary only enumerates a few exit codes it knows, and things like segfaults (−11) don't land in any counter; #2257 missed two that way.

| exit code | mutmut's interpretation | Handling in this repo |
| --- | --- | --- |
| 1 | killed | killed |
| 0 | survived (🙁) | survived, goes to judgment |
| 36 / −24 / 24 / 152 / 255 | timeout (⏰) | review in a new process per section 4 |
| 3 | mutmut treats a pytest internal error as killed | review |
| others (including −11 segfault, None not checked) | suspicious or not recorded | review |

There is only one criterion: **any exit code that is neither 1 nor 0 is reviewed**, and only those confirmed as survivors by review go to judgment. 0 means pytest passed everything, which is itself confirmation of survival; no review needed.

## 4. New-process review of timeouts

Timeouts are a rare fallback, not the norm. Their former cause was fork: mutmut forks a subprocess per mutant to run tests, and aiosqlite connections left in the pool of the parent process's module-level engine (`lib.db.engine`) were inherited by the subprocess, while the worker thread driving them did not come along, so the subprocess's first test using that engine waited forever for a query result, used up its budget and was recorded as a timeout. `tests/conftest.py` uses `os.register_at_fork` to discard pooled connections in the subprocess (`dispose(close=False)`); a full 8-way re-run of the first batch's 8 modules had 0 timeouts. Timeouts that still occur do not count as killed; confirm them once more in a new process running only the linked tests, 3 to 11 seconds each.

```bash
# Run from the project root: tests-for-mutant reads mutants/mutmut-stats.json; running inside mutants/ reports Failed to load stats
uv run mutmut tests-for-mutant lib.speech.speech_rate.x_estimate_spoken_seconds__mutmut_8 > nodeids.txt

# Run inside mutants/, using the .venv interpreter, not uv run (see section 6)
cd mutants && MUTANT_UNDER_TEST=lib.speech.speech_rate.x_estimate_spoken_seconds__mutmut_8 \
  ../.venv/bin/python -m pytest -x -q @../nodeids.txt
```

pytest natively supports reading arguments from `@file`, one per line, keeping spaces and quotes as is; the 180-plus parametrized nodeids in this suite that contain spaces or quotes all work. Don't use `$(cat …)` or bare `xargs`: under zsh the former becomes a single argument and pytest silently runs 0 tests, and the latter splits nodeids containing spaces.

Interpretation: **1 failed means killed; all passing means survived.** For those confirmed killed by review (including segfaults and the like that land in neither bucket), change that mutant's exit code to 1 in the baseline copy saved in section 2, so that section 5 puts them in the "baseline killed" guard layer; leave those confirmed as survivors unchanged. Of the first batch's 616 mutants, only 2 segfaults needed review, and both were confirmed killed.

## 5. Three-layer acceptance after rework

After changing the assertions, **fully re-run** `mutmut run` once on the same batch of modules, then compare. `mutmut run` does not re-run mutants that already have results, and the mutant list itself lives in `.meta`, so deleting only `.meta` gives 0 mutants. To re-run the same batch of modules: move `mutants/mutmut-stats.json` out, delete the whole `mutants/`, then put it back into the newly created `mutants/`; stats need not be recollected (saves 5 minutes). If the module set in `only_mutate` changed, delete the whole `mutants/` including stats: stats only record function hits for the mutated modules, so newly added modules would be recorded wholesale as 🫥 no linked tests.

```bash
uv run python scripts/mutmut_compare.py \
  --baseline research/mutmut-batch-1/baseline \
  --current mutants \
  --reworked reworked.txt \
  --equivalent equivalent.txt
```

`reworked.txt` has one mutant name per line targeted by this rework; `equivalent.txt` is optional, one mutant name per line judged to be an equivalent mutant. The script outputs the three-layer table and a to-review list; exit code 0 means pass, 1 means fail or items to review, 2 means inconsistent input (the baseline and this round have different sets of mutant names, a listed mutant is not in the baseline, etc.). With items to review, it does not pass: after reviewing per section 4, change the exit code of that mutant in this round's `.meta` to 1 if confirmed killed or 0 if confirmed surviving, then compare again for the final verdict.

| Layer | Pass condition | What to do on failure |
| --- | --- | --- |
| Mutants targeted by this rework | All exit code 1 | Became a timeout → review per section 4, 1 failed means killed; still survives → four-way triage (see below) |
| Mutants killed in the baseline | None became exit code 0 | Became a timeout → new-process review; 1 failed means the guard holds, only all passing counts as a regression; regression = broke another test |
| Mutants that survived in the baseline and were not reworked this time | Kills are only recorded; equivalent mutants still have exit code 0 | An equivalent mutant got killed → first review in a new process per section 4, but drop `-x` and run the same command 3 times in a row. Count failure points by the file and line number of the failing assertion in the traceback, not by test nodeid: a test only stops at its first failing assertion, so the same nodeid cannot tell a new assertion from an existing one. Same failure points in all 3 runs and all are assertions added this time → the judgment was wrong, not this round untrustworthy; re-judge and record it in the judgment table (if the added assertion claims more than that line in the judgment table, narrow the assertion and keep it equivalent); same failure points in all 3 runs but including existing assertions → likewise re-judge: surviving in the baseline only means that baseline round's stats did not link this test to the mutant, not that it is equivalent; all 3 runs pass, or failure points differ between runs → the whole round is void: an equivalent mutant cannot be killed, which means this round's subprocess results are untrustworthy (e.g. an abnormal exit recorded as killed); an equivalent mutant became a timeout or segfault → review per section 4, 1 failed means killed. After re-judging or editing `.meta`, compare again for the final verdict. Abnormal exit codes of other non-reworked mutants do not affect the verdict |

There is also an independent hard gate: `git diff --name-only origin/main` must not include `lib/` or `server/`. Once production code changes, `mutants/` is regenerated and mutant names shift, so the comparison itself no longer holds.

**Four-way triage** (when a target mutant still survives after rework; decide once, and do not withdraw added assertions): the assertion was not written correctly, or the assertion was not executed → keep fixing; it actually needs different inputs to kill → stop, on the grounds of "no coverage compensation"; it is actually an equivalent mutant → stop.

Running only some modules cannot detect the second layer; re-runs take the whole batch of modules.

### 5.1 Splitting one batch of modules into several rework tickets

The surviving mutants of one batch of modules are often split into several rework tickets merged across sessions; they share the same baseline saved in section 2, and each ticket is accepted separately:

- **Start with a tracer bullet**: for each batch's rework, first pick 2 mutants and take them alone through "change assertions → full gate → full re-run → three-layer acceptance → PR merge", and only after that passes rework the rest in bulk. The tracer-bullet two share the same baseline with the later bulk tickets; when the bulk tickets are accepted, they fall into the third layer "survived in the baseline and not reworked this time", and their kills are only recorded.
- `--reworked` lists only this ticket's list; `--equivalent` takes the whole batch's equivalent mutant list (every ticket must check that none of them are killed).
- Targets already killed by an earlier-merged ticket fall into the third layer "survived in the baseline and not reworked this time" in later tickets; their kills are only recorded and are not anomalies.
- Before branching for each ticket, `git fetch` and check that the baseline's modules have had zero changes on `origin/main` since the baseline commit: `git diff --stat <baseline commit> origin/main -- <module list>` is empty. A rework PR itself does not touch these modules (hard gate), but other commits on `main` will.
- When a module has been changed by another commit, the script refuses to compare on "different source hash for a function with the same name"; then the old baseline and judgment table are void for that module: mutant names are numbered by sequence within the function, so once the source changes the numbering shifts wholesale and judgment rows match no mutant. The remedy is to **re-run a baseline round for that module and re-judge its surviving mutants** (other unchanged modules keep using the old baseline; if the `only_mutate` module set changes, delete stats too); do not rebase the research branch — the research branch stores result snapshots, and rebasing cannot change the mutant names in the snapshots. The same applies when the change touches only a few functions: mutmut does not guarantee that other functions' numbering stays the same, so no partial reuse.

## 6. Known pitfalls

- **Incremental stats abort when newly added test files touch none of the mutated functions.** mutmut detects new tests and runs stats only for them; if this set of tests touches no mutant, it exits with `Stopping early, because we could not find any test case for any mutant`. Delete `mutants/mutmut-stats.json` to run full stats.
- **Don't run `uv run` inside `mutants/`.** uv builds a separate environment from that copy of `pyproject.toml`, mutmut isn't in it, and the trampoline import at the top of mutated modules fails straight away with `ModuleNotFoundError`. Use `../.venv/bin/python`.
- **`tests-for-mutant` only works from the project root.** It reads `mutants/mutmut-stats.json`, which it cannot find when run inside `mutants/`.
- **Don't lower `timeout_multiplier`.** Lowering it saves no time; it only makes confirmed real survivors get recorded as killed, hiding ineffective tests.
- **If −11 (segfault) appears in bulk, the whole round is void; don't review them one by one.** One known cause is on macOS: when there are no `*_proxy` variables in the environment, `urllib.request.getproxies()` calls SystemConfiguration via `_scproxy` to read the system proxy, and that framework segfaults outright in forked multithreaded subprocesses, hitting every test that constructs `openai.OpenAI` / `httpx.Client` (577 of 2186 in the second batch), while the same mutant reviews fine in a new process. `tests/mutmut_plugin.py` has replaced these two entry points with constant implementations; if it appears in bulk again, run a single one with `PYTHONFAULTHANDLER=1 uv run mutmut run <mutant>` to see the crash stack, and don't treat it as load or a fluke. Criterion: −11 should only appear sporadically (2 of 616 in the first batch).
- **Use the full test selection.** Running only `tests/unit` is 5.5 times faster, but about 45% of survivors are false, and each needs a review against the full suite, so the total cost is higher.

## 7. Cost reference

Measured on the first batch of 8 modules, 616 mutants in total:

| Item | Value |
| --- | ---: |
| Mutant density | about 1100 per thousand lines of source |
| Wall clock for one round (8-way parallel) | about 10 minutes for 8 modules / 616 mutants (excluding full stats, about 5 minutes); before timeouts were eliminated it was 17:47, as wall clock tracks the number of timeouts |
| New-process timeout review | 3 to 11 seconds each |
| Per-item judgment | about 3 seconds each |

## 8. Modules already covered

When choosing the next batch, pick from outside this table; modules already run are not re-run, since a second pass adds nothing (no-coverage and equivalent mutants would be reported again as is). Judgment tables and baseline snapshots are on each batch's research branch and do not go into `main`.

| Module | Batch | Ticket | Research branch |
| --- | --- | --- | --- |
| `lib/backends/generation_type_buckets.py` | Batch 1 group A | #2257 | `research/mutmut-batch-1` (`baseline/A/`) |
| `lib/infra/content_digest.py` | Batch 1 group A | #2257 | `research/mutmut-batch-1` (`baseline/A/`) |
| `lib/episode/episode_paths.py` | Batch 1 group A | #2257 | `research/mutmut-batch-1` (`baseline/A/`) |
| `lib/script/grid/splitter.py` | Batch 1 group A | #2257 | `research/mutmut-batch-1` (`baseline/A/`) |
| `lib/speech/speech_rate.py` | Batch 1 group A | #2257 | `research/mutmut-batch-1` (`baseline/A/`) |
| `lib/infra/text_metrics.py` | Batch 1 group A | #2257 | `research/mutmut-batch-1` (`baseline/A/`) |
| `lib/custom_provider/discovery.py` | Batch 1 group B | #2257 | `research/mutmut-batch-1` (`baseline/B/`) |
| `lib/backends/text_backends/openai.py` | Batch 1 group B | #2257 | `research/mutmut-batch-1` (`baseline/B/`) |
| `lib/backends/text_backends/instructor_support.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/backends/text_backends/ark.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/backends/text_backends/gemini.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/backends/text_backends/grok.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/backends/video_backends/gemini.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/backends/image_backends/dashscope.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/backends/image_backends/minimax.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `lib/custom_provider/backends.py` | Batch 2 pool 1 | #2297 | `research/mutmut-batch-2` |
| `server/agent_runtime/sdk_tools/asset_inventory.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/enqueue_assets.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/entry.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/episode_planning.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/generation_batches.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/patch_episode_meta.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/patch_project.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/rename_asset.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/retry_project_migration.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/workflow_plan.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |
| `server/agent_runtime/sdk_tools/workflow_status.py` | Batch 2 pool 2 | #2298 | `research/mutmut-batch-2` (`pool2/`) |

Of the 11 modules in batch 2 pool 2, 9 produced 0 mutants (`@tool` closures are skipped entirely); by the admission rules in section 2.1 such files should not have been picked in the first place, and they are listed here so they are not run again.
