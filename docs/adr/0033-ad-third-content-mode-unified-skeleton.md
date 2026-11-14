---
status: accepted
---

# Ad/short film as the third content type: the skeleton is derived from the generation route

> The current architecture uses self-contained `video_units[]` to unify the reference route across all three content modes; for the current contract of skeleton and route see
> [ADR 0045](0045-script-skeleton-registry-dual-resolvers.md) and
> [ADR 0055](0055-generation-route-locked-at-creation.md). The
> `shots + reference_units` derived grouping in the first version of this ADR only explains historical data and is no longer a runtime contract.

Ad/short film mode (with product-promotion short videos as the main scenario) produces a single video rather than a multi-episode series and needs to enter the type system. Decision: `ad` lands as the third value of content_mode; the generation route remains an independent dimension, and the project-level `generation_mode` decides whether the script uses the storyboard skeleton or the reference video skeleton.

## Decision

- **`ad` is the third value of content_mode**: reuses all mechanisms dispatched by content_mode (profile variant `CLAUDE.ad.md`, SCRIPT_SHAPES, the immutable-after-creation constraint, per-episode statistics dispatch in the project summary).
- **The skeleton is derived from the generation route**: the storyboard path uses flat `shots[]` (`shot_id`, E1S{n}), with each shot carrying `section` and first-class voice-over copy `voiceover_text`; the reference_video path uses self-contained `video_units[]`, with each unit itself carrying its body text, arranged duration, references and output ownership. The runtime does not read or write back the old `reference_units`.
- **ad only opens storyboard and reference_video**: grid is not opened — the per-cell resolution of a grid conflicts with the product high-fidelity goal, and its art-style consistency value is carried in ad by product/style references.
- **Always carried as a single episode**: an ad project's episodes are always `[{episode: 1, …}]`, and the script is `scripts/episode_1.json`; per-episode machinery (status/archive/version/cost/export) has zero structural changes, and the frontend hides episode semantics for ad. A future "one product, multiple variants" would extend as one episode = one variant.
- **Shot duration constraints are injected dynamically by generation_mode**: the storyboard path hard-enumerates per supported_durations (a model capability constraint); on the reference path the unit holds a 1–300 second arranged duration, and the generation pre-check then projects it onto the provider's request tiers.

## Historical decision and reason for replacement

The first version had both ad routes use `shots[]` as the content truth, and persisted reference_video grouping as a `reference_units` index that only referenced `shot_id`. That shape required the runtime to hydrate a two-layer structure and also split the reference route from the self-contained unit contract of narration/drama. The current design instead has the reference route of all content modes uniformly use `video_units[]`; the old `shots + reference_units` is read only once by the project schema migration, and after migration business code only consumes the new skeleton.

## Consequences

- VALID_CONTENT_MODES, SCRIPT_SHAPES, the profile manifest, the data validator and the creation wizard extend with the third value; for ad-specific fields (`target_duration`, `brief`, the `products` bucket) see the proposal and ADR 0034.
- The episode ledger redesign must treat ad as an always-single-entry ledger/exempt from split planning, and must not assume content_mode is binary.
- Existing ad reference scripts have their provable unit identity, boundaries and historical outputs preserved by the startup migration; after migration, no dual reading of the old structure or re-derivation entry point is kept.
