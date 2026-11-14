---
status: accepted
---

# Video duration uses per-model supported_durations as the single source of truth; values are passed through as-is, and failing to resolve fails loud

Bucket mapping inside backends (silently turning 6 into 8) and the implicit `or [4,6,8]` fallback were the root cause of the incident "chose 6s but it was changed to 8s, then rejected by the other end as invalid". We decided each video model declares a non-empty discrete `supported_durations`, and the three consumption points (script prompt / frontend picker / video request body) consume it from the same source; each backend removes duration bucket mapping and normalisation, the request body passes the value through as-is, and out-of-range values are reported back by the other end with a 400; when the resolver gets an empty set it raises `ValueError`, and all implicit fallbacks are removed — better to fail loud and guide the user to fix it on the configuration page than to silently tamper with the user's/LLM's choice or mask a configuration defect.

## Consequences

- The schema layer does not introduce a continuous-range type; instead the compromise is fully expanded lists + continuity detection in the frontend.
- When a custom provider omits it, it is prefilled from a model_id heuristic preset table (falling back to a conservative default on a miss); the Alembic backfill migration inlines a copy of the preset snapshot rather than importing the module, to keep historical migrations deterministic.
- One restricted exception: because Vidu's API lists widely differing valid duration sets per endpoint, it keeps `_coerce_duration` endpoint-level nearest-value correction + a warning; this is a different dimension from the model-level single source of truth.
- Fail loud guards against "missing model declarations", not "the duration dimension doesn't exist on this endpoint". When a ComfyUI endpoint's `frames` is unbound or the frame-rate source can't be read, the entire duration dimension is not driven by ArcReel (see `docs/adr/0082`), and the empty set is a valid state: it carries a separate flag distinguishing it from a missing declaration, all consumption points let it through based on that flag, and this decision's fail-loud stance is not relaxed because of it.
