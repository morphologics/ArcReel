---
status: accepted
---

# Deletion is recoverable, restore only adds, and deletion records are restored across versions via the project migration chain

Deletions within a project used to be hard deletes; structural removal left media on disk per `docs/adr/0003`, and `docs/adr/0080` left room for a project trash. Decision: every deletion within a project, whether initiated by the creator or the agent, becomes a deletion record in the project trash; a project deleted as a whole becomes a deleted project. Restore only adds, never overwrites, never blocks: a restored object keeps its original identity if that is free, and gets a new identity if it is taken; it is not merged with existing content, nor rejected because of conflicts. A deletion record saves the project's structural state before deletion; when an old record is restored, it is upgraded to the current schema through the project migration chain, just like archive import, before the object is taken out, so restoring still works across versions, and migration steps need not know the project trash exists.

## Considered Options

- **Restore domain objects back to their original place**: every kind of object would need its own set of conflict rules (the episode's source range has been taken by re-planning, an asset has been recreated with the same name, the script has been modified over many rounds), and the result of restoring might be messier than before deletion.
- **Recycle files only**: structural deletions could not actually be recovered, making the project trash useless.
- **Soft delete in place**: every read path would have to filter deleted objects, and deleted objects would keep occupying asset names and ranges of the whole-book source.
- **Deletion records store only the removed structural fragment**: the project schema changes in almost every release, so across versions either only files could be restored, or every migration step would have to rewrite fragments too; the migration chain can only handle complete projects.
- **Deletion records define their own version-independent format**: amounts to building another serialisation that evolves with the project schema.

## Consequences

- A deleted episode is restored as an episode with its own source text; even if it was originally cut from the whole-book source, it no longer occupies a range of the whole-book source.
- `docs/adr/0003`'s "structural remove only removes script references, and existing files in the project are kept" is superseded by this ADR; deleting an asset also no longer leaves its asset images in place.
- Old versions produced by overwriting stay in version history and do not go to the project trash.
- Storyboards and video units are an exception: numbers are only allocated, never reused (`docs/adr/0090`), so they always take new numbers on restore, and the original number is not reused even if free, so that clips and trims still referencing the original number do not attach to the restored entries.
- The agent can delete project content only through the project trash, and cannot directly delete media or version history; emptying the trash and permanent deletion belong to the creator alone.
