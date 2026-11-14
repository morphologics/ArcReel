---
status: accepted
---

# Execution-time predicates share a source with the actual execution path

Code that judges provider execution behavior (billing basis, whether the final cut has sound, validation pass-through) used to infer it from intermediate fields of ArcReel-side configuration — e.g. inferring whether the provider would produce audio from the configured resolution or quality tier — while the provider acts only on the fields in the request body. Between configuration and request construction lie transformations such as normalization, fallback, truncation, and layering of bases, and the inference can go wrong at each transformation point, producing inconsistencies such as "the request has sound but the final cut is silent, yet it is still billed at the with-sound price". Decision: **execution-time predicates always take their values from the fields that will actually be sent to the provider, sharing a source with request construction** — to judge whether some behavior happens, read its real input; when the same transformation is needed, call the same function rather than recomputing it at the predicate.

## Explicitly not adopted

- **Inferring provider behavior from ArcReel-side intermediate fields**: the transformations from intermediate fields to request fields (capability fallback, layering of billing bases, silent truncation) are invisible to the predicate, so edge cases will inevitably diverge.
- **Copying request-construction logic at the predicate**: the two copies of logic have no common source of truth; change one and miss the other; predicates should reuse the output of the construction path or its same transformation function.
- **Using the billing-basis field to double as the request-basis predicate** (or vice versa): each basis has different rules layered on (such as provider special cases always billed as with-audio), so letting one stand in for the other would be always true or always false for the special-case providers; both bases are exposed side by side and each is used for its own purpose.

## Consequences

- The value paths of capability, billing, and validation judgements must be traceable to request-body fields, or share the same transformation function with request construction; in review, code that "reads configuration to infer execution behavior" is challenged under this principle.
- The declaration layer is isomorphic: the video capability source of truth belongs, field by field, to the backend declaration (`VideoCapabilities`), sharing a source with request construction (see `docs/adr/0054`), which is this principle's counterpart on the static declaration side. For the declaration-layer source of truth of other capability fields such as images, see `.claude/rules/provider-capabilities.md` "供应商能力与契约" (Provider capabilities and contracts).
- On the video side, duration constraints and quotes are evaluated by the resolution actually sent in the request; see `docs/adr/0086`.
