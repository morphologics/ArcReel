---
status: accepted
---

# Project events keep two sources, explicit events and snapshot diffing; the project state projection is derived from the registry and polling is read-only

The browser learns that a project changed only through project events: each time it receives a batch it re-fetches the whole project, and it does not refresh separately after an agent turn ends. The snapshot used for diffing was originally a hand-picked shadow schema that recognised only a few fields of characters, scenes and props and a few fields of script entries; changes to products, derivatives, project settings, an episode's `source_range` / `hook`, narration source text, dialogue, `scene_description` and notes could not be detected, so after the agent made changes the UI stayed on the old state. Every new asset or skeleton type required coming back to edit it by hand (9e4298a9, a0152134, 9c208728), and when products and derivatives launched it was not updated at all. In addition, polling also synchronised the episode index and wrote back script migrations, so the read path was writing to disk, and only when a browser was subscribed.

We decided:

- **Both sources coexist.** Explicit events handle facts the snapshot cannot see (same-path byte replacement, task semantics), and snapshot diffing is the fallback for every other change that lands in project state; deduplication and catch-up-scan coordination between the two paths (2ed330d2) stays unchanged.
- **The project state projection is derived from the registry and compares whole entries.** Assets take the whole entry for each row of the asset type table (including products); episode entries, the overview, script entries and script-level fields are all taken whole; project settings take the entire contents of `project.json` minus the asset tables, episodes, overview and `metadata`. Only two things are excluded: `metadata`, which changes on every write; and script entries' `generated_assets`, which is used only to determine storyboard image / video readiness, with other changes belonging to explicit events, since otherwise every generation would be reported once by it and once by the task completion event.
- **Derivatives get their own events, with identity written as `base/derivative`.** This matches the identity used by generation task completion events, so the catch-up scan can cede the same change to that batch; when comparing base entries, the derivative table is excluded. The mechanism is derived from the derivative capability in the asset type table, not hard-coded to characters.
- **Polling is read-only.** The event service no longer synchronises the episode index, and both scripts and projects are loaded read-only.

## Explicitly rejected

- **Have every write entry point report before / after itself, retiring snapshot polling.** This would become a third event source and reintroduce baseline reconciliation problems with explicit events and the catch-up scan, i.e. exactly the missed broadcasts and false deletion reports that 2ed330d2 just fixed.
- **Compare against a whitelist of declared fields** (fields derived from the asset type table, `PROJECT_SETTINGS` and entry models). Fields stamped by the system itself (such as `voice_updated_at`, `end_frame_image`, `planning_cursor`) are not in any declaration, so a whitelist would still miss things and would still need long-term maintenance; whereas the cost of one extra report is just a redundant notice, and one missed report means the UI stays on the old state.
- **Synchronise the episode index during polling.** The agent's Write / Edit / Bash cannot touch `scripts/*.json` and `project.json`, and every script write path in the project manager already synchronises episode bindings itself; this synchronisation ran only when a browser was subscribed, which shows that no flow depends on it. Editing script files directly outside the application is not a supported scenario.

## Consequences

- When a new asset type or skeleton kind is added, snapshot diffing covers it automatically with no need to go back and edit the event service by hand; the frontend still needs route mappings and copy for new asset types, backstopped by an exhaustiveness test that iterates the asset type table.
- If an `episode_N.json` is placed in from outside the application, its entries will enter the snapshot, but the episode list will not automatically register that episode.
