---
status: accepted
---

# Layered data root: projects move into `projects/`, and prefixes are no longer used inside the data root to distinguish projects

The data root was originally just the projects directory, and when a configurable data root was introduced it was simply treated as the data root: projects sat on the same level as system state such as the database, the global asset library and user memory, relying on a `.` / `_` prefix to declare "this is not a project". Logs and Vertex credentials, which did not fit, were pushed into the code directory and the parent of the data root; directories that forgot the prefix were at various times listed as projects and could not be deleted; and "what is a project" was determined in several places, each with its own rule. We decided to layer the data root: projects move into `<data root>/projects/<project name>/`, the remaining runtime data sits alongside it under unprefixed names, and all runtime data managed by ArcReel itself lands inside the data root (the Claude SDK's own configuration directory is excluded; see `docs/adr/0074`).

```
<data root>/
├── projects/<project name>/
├── global_assets/
├── users/<user_id>/memory/
├── arcreel.db
├── logs/
├── vertex_keys/
├── trial_runs/
└── runtime/
```

- **Nested in place**: the data root is still the directory the deployer has already configured, mounted and backed up, and upgrading does not change deployment configuration. The default data root is therefore still the git-ignored `projects/` in the repository, and projects land in `<repository root>/projects/projects/`.
- **Projects are determined in only one place**: only directories under `projects/` with a valid name and a `project.json` are projects; nothing else in the data root is.
- **Agent read access to the data root is denied by default**: only the current project and the current user's memory are allowed, and new system directories are protected without needing registration (`docs/adr/0026`).
- **Downgrade is not supported**: older versions would treat the new directories under the data root as projects.

This ADR supersedes three points in `docs/adr/0074`: "user memory lives in `<data root>/.arcreel/users/…`", "`_`-prefixed directories under the data root denote cross-project shared asset libraries", and "the user memory directory is derived from `projects_root`": user memory is now derived from the data root; `.arcreel/` inside a project is unchanged.

## Explicitly rejected

- **Keep the flat layout and only centralise project determination**: every new system directory would still have to remember the prefix; logs and credentials would still have nowhere to go, and the code directory would keep holding runtime data, which is not writable in deployments where the install directory is read-only.
- **Switch to a new default data root (e.g. `<repository root>/data/`)**: every Docker and custom deployment would have to change its volume mounts, and missing one would show up as "all projects disappeared".
- **Put system state into hidden directories under the data root (e.g. `.arcreel/trial_runs/`)**: this continues the prefix convention and only hides the same problem deeper.
- **Keep existing names such as `_global_assets` and `.arcreel.db`**: after layering, prefixes no longer serve to distinguish anything, and keeping them would let the old convention that "prefixes have meaning" keep being copied.
