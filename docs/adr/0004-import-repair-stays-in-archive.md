---
status: accepted
---

# Import repair stays in archive and does not go through the unified save entry point; leaked shape constants converge onto existing sources of truth

> The part of this ADR about the import contract "block if asset definitions are missing" has been superseded by `docs/adr/0098`: undefined characters, scenes and props referenced by a script are always filled with placeholders and recorded in the migration report. The decision that import repair stays in archive is unchanged.

The `project_archive` import path performs a lot of "first-aid repair" on user-uploaded archives that may be incomplete or scrambled: recovering old version files from `versions/`, guessing paths in arbitrary layouts back to their correct locations, building a filename index for matching, and blocking if asset definitions are missing. An architecture walkthrough suggested adding `ProjectManager.restore_from_staging()` to own these repairs exclusively, so that "import path = save path = same guarantees". We **rejected** that suggestion: this first-aid logic is import-specific I/O, not ProjectManager's domain knowledge (PM only operates on installed, well-formed project directories); the import contract (try hard to repair + block if definitions are missing) is inherently different from the "no worse" semantics of the unified save entry point `_write_script_unlocked` (ADR-0002, which accepts old scripts that were already broken before the change), and forcing them together would mix the two contracts and also stuff roughly another 580 lines into the 94-method PM god module that candidate 6 aims to slim down.

The only real domain-knowledge leak in `project_archive` is "duplicated shape constants":

- **Canonical resource paths** (`resource_type` → path relative to the project, e.g. `characters/{id}.png`, `videos/scene_{id}.mp4`) were previously copied in three places — `MediaGenerator.OUTPUT_PATTERNS` (write side), `versions.py::_resolve_resource_path` (recovery side), `project_archive._canonical_resource_path` (import side). Converged into a single function consumed by all three.
- **content_mode → script field name dispatch** (narration uses `segments`/`segment_id`, drama uses `scenes`/`scene_id`) — archive previously re-copied, with a hand-written `if/else`, fields already declared in the model layer. Converged onto the skeleton registry in `lib/script/script_skeleton` (`SKELETONS` / `resolve_declared_kind` / `resolve_kind_items`); archive calls it instead of deriving it itself.

The `generated_assets` template is already delegated to `PM.create_generated_assets` (not a copy) and needs no handling.

## Consequences

- **Decoupled** from candidate 6 (splitting the PM god module): this decision does not touch PM, can land independently at any time, and need not wait for the PM split. The walkthrough's original statement that it "naturally pairs with candidate 6" is reversed here.
- The unified save entry point remains the single guard point for script writes (ADR-0002/0003); import is a **deliberate exception** — it does first aid on dirty archives before installing them into the project directory, and this layer of repair happens outside the unified entry point. This ADR is recorded precisely to stop future well-meaning changes that "merge import into the unified entry point as well".
- The canonical path function actually spans two families: media resources (storyboards/videos/grids/reference_videos) and character/scene/prop sheets — the path patterns of both families are now registered in a single table, `_PATTERNS` in `lib/project/resource_paths.py`, and consumed via `resource_relative_path` by MediaGenerator, the versions router and project_archive; additionally, `characters/refs/{name}.png` (reference_image) is not in that table.
