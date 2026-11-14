---
status: accepted
---

# Image endpoints are split by capability into T2I/I2I dual slots; runtime gating is strict, with no implicit fallback

In the NewAPI/OneAPI relay ecosystem many models expose only generations (no edits support); passing reference images to a "generations-only model" gets routed to an edit call, which 404s remotely with ambiguous attribution. We decided to split the OpenAI image endpoint from a single wildcard into three — "wildcard / T2I only / I2I only" — with default configuration slotted by capability (`default_image_backend_t2i` / `default_image_backend_i2i` — optional capability-bucket override keys on top of the single default model; an unconfigured slot falls back to the default model, see `docs/adr/0054`); at runtime the path is chosen strictly by whether reference images are present, and a capability mismatch directly raises `ImageCapabilityError` with a stable code, **no longer doing** implicit fallbacks such as "fall back to T2I if the reference image can't be read" — failing early with a clear error beats ambiguous remote errors after automatic dispatch.

## Consequences

- Configuration becomes finer-grained: t2i / i2i can each specify a model; dual frontend dropdowns and a data migration follow. The two slots are presented in the capability-bucket form of `docs/adr/0054` and need not be filled in individually; this ADR's three-way endpoint split and runtime gating are independent of that form, and with both slots empty, gating is still strictly by capability.
- Orthogonal to `docs/adr/0001` (capability resolved at execution time): that one says "request shape determines t2i/i2i", this one says "split default configuration slots by capability + no fallback". The same three modes + error codes are reused by image backends such as dashscope / vidu.
