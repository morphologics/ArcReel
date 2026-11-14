---
status: accepted
---

# Generation entry points require references to be registered and have asset images: unregistered references block, and character originals no longer stand in for asset images

> In this ADR, the currency-judgement part of "fail at execution time before submitting to the provider, currency judgement does not project an expected basis, preview shows unavailable" has been superseded by `docs/adr/0098`: gaps still make execution and preview refuse, currency judgement projects the basis as usual, and missing items are recorded as absent.

Video generation is ArcReel's most expensive generation action. Previously two rules let it proceed even when reference images were incomplete: unregistered `@[名称]` (`@[name]`) in the body only warned and were silently dropped; registered characters without an asset image fell back to their original images. Decision: both are tightened at the **generation entry points** (single-item generation and whole-batch admission) — referencing an unregistered asset name (including references left over after an asset is deleted) blocks; a character, scene, or prop without an asset image blocks, and original images serve only as input for generating asset images and do not enter storyboard or video requests; a derivative without an asset image likewise blocks. Products keep ADR 0034: the original image is the acceptance anchor and goes directly into downstream references.

**Editing and saving are unchanged**: the editor and save paths still only warn, so authors need not create assets first while writing. ADR 0064's reasoning "don't force authors to fill in the asset table before writing" constrains only writing, not spending money; once blocking moves to the generation entry points, the two no longer conflict. This decision supersedes two passages in ADR 0064, "unregistered `@[名称]` (`@[name]`) only warn and don't block" and "if there is no asset image, use all of its original images"; the rest is unchanged.

**Wanting to use your own image directly as the character image**: go through the character asset image upload entry and register the image as the asset image, rather than relying on the original-image fallback.

## Execution time follows the same approach (revision)

Entry admission looks only at registration and asset image fields and does no IO; that stays the same. But between enqueueing and execution, assets may still be deleted, renamed, or lose their files. Previously, the executor silently dropped images when it hit such gaps and charged as usual, while the target-state planner gave up projecting and judged the freshly generated artifact stale — opposite conclusions in two places from the same data. Revised to: execution, currency judgement, and final prompt preview for storyboard images, asset images, derivative asset images, and grids share the same "generation input", classified by the kind of gap:

- **Reject**: reference not registered (`reference_asset_unregistered`); a character, scene, or prop is registered but has no asset image, or the asset image is unavailable — file absent, not registered in the manifest (`reference_asset_missing`); a product declares an asset image but it is unavailable — rejected with the same code, not falling back to using only the original (products without an asset image still use only the original per ADR 0034); an original image is declared but the file can't be read (`asset_original_missing`). Execution fails before submitting to the provider, currency judgement does not project an expected basis, and the preview shows unavailable.
- **Omit**: the previous storyboard image is unavailable. It is a continuity input derived by the system from storyboard order, not a reference declared by the author.

Criterion: **if an input declared by the author can't be read, reject; if an optional input derived by the system can't be read, omit it.** Missing originals were previously handled as "historical causes such as archive migration, not blocking"; but the original is the author's explicit intent for the appearance, and a product original is moreover the acceptance anchor of ADR 0034; generating without it means making the user pay for a result that doesn't match their intent, while the fix only requires re-uploading or clearing the field, so it is tightened too. A missing original gets its own code rather than being merged into `reference_asset_missing`, for the same reason this ADR separates unregistered from no-asset-image: the fixes differ.

An unavailable asset image and a missing original are judged only at execution time, and entry admission adds no IO for them: both appear only when data is damaged, and failing before payment already ensures "incomplete means no spending".

## Consequences

- For characters in existing projects that "have originals but no asset image", reference-to-video goes from generatable to blocked, and production status reports `未出图` (No image yet) at the asset step; the storyboard route only accepted asset images anyway and is unaffected.
- The rule "use the asset image if there is one, otherwise all originals" in `resolve_reference_assets` is kept only for products; the `missing` set of `derive_references_from_text` becomes the basis for blocking at generation entry points, while the editor preview still presents it as a warning.
- Deleting an asset no longer needs to automatically rewrite leftover references: leftover references block on the next generation with a prompt to fix, the same way out as names that were never registered.
- After the execution-time revision, existing characters and products whose originals are lost can no longer generate asset images; the original must be re-uploaded or the original field cleared; storyboard images referencing derivatives are no longer judged stale right after being generated.
