---
status: accepted
---

# Migration and import keep things as they are where possible: generation gaps only block generation, not backfill, and a whole project is rejected only if unreadable or unsafe

During upgrades and archive imports, backfill used to vet existing artifacts against current generation inputs: an empty description, a referenced asset without an asset image, or dialogue and voice-over mixed in a unit would all make the basis impossible to project, so the artifact was not registered and read as missing; each migration step also decided for itself which structural anomalies rejected the whole project. The result was that artifacts fully usable for business, which users had already paid for, "disappeared" after upgrade, data shapes produced by the normal flow in the past blocked whole projects, and the migration report existed only in the API, invisible to users. The rules themselves were stopping data that did not affect the business.

Decision:

- **Scope.** Migration and import only guarantee that data produced by the standard flow in the past goes through; anomalies caused by users' manual edits are not specially guarded against, but are not allowed to bring down the whole project either.
- **Generation gaps only block the act of generating; the basis can always be projected.** Missing inputs are recorded as absent in the basis; when the inputs are complete the basis is unchanged, and registered artifacts are unaffected. During upgrade backfill, every formal artifact whose file is usable is registered and judged current; archive import reuses the valid frozen basis in the archive (`docs/adr/0062`). Filling in the gaps later changes the basis, and the artifact is judged stale as usual. Existing videos of mixed-speech units (`docs/adr/0089`) are backfilled too, though the unit itself still needs re-planning. Legacy narration audio missing the TTS settings used at synthesis is not a gap: speech parameters are part of what it delivers, and if the record lacks them they genuinely cannot be computed (`docs/adr/0075`), so it is still not registered.
- **User content is never deleted or discarded.** Unreadable files are left in place and not registered; structural defects that can be repaired by deterministic rules are repaired, and those that cannot are left as they are.
- **Old shapes produced by the normal flow in the past are normalised by migration, not rejected.** When normalisation involves a trade-off, the other version is kept as a backup. When an imported script references undefined assets, placeholders are added.
- **Rejecting a whole project because of data is limited to two cases:** the project file, script, version records or artifact manifest cannot be read (not valid JSON, or missing content that determines its shape, such as the creation type); or the import package is unsafe. A migration that could not finish (insufficient disk space, write failure, etc.) is not a verdict on the data; it still blocks as before, the project is left as it was, and it is retried after the fault is fixed (`docs/adr/0022`).
- **The migration report records only what migration changed and what it lost:** trade-offs in normalisation, placeholders added, artifacts that could not be brought in. Generation gaps do not go into the report; users will be refused and shown the reason when they go to generate. Units needing re-planning do not go into the report either, since they already have persistent state. When the report is non-empty, the user is told once after an upgrade, and asked to confirm once before an import.

## Explicitly rejected

- **Register existing generated asset images by the image itself (same as uploaded images)**: later description changes would never be judged stale, losing currency tracking and adding a rule that forks by origin; the basis always being projectable already lets these images register normally.
- **Mark stale after backfill, or introduce a "pending confirmation" state**: the reasons `docs/adr/0062` rejected this still hold: a gap is not the same as "old", and every place that reads status would need an extra branch.
- **Confirm or modify the report item by item**: import only needs one informed consent; item-by-item operations would turn migration into a workflow.
- **List all generation gaps in the report**: most upgrades would pop up a long list of items the user does not need to deal with right now, burying the items that actually changed content.

## Consequences

- Narrows `docs/adr/0062`: not registering because the basis cannot be projected now applies only where the basis genuinely cannot be computed; backfill failure blocking the whole project now applies only where migration could not finish. Supersedes "projection failures go into the migration report and the user decides whether to regenerate" in `docs/adr/0075`, with the rule for legacy narration audio unchanged; supersedes "when there are gaps, currency judgement does not project an expected basis" in `docs/adr/0073`, with refusal at generation time unchanged; supersedes "block import when asset definitions are missing" in `docs/adr/0004`; narrows "any corrupted file lands as a needs-repair verdict" in `docs/adr/0064`, with corrupted drafts left as they are.
- In projects that have already been migrated, artifacts previously skipped because of gaps need to be backfilled once more; already registered entries are left untouched.
