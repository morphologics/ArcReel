---
status: superseded by ADR-0104
---

# Provider capability declarations converge to the model level; provider capabilities are a derived read-only view

Different models from the same provider have different capabilities (e.g. Ark lite text models do not support structured_output, yet being overridden by a top-level declaration causes native calls to error), so declaring `media_types` / `capabilities` at the provider top level inevitably makes declarations drift from the real capabilities of individual models. We decided to declare capabilities (the capability enums for text/image/video respectively) on `ModelInfo` (model level); `ProviderMeta`'s `media_types` / `capabilities` become read-only `@property` values aggregated from all its models and are not stored independently — the source of truth for capabilities is the model; provider-level capability is a derived view and cannot be written.

## Consequences

- Capability queries all have to aggregate from models, in exchange for a single source of truth; provider resolution, automatic inference, the frontend model picker and billing dispatch all depend on this data shape.
- The registry's capability declarations are **descriptive metadata**; the capability set constructed by the backend at runtime is what actually decides the parameters sent to the upstream SDK, and the two can intentionally disagree (consistent with the "declaration vs execution" layering of `docs/adr/0001`).
- Adding a model = writing one capability declaration in its `ModelInfo`; no need to change the provider top level.
