---
status: accepted
---

# Assets use name as identity; renaming goes through a cascading rewrite rather than introducing stable ids

Assets (character/scene/prop/product) use name as their unique identifier, and scripts, prompt rendering, and `@[名称]` (`@[name]`) mentions all use the proper name as the medium — the script is a document read and written by the LLM, and reading proper names directly is core usability. Renaming is therefore defined as an atomic cascading transaction: in one go it updates the asset bucket key, all script references across all episodes (reference arrays, speaker, mentions), associated files named by name and their path fields (invariant "file stem = asset name"), and the reference-image names and paths frozen in grid records (the basis of grids and cut-cell storyboards is replayed from them, see `docs/adr/0062` "分镜图与宫格的依据不记资产名" (The basis of storyboard images and grids does not record asset names)).

## Explicitly not adopted

- **Introducing stable asset ids, with script references storing ids instead**: renaming would reduce to changing a display field and never cascade, but it would need a full script schema migration and migration of existing projects, and every layer — LLM-generated scripts, mentions, prompt rendering — would need dereferencing added, hurting proper-name readability. Rejected — id-ification spreads complexity across the high-frequency paths of every generation/render, while cascading concentrates complexity in the low-frequency rename operation.

## Consequences

- If the target name conflicts with an existing asset of the same type (in the NFC coordinate system), the rename is rejected; merging is a different semantics and is not carried by renaming.
- The global asset library and projects are in a snapshot-copy relationship, and renaming does not propagate between them.
- Renaming is not mutually exclusive with in-progress generation tasks: a task running at the moment of renaming may render under the old name or fail, and rerunning fixes it (under single-user deployment the probability is out of proportion to the cost of protection).
