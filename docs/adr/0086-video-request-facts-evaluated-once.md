---
status: accepted
---

# Video request facts are evaluated only once: the read side and execution side differ only in identity source

The execution facts of a video generation request (the execution model, the resolution sent, the narrowed selectable durations and why, whether duration is fixed by the endpoint, the audio-track form) used to be worked out separately by quoting, pre-check, script planning, the UI, the agent and execution, and they diverged: the seconds that pre-check asked to confirm differed from what execution actually requested, and quoting used one resolution while charging used another. We decided to gather them into "video request facts", evaluated once per request by "project × route × task type bucket × identity source", with every consumer only reading the result; the read side uses the execution model resolved from the current configuration as its identity, the execution side uses the invocation channel actually constructed as its identity (`docs/adr/0049` (3)), and the rest of the evaluation is identical.

Three rules follow from this:

- **If resolution is not set, it is not sent, with the same rule for both video routes**: the reference-to-video route no longer sends a fallback resolution; duration constraints and quoting are evaluated against the resolution the request actually sends (`docs/adr/0019`, `docs/adr/0056`).
- **If the i2v bucket is unavailable, the result is "unknown"**: in reference-to-video projects, the tiers for units without reference images come only from the i2v bucket; when that bucket cannot be resolved, it is presented honestly as "unknown" with a reason given, rather than falling back to the r2v model's tiers.
- **Execution no longer lets empty values through**: when the duration dimension fails to resolve at execution or narrows to an empty set, execution blocks and records a failure code (the legitimate empty set for duration fixed by the endpoint is excluded; see `docs/adr/0082`), instead of following "capability lookup failed, so let the empty value through" and leaving it to the provider to surface as a paid failure.

## Explicitly rejected

- **Soft fallback to r2v tiers when the i2v bucket is unavailable** (so as not to block the creative flow): at execution, such units will inevitably be blocked, so falling back would only let the planning stage approve a plan that cannot be executed.
- **Keep the per-provider fallback resolution for the reference-to-video route**: the reason for that exception no longer exists; the fallback value would override the literal size of a ComfyUI workflow (`docs/adr/0082`), fail on endpoints that only accept specific tiers, and let quoting and charging each take a different value.
- **Have the read side and execution side assemble separately, sharing only the underlying rules**: each assembly fills in its own defaults, and divergence happens quietly when fields are missing.
- **Keep a mirror check of "does the unit have reference images" in the UI**: it looks only at declared references, not at usable reference images, and would contradict the server's bucket decision.

## Consequences

- After reference-to-video projects without a resolution set switch to not sending one, the execution fingerprint changes accordingly, and already selected videos no longer hit free reuse at the same tier; artifact currency is unaffected (`docs/adr/0062`).
- The video lane's fallback resolution and the empty-value pass-through for the duration dimension in `docs/adr/0049` are superseded by this ADR; `docs/adr/0019` no longer has an exception for the reference-to-video route.
- Testing: tests for consumers such as quoting, pre-check and execution construct results with `make_video_request_facts` from `tests/factories.py` and construct `VideoRequestFactsFailure` directly as the failure object, rather than faking at the capability resolver layer or hand-building capability dicts; capability evaluation itself is tested with a real `ConfigResolver` plus a test database.
