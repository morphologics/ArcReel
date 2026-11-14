---
status: accepted
---

# The file order of the whole-book source is recorded explicitly; changes remap cut episodes per file, by text alignment

The whole-book source can be made up of multiple files joined together. Decision: the file order is recorded explicitly by the project and adjustable by the creator, and file names do not determine order; a cut episode's source range is contiguous along this order, with start and end each recorded as "file + offset within file", and may span files; adding, removing, replacing, editing or reordering a file affects only the cut episodes whose source ranges touch that file; when a file's content changes, text alignment between before and after the change maps those episodes' boundaries onto the new text, and only episodes whose in-range text actually changed are marked stale. Changes to the whole-book source do not require re-planning from episode 1. The most common project has only one source file, and invalidating the whole book's cuts for one typo fix would be a cost out of all proportion to the change.

## Rules

- The file list and order of the whole-book source are recorded in the project. Newly uploaded files are appended at the end, and the creator can reorder them; existing projects have them recorded once in the original file-name order.
- A file's identity is its item in the list. Replacement keeps its position, file name and source file type, and the type can be changed on replacement (see `docs/adr/0036`).
- Source ranges can span files; file boundaries are not episode boundaries. The AI planning window reads along the file order and is truncated at a change of source file type; a cut episode's type is the type of the file where its source range starts, and changing the type does not touch the ledger (see `docs/adr/0036`).
- When a file is replaced or edited, the normalised text before and after the change is aligned, and the boundaries of each episode's source range are mapped:
  - episodes whose in-range text has not changed only have their offsets shifted, and their status is unchanged;
  - episodes whose in-range text has changed take the mapped range and are marked stale: existing artifacts, and, where there are no artifacts yet, the title, hook and episode outline, were all made from the old text;
  - episodes whose entire in-range text has been deleted are handled as a removed cut: those with artifacts become episodes without source text and are marked stale, those without artifacts are removed directly (see `docs/adr/0032`).
  - New text inserted exactly at the boundary between two episodes belongs to the earlier episode; new text that falls outside any cut episode (before the first cut episode, after the last, or in empty stretches between cut episodes) is uncut source text.
- Deleting a file is equivalent to deleting all its text and is handled by the rules above: cut episodes entirely within this file are handled as removed cuts, and episodes extending into this file keep their remaining parts and are marked stale.
- When a new file is inserted at the boundary between two episodes or outside cut episodes, the new file is uncut source text and no episode changes; when the insertion point falls inside an episode that spans files, the new file belongs to that episode, which is marked stale.
- On reordering, the cut episodes in a moved file move as a block in the airing order, keeping the source order among cut episodes; episodes from other sources follow the story into place by the anchor rules of `docs/adr/0032`. Episodes whose content has not changed are not marked stale. An episode that spans the boundary of a moved file keeps only the part in the file where it starts that is still contiguous after reordering; the rest becomes uncut source text, and the episode is marked stale.
- Every file that has had cut episodes registered keeps a normalised text snapshot inside the project. When a file is changed outside the service and its fingerprint does not match the record, the snapshot is used as the pre-change text, the same alignment mapping is applied, and the creator is told which episodes were affected.
- The agent's writes to `source/` go through the same service command as the web, and cannot write directly with file tools or Bash (see `docs/adr/0026`).

## Considered Options

- **Re-plan from episode 1 whenever any registered file changes**: with only one source file, even a single typo fix would mean redoing the whole book.
- **Release all cut episodes in a file when its content changes**: simple rule, but for single-file projects it equally means redoing the whole book.
- **Source ranges do not span files**: the affected range is easiest to compute, but file boundaries become mandatory episode boundaries; in novels split by chapter, story arcs often span chapters and could only be hard-cut at chapter ends.
- **Sort by file name**: `第10章.txt` (Chapter 10) would sort before `第2章.txt` (Chapter 2); uploading a file that sorts before already cut ranges would mean starting again from episode 1.
- **Forbid reordering files containing cut episodes**: reordering does not change any episode's content, so forcing the creator to release and re-cut first is unnecessary.
- **Store no snapshots, and have changes outside the service mark all cut episodes of the whole file stale and block planning**: desktop users editing files directly is not uncommon, and the alignment mapping has to be implemented anyway; storing snapshots adds only one more write point.

## Consequences

- The purpose of the source fingerprint is to detect changes made outside the service; detection triggers the alignment mapping and no longer rejects planning.
- Alignment is approximate: after large rewrites the mapped ranges may not contain complete story arcs; those episodes have been marked stale, and the creator reviews the boundaries.
- Snapshots double the space used by the source, which is negligible for text.
