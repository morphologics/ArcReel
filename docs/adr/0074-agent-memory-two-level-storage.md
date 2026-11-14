---
status: accepted
---

> Three points — "user memory lands in `<数据根>/.arcreel/users/…`" (`<data root>/.arcreel/users/…`), "`_`-prefixed directories under the data root denote cross-project shared material libraries", and "the user memory directory is derived from `projects_root` + `user_id`" (after the data root and project directories were separated, it is derived from the data root instead) — have been superseded by `docs/adr/0088`; the rest remains in effect.

# Agent memory is stored at two levels: project memory uses native auto memory redirection, user memory is self-built under the data root

The embedded creative agent's "agent memory" has two levels, project memory and user memory, but Claude Code's auto memory recognizes only one directory per session, so one of the two levels must be self-built. Decision: **native auto memory takes project memory**, with its directory redirected to `<项目目录>/.arcreel/memory/` (`<project dir>/.arcreel/memory/`); **user memory is self-built**, landing in `<数据根>/.arcreel/users/<user_id>/memory/` (`<data root>/.arcreel/users/<user_id>/memory/`); at session assembly the server injects its `MEMORY.md` index into the append prompt using the same truncation rules as native, and the agent writes with Write/Edit. Native's derivation rules, background extraction, and index-overflow warnings are all designed around "the current working directory", and project memory changes most often and benefits most; user memory is small and stable, and injecting an index is enough.

**Redirection goes through the flag settings layer of `ClaudeAgentOptions.settings`**, injecting the `autoMemoryDirectory` absolute path per session, without materializing any settings file and without switching `CLAUDE_CONFIG_DIR` or HOME. When `memory_paths.auto` in the session init message doesn't match expectations, an error is logged and the session opens anyway, without hard failure.

**Not placed under `.claude/`**: the project's `.claude/` is the materialized profile tree, and both a manifest mismatch and "restore built-in profile" delete the whole tree, so memory placed there would be silently wiped; `.arcreel/` is the existing ArcReel internal state directory, the validator skips dot directories, and archive export copies only visible items, so memory naturally stays out of archives and isn't reported as an unrecognized directory. `_`-prefixed directories under the data root denote cross-project shared material libraries, a different meaning, so `_users/` is not used.

## Explicitly not adopted

- **Native takes user memory, project memory self-built**: symmetrically feasible, but native background extraction leans toward the current work content, and landing it at the user level would mix project details into cross-project notes.
- **Both levels self-built with native disabled**: throws away native's write instructions and index warnings along with it; the biggest gap.
- **Materializing `.claude/settings.local.json` to carry `autoMemoryDirectory`**: the path varies per project, conflicting with the sha256 manifest's "built-in files unmodified" judgement, and whether SDK sessions adopt project-level values is unverified.
- **Switching `CLAUDE_CONFIG_DIR` per user**: drags along the transcript, `~/.claude.json`, and user-level settings, with an impact far beyond memory itself.
- **Migrating the existing `~/.claude/projects/<repo>/memory/` under the repository slug**: its content is mixed with developer notes and can't be attributed to projects; it's only a dev-machine phenomenon.

## Consequences

- `AgentAccessPolicy` lists both memory directories as agent read/write areas: the hook layer allows Read/Write/Edit/Glob/Grep. The kernel sandbox layer sets no write allowance for the user memory directory: built-in file tools don't go through the kernel sandbox and need no such configuration; `allowWrite` applies only to Bash, and its writable bind mount under bwrap would override the `denyRead` on `users/`. With that allowance removed, Bash reads and writes to the user memory directory are both rejected. Project memory is inside cwd, and Bash reads and writes it as usual. The user memory directory is purely derived by the policy from `projects_root` + `user_id`; `user_id` is passed per call alongside `project_cwd`, and the policy remains a process-level singleton.
- The subprocess HOME, `CLAUDE_CONFIG_DIR`, and the tool-results read exception for `claude_projects_dir` are all unchanged.
- Subagent `memory: "project"` lands in `<cwd>/.claude/agent-memory/`, which is likewise wiped by a profile reset; the choice of subagent memory scope must take this into account separately.
