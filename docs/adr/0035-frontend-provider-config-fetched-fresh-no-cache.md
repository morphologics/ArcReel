---
status: accepted
---

# The frontend fetches provider configuration at the point of consumption and does not persistently cache mutable configuration

The module-level caches in `provider-models.ts` (`_cache` for built-in / `_customCache` for custom) were never invalidated — `invalidateProviderModelsCache` had zero callers. This was the root cause of "I added 10s to a model in provider settings, but the duration picker in project settings still shows only the old values": the frontend cache had become a second source of truth that could drift, violating ADR 0018 (`supported_durations` per-model as the single source of truth) and ADR 0013 (model level as the source of truth for capabilities). We decided to make both fetchers completely stateless — every call fetches `GET /custom-providers` and `GET /providers` directly, and all module caches, in-flight promises, and invalidate functions are deleted — eliminating this whole class of staleness structurally through "re-fetch on every consumption", rather than relying on callers remembering to invalidate.

## Consequences

- Entering settings / the creation wizard / the canvas each costs one extra fetch of the provider list (small JSON). No thundering herd occurs: consumers each call once at the top level of the page and pass the result down via props; no sibling components fetch the same fetcher concurrently, so no in-flight deduplication is introduced.
- The zustand store approach was rejected: its benefit of "reactive same-screen consistency" does not apply under this application's routing structure (the settings page and the project page are never on the same screen), so it would be over-engineering. Only when a hot path with N concurrent consumers actually appears will it be time to introduce a store.
- `config-status-store` uses its own fetch plus its own `refresh()` (already called after changes). It only produces the configuration red-dot indicator and `availableMediaTypes`, and does not expose model lists to the duration/capability pickers, so it is outside the scope of this constraint.
- A stored `default_duration` that goes out of range because a model's durations shrank is backstopped by the backend resolver failing loudly (ADR 0018); a "stored value is no longer valid" hint in the frontend picker is a separate topic and out of scope here.
