---
paths:
  - "tests/**"
  - "packages/*/tests/**"
  - "frontend/src/**/*.test.*"
  - "frontend/src/test/**"
  - "frontend/src/__mocks__/**"
  - "frontend/e2e/**"
---

# Testing

Structural rules are enforced by `scripts/audit_tests.py --check` (the rule list is in that script's module docstring), which gives fix guidance when a rule is hit; this document covers only what it cannot decide.

## Tiers and location

### Choose the tier by the dependencies a test case actually touches

| Tier | Allowed to touch |
|---|---|
| `unit` | In-memory objects, `tmp_path`, and read-only access to files committed in the repo; file writes land only in `tmp_path`; no real DB, subprocesses or network |
| `integration` | Real DB, runtime file system reads/writes (data root, user directories, etc.), bundled ffmpeg subprocesses |
| `e2e` | Real external services (remote APIs, LLM calls); skipped in CI by default |

A missing tier directory, or a unit-tier test using the database fixtures from `tests/conftest.py` (which get `uses_db` injected), is rejected at collection time; connecting to a real DB, spawning subprocesses or accessing the network on one's own without those fixtures has no mechanical check and is left to review. When a unit-tier test spawns subprocesses (git, ffmpeg) or accesses the network, the `-m unit` fast loop slows down and starts depending on the local environment; conversely, a test that touches nothing but sits in the integration tier gets pushed out of the fast loop and lands in the wrong CI shard. Reading files committed in the repo (prompt rules, skill text, docs, etc.) is fast and guaranteed to exist with the checkout, so it neither slows the fast loop nor depends on the local environment, and therefore stays in the unit tier.

Location conventions: alembic migration tests use one file per migration script, live in `tests/integration/lib/db/migrations/`, and keep using SQLite; tests for workspace sub-packages go in `packages/<sub-package>/tests/`, laid out following the sub-package's source directory structure, without tiers; tests that assert on sub-package output after it has been rendered into natural language go in the main repo's `tests/unit/lib/i18n/`.

## Test doubles

### Use test doubles in priority order, and replace only repository boundaries

Real objects (in-memory SQLite, `tmp_path`) > hand-written doubles in `tests/fakes.py` > Mocks with `spec` / `autospec` > bare `MagicMock` / `AsyncMock`. Mocks replace only repository boundaries: third-party SDKs, network transport, subprocesses, the file system, the clock. Dependencies inside the repo use real instances. Once an in-repo object is replaced with a double, the contract between it and the object under test has no test coverage, and tests still pass even if the production implementation is broken.

The same applies on the frontend: internal child components render for real by default; only mock heavyweight components (virtualization, animation, canvas), components with side effects (sending requests, starting timers), and purely presentational components unrelated to the test. Once the child component that carries the interaction under test is mocked out, the test is left with only the rendered shell.

Exception: the double replaces one of the boundaries above, or is used to trigger a branch a real object cannot trigger (exception, timeout, external failure).

### Add a seam when you need to control internal behaviour; a seam is an explicit parameter with a production default

A seam is a constructor parameter or keyword argument with a production default that does not change production behaviour, e.g. `retry_async(operation, *, clock=..., jitter=...)`. It applies to polling clocks, intervals, backoff, HTTP probe clients, the file system and subprocesses. A replaceable module-level global does not count as a seam: it makes tests share mutable state. Testability refactors likewise must not change production behaviour; on the frontend only extracting pure functions and hook-level structural splits are allowed.

### Reset hooks for process-level caches are the public functions `reset_for_tests()` / `reset_<name>_for_tests()`

When a production module uses a process-level cache such as `functools.cache`, expose a public reset hook (e.g. `lib.infra.app_data_dir.reset_for_tests`) that only clears the cache and does not change production behaviour. This is a transitional form: new code should prefer injecting cached dependencies as parameters.

### Intercept outbound HTTP with respx; use `dependency_overrides` for FastAPI dependencies

respx keeps the real httpx client and intercepts at the transport layer (traffic from `AsyncOpenAI` is captured too), so assertions are on the real serialized request. Route dependencies are replaced with `app.dependency_overrides`, not patched.

### Interface doubles for frontend page-level suites are recorded from the real backend

Responses under `frontend/e2e/fixtures/recorded/` are generated only by `pnpm e2e:record` against the real backend; do not hand-write protocol stubs, and do not hand-edit recorded files. A backend PR that changes an interface shape must re-record at the same time, so the shape change shows up in the diff of the double files. Hand-written doubles only reflect the author's understanding of the interface: if the backend's actual fields, frame order or query shape change, the page-level suite still passes and the regression only surfaces at runtime. Stress variants such as long text or many items are written in the scenario's `api` field.

Exception: PRs that do not change interface shapes need not re-record. Any change to a recorded file must still come from `pnpm e2e:record`.

## Assertion value

### Each test case protects one observable contract

Assertions land on real output: the type and attributes of return values, the real request captured by respx, observable state. `audit_tests` already blocks tests that assert only on double call records, patch private symbols, or duplicate a case in the same file; it cannot detect the following four forms, which are for review to point out:

- **Duplicated and weakened**: covers the same path as another test, but its assertions are a strict subset of that one's.
- **Over-specification**: asserts implementation details such as log text, dictionary key order or private attributes, rather than the contract.
- **Severely unbalanced setup and assertion**: dozens of lines of preparation, ending with an assertion only that some call happened.
- **Assertion helpers that only assert on double call records**: hiding the `audit_tests` criterion inside a helper.

Such tests do not protect behaviour, yet turn red en masse during refactors; their value is negative. When one is found, delete or rework it rather than adding assertions to pad coverage: if the behaviour is already substantively covered by other tests, or there is no real branch or contract constraint, delete it outright; if it is worth protecting, rework it to assert the contract, and if that needs a seam in production code, do it in a separate batch.

Coverage and mutation scores are only signals: do not write tests for the numbers or to kill mutants, and a coverage drop caused by deleting meaningless tests is acceptable. The batch process for mutation testing is in `docs/testing/mutmut-runbook.md`.

## Timing

### Drive waits, retries and timeouts through a clock seam or an event handshake

Real-time waits (`time.sleep`, fixed `setTimeout`) make tests slow and cause intermittent failures under high load. Treat intermittent failures as ordinary defects and fix them on the spot with a clock seam or event handshake; those that cannot be fixed or are not worth fixing are deleted or reworked per "Each test case protects one observable contract". Do not introduce automatic retries (pytest-rerunfailures, CI job-level retry); they mask failures that should be exposed. Frontend tests keep vitest's default 5-second timeout; individual slow tests explicitly override it and state the reason.

Exception: probabilistic stress tests (real concurrency + real time) may exist only once registered here. The only one currently registered is the atomic-write stress test in `tests/integration/lib/project/test_project_manager_concurrent_save.py`.

### When waiting for an async process, use its last step as the readiness signal

Async processes often complete in several steps, e.g. switching in-memory state, persisting to the DB, broadcasting; or an effect subscribing only after the element renders, or a debounced refresh settling only after data arrives. If a test proceeds as soon as one of these steps becomes visible, the later steps interleave with the assertion or the next action and fail intermittently under high load. Take the wait condition from the last step of the process: the final-state broadcast, the dequeue event, the subscription taking effect, the refresh settling. If other tests of the same process use the same waiting pattern, fix them too.
