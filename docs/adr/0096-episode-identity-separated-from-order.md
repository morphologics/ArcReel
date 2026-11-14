---
status: accepted
---

# Episode identity is separated from airing order: the episode number is demoted to an internal ID

The episode number used to be at once an episode's identity (running through paths, entry IDs, media file names, the artifact manifest, tasks and links), its display name and its airing order; identity must be frozen, so the display name and order were frozen with it, resulting in skipped numbers, gaps left by deleted episodes, no insertion in the middle, and episodes from other sources wedged between cut episodes. We decided to separate the three: the existing integer episode number is demoted to an internal episode ID, only allocated and never reused, taking the next number after the highest in the project's history; the entry order in the episode ledger is the airing order; creators identify episodes by title and by an episode's position in the airing order, and the episode ID does not appear in any text visible to creators.

## Considered Options

- **Renumber on demand**: rewrites all artifact names at once, any failed step breaks links, and skipped and empty numbers would reappear afterwards anyway.
- **Switch to opaque IDs**: renames all existing artifacts, only to get the same stability as keeping the integers.
- **Multiple numbering lines by episode source**: an episode's source can change (an episode without source text can later have source text added), and having its own source text does not mean it is a side story; multiple numbering lines would ultimately still have to be merged into one airing order.

## Consequences

- Entry IDs carry the episode ID as a prefix (`E3S01`), which may appear in the episode that is 5th in airing order, so creator-visible places show only the in-episode part, qualified by the episode title when outside the episode page.
- Episodes cut from the whole-book source are always ordered by source position among themselves; episodes with their own source text and episodes without source text can be placed anywhere, including between two cut episodes; when planning changes cut episodes, they are placed by the rules of `docs/adr/0032`.
- Candidate episodes from re-planning are always assigned new episode IDs, and old artifacts are not re-attached to new episodes by position; an episode ID is kept only for manual cuts where the old and new are genuinely the same episode (see `docs/adr/0032`).
- The highest number in history must cover every record in the project that still carries an episode ID (the ledger, leftover artifacts on disk, task and call records); otherwise a reused episode ID would let a new episode claim an old episode's artifacts and costs.
- Agent tools refer to an episode by episode ID, but speak to creators in terms of title and position.
