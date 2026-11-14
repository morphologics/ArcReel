---
status: accepted
---

# Structured output: selective fallback rather than a unified entry point; fallback mechanisms differ by wire-channel availability

Models that natively support `structured_output` go through native wire-level structured parameters (`response_format` / `response_json_schema`), which is faster and more accurate. Fallback is triggered only in two situations: **up-front capability gating** — models the registry does not declare as supporting it (including unregistered models; conservative fallback — better to fall back than to call a native API that will error); **after-the-fact runtime re-validation** — after a native call returns HTTP 200, check whether the response really satisfies the schema, because third-party relays/proxies may silently ignore wire-level parameters and return prose or non-conforming JSON (re-validation uses `strict=False` to tolerate coercible values, avoiding mistakenly falling back — and double billing — on valid responses the provider already accepted; when fallback is triggered, the native call's tokens are merged into the fallback result so nothing goes unrecorded). We decided not to route all backends uniformly through the fallback path — wrapping another layer around the native path only makes it slower and less accurate; alternatives such as PydanticAI / BAML are too heavy or have incompatible DSLs.

The fallback mechanism differs by backend, and the criterion is **whether the fallback must bypass the wire channel**: openai / ark go through Instructor (pure functions in `lib/backends/text_backends/instructor_support.py`: inject the schema into the prompt + parse + validate with retries, transparent to upper layers); gemini goes through prompt injection inside the backend (write the schema into the prompt and resend a plain-text call, strip markdown fences before validating, retry with error feedback on failure) — its fallback trigger scenario is precisely relays silently ignoring wire-level parameters, and Instructor's genai integration (JSON/TOOLS modes) likewise lands on wire-level parameters such as response_schema / function calling, which the same relay would ignore; writing the schema constraint into the prompt is the only mechanism that does not depend on wire parameters.

For the composition of retry and fallback (the retry decorator wraps only a single network call; fallback carries its own retries) see `docs/adr/0047`.

## Consequences

- One more third-party dependency, and capability checks must consult the registry (gated by model capabilities, paired with `docs/adr/0013` "capabilities declared at the model level").
- Models without native structured output, and scenarios where native parameters are silently ignored by a relay, can both produce structured results without breaking the native path.
- Fallback no longer equals only Instructor: gemini's fallback validation/retry logic lives inside its backend (`_prompt_json_fallback`) and does not go into `instructor_support`.
