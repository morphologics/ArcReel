# Claude Code auto memory under the Agent SDK: mechanism and configuration surface

> Status: research complete; conclusions feed the map [#2306](https://github.com/ArcReel/ArcReel/issues/2306); implementation tickets #2309 / #2310 build on it.
> Related: [#2307](https://github.com/ArcReel/ArcReel/issues/2307) (this ticket).
> Version baseline: Python `claude-agent-sdk` 0.2.139, bundled CLI 2.1.233 (`.venv/lib/python3.12/site-packages/claude_agent_sdk/_cli_version.py`). Version numbers cited for docs follow the Claude Code CHANGELOG; CLI source quotes come from `strings` output of the bundled binary `claude_agent_sdk/_bundled/claude` (a bun-packed Mach-O with minified identifiers; below they are marked `fn:<minified name>` so they can be re-checked).

## Background

ArcReel's embedded creative agent starts an SDK session in `server/agent_runtime/options_assembler.py` with `SystemPromptPreset(preset="claude_code")` + `setting_sources=["project"]`, with cwd `app_data_dir()/<project_name>`, which by default is `<repo>/projects/<project_name>` inside the repo (`lib/infra/app_data_dir.py`). This research answers whether auto memory takes effect under this configuration, where it lands, whether it can be redirected, and what subagent memory means.

## Conclusions at a glance

| Question | One-line conclusion |
|---|---|
| 1 Enabled by default? | Yes. SDK / `-p` mode does not disable it; it is controlled by `autoMemoryEnabled`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `--bare`/`CLAUDE_CODE_SIMPLE`, safe mode, `/pause-memory`; `exclude_dynamic_sections` only moves it, it does not turn it off. |
| 2 On-disk location | `<config dir>/projects/<slug(git repo root ?? cwd)>/memory/`; the index `MEMORY.md` is truncated at 200 lines or 25 000 bytes; topic files carry frontmatter; the instruction text is in §2.4. |
| 3 Redirection | Possible: `autoMemoryDirectory` (absolute path or `~/`; policy > `--settings` > local/project (when trusted or non-interactive) > user); `CLAUDE_CONFIG_DIR` moves everything (transcripts / settings included); `HOME` has no documented support; `CLAUDE_CODE_PROJECT_DIR_NAME` needs CLI ≥ 2.1.234 and is not supported on this machine. |
| 4 Subagent memory | `user` → `<config dir>/agent-memory/<name>/`; `project` → `<cwd>/.claude/agent-memory/<name>/`; `local` → `<cwd>/.claude/agent-memory-local/<name>/`; depends on the master switch, unaffected by `autoMemoryDirectory`. |
| 5 Main agent scope switch | None. The main agent has only one derivation rule (HOME/config dir + git root slug); the only "in-project" option is pointing `autoMemoryDirectory` at an absolute path. |

## 1. Whether it is enabled by default, and which switches control it

### 1.1 Official docs

- "Auto memory is on by default." —— [memory#enable-or-disable-auto-memory](https://code.claude.com/docs/en/memory#enable-or-disable-auto-memory)
- Settings key `autoMemoryEnabled` (Boolean, default `true`, allowed in any settings file). When `false`, "Claude doesn't read from or write to the auto memory directory". —— [settings-reference#automemoryenabled](https://code.claude.com/docs/en/settings-reference#automemoryenabled)
- Environment variable `CLAUDE_CODE_DISABLE_AUTO_MEMORY`: `1` disables; `0` forces it on, even with `--bare` or `autoMemoryEnabled: false`. "When disabled, Claude does not create or load auto memory files." —— [env-vars](https://code.claude.com/docs/en/env-vars)
- `--bare` / `CLAUDE_CODE_SIMPLE=1` (introduced in v2.1.81): CHANGELOG text "auto-memory fully disabled". —— [cli-reference](https://code.claude.com/docs/en/cli-reference), [CHANGELOG 2.1.81](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md)
- SDK mode **does not disable it**: auto memory is "Loaded into the system prompt at session start", listed in the "What settingSources does not control" table, "read regardless of its value"; to disable, use `autoMemoryEnabled: false` or `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in `env`. —— [agent-sdk/claude-code-features#what-settingsources-does-not-control](https://code.claude.com/docs/en/agent-sdk/claude-code-features#what-settingsources-does-not-control)
- Same for `-p`: "Without it [`--bare`], `claude -p` loads the same context an interactive session would, including anything configured in the working directory or `~/.claude`." —— [headless](https://code.claude.com/docs/en/headless)
- Under the SDK the agent writes memories with the ordinary `Write`/`Edit` tools; there is no dedicated memory tool, "so those tools must be enabled for the agent to save memories". —— same claude-code-features page
- `exclude_dynamic_sections` **cannot exclude** memory; it only moves it into the first user message: "the working directory, the git-repo flag, the platform, the active shell, the OS version, and auto memory paths still reach Claude, but as part of the first user message rather than the system prompt."; it applies only to the `claude_code` preset. Added in Python SDK 0.1.57 (SDK CHANGELOG #797); the CLI counterpart `--exclude-dynamic-system-prompt-sections` was added to print mode in v2.1.98. —— [agent-sdk/modifying-system-prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts#improve-prompt-caching-across-users-and-machines)
- Multi-tenant warning: "Do not rely on default `query()` options for multi-tenant isolation... set `settingSources: []` plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in `env`." —— claude-code-features, [agent-sdk/hosting](https://code.claude.com/docs/en/agent-sdk/hosting)

### 1.2 CLI 2.1.233 source (bundled binary)

Master switch `fn:Ig()`, short-circuiting in order:

```js
function Ig(){
  if(P3())return!1;                       // in-session /pause-memory flag
  if(dd())return!1;                       // CLAUDE_CODE_SAFE_MODE / --safe-mode
  let e=process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY;
  if(Ln(e))return!1;                      // "1"/"true"/"yes"/"on" → off
  if(af(e))return!0;                      // "0"/"false"/"no"/"off" → forced on
  if(V.CLAUDE_CODE_SIMPLE)return!1;       // --bare
  if(V.CLAUDE_CODE_REMOTE&&!process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR&&!V.CLAUDE_COWORK_MEMORY_PATH_OVERRIDE)return!1;
  if(Kvn())return!1;                      // GrowthBook remote rollout kill switch per model/org
  let t=Wo();                             // merged settings
  if(t.autoMemoryEnabled!==void 0)return t.autoMemoryEnabled;
  return!0}
```

Settings schema description verbatim: `autoMemoryEnabled` "Enable auto-memory for this project. When false, Claude will not read from or write to the auto-memory directory."

System prompt assembly `fn:lW`: under the `claude_code` preset, the memory section enters the system prompt as `xF("memory", twn(...))`; when `excludeDynamicSections` is true, the system prompt instead contains the static version `fn:Qad` ("The directory path is provided in your session context."), and the directory path is produced by `fn:Cvi → eld` as a `# auto memory\nMemory directory: \`<dir>\`` section injected into the first user message. When a custom string `system_prompt` is passed, `lW` is not called at all, and the memory section is appended only if the internal environment variable `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` is present (`fn:twn` call site `m=o!==void 0&&Jvn()?await twn(r):null`).

Print / SDK mode has no separate disable branch: `fn:eBe()` (`if(xn())return!0`, where `xn()` means "non-interactive") actually makes non-interactive mode treat the workspace as already trusted.

### 1.3 Local verification

A minimal probe (`uv run python`, `query()` + `SystemPromptPreset(preset="claude_code")` + `setting_sources=["project"]`, `max_turns=1`) was run once under each of two cwds, with these results:

| cwd | init message `memory_paths.auto` | Directories created after startup |
|---|---|---|
| Non-git directory `.../sdkcheck/nogit` | `~/.claude/projects/-…-sdkcheck-nogit/memory/` | Directory of the same name + empty `memory/` + transcript `.jsonl` |
| Subdirectory of a git repo `.../sdkcheck/gitrepo/sub` | `~/.claude/projects/-…-sdkcheck-gitrepo/memory/` (repo root slug) | `-…-gitrepo/memory/` (empty) and `-…-gitrepo-sub/<id>.jsonl` (transcript by cwd) |

Conclusion: auto memory is **enabled** in SDK mode; the init system message contains `memory_paths` (CLI 2.1.233 schema: `memory_paths:{auto?:string,team?:string}`, marked `@internal`), and the empty `memory/` directory is created at startup (`fn:jIe` ensureMemoryDirExists).

**Why there is no `-Users-<user>-MyProjects-ArcReel-projects-*/memory` on this machine**: the memory directory is derived from `Lu(cwd) ?? cwd`, where `Lu` resolves the git repo root (`fn:Lu → Zc → rootByPath`). The ArcReel project cwd `<repo>/projects/<name>` is inside the ArcReel repo (`projects/` is just gitignored, not a separate git repo), so the SDK session's memory lands in `~/.claude/projects/-Users-<user>-MyProjects-ArcReel/memory/`, **the same directory** as the memory of a developer running interactive Claude Code in the repo. This is neither disabled nor untriggered; the derivation rule since v2.1.63, "auto memory shared across git worktrees of the same repository", merges it into the repo root. There is likewise no `-…-ArcReel-projects-<name>/` transcript directory on this machine, which means no SDK session was started from that data dir within the 30-day retention period (or the data dir points elsewhere); this is unrelated to the missing memory directory.

Risk: the creative agent and developer sessions share the same `MEMORY.md` and contaminate each other (the creative agent reads developer feedback such as "don't mention the white-label fork in PRs", and developer sessions read project memories saved by the creative agent).

## 2. On-disk rules

### 2.1 Directory derivation (`fn:X4s.resolve`)

```js
resolve=mu(()=>{
  let e=pnd()??f1_();            // CLAUDE_COWORK_MEMORY_PATH_OVERRIDE ?? autoMemoryDirectory
  if(e)return e;
  let t=k8.join(Rwe(),"projects"), // Rwe = CLAUDE_CODE_REMOTE_MEMORY_DIR ?? config dir En()
      r=Lu(Va())??Va();           // git repo root ?? projectRoot(cwd)
  return(k8.join(t,WT(r),d1_)+k8.sep).normalize("NFC")},  // d1_="memory"
  ()=>`${Va()}|${eBe()}`)
```

- Config dir `fn:En` = `CLAUDE_CONFIG_DIR ?? ~/.claude`.
- Slug `fn:WT`: `fEo(e)=e.replace(/[^a-zA-Z0-9]/g,"-")`; beyond `Yre=200` characters it is truncated to 200 and a path hash is appended. The docs say the same: "`<project>` is your working directory path with non-alphanumeric characters replaced by `-`… truncates the name to 200 characters and appends a hash" —— [sessions#where-transcripts-are-stored](https://code.claude.com/docs/en/sessions#where-transcripts-are-stored)
- Docs: "The `<project>` path is derived from the git repository, so all worktrees and subdirectories within the same repo share one auto memory directory. Outside a git repo, the project root is used instead." —— [memory#storage-location](https://code.claude.com/docs/en/memory#storage-location); v2.1.63 CHANGELOG.
- The transcript directory `fn:bN(cwd)` does not use the git root; it still uses the cwd slug (confirmed by the local verification table).

### 2.2 Index and truncation

- Constants: `M_="MEMORY.md"`, `iQ=200` (lines), `Mce=25000` (bytes). `fn:pRr` first truncates to 200 lines, then truncates to 25 000 bytes at the last newline, and produces the warning "`MEMORY.md` is N lines (limit: 200). Only part of it was loaded. Keep index entries to one line under ~200 chars; move detail into topic files."
- Docs: "The first 200 lines of `MEMORY.md`, or the first 25KB, whichever comes first, are loaded at the start of every conversation." 25KB limit v2.1.83; near-limit reminder v2.1.186; error after an over-limit write v2.1.210; count only loaded content v2.1.211. —— [memory#how-it-works](https://code.claude.com/docs/en/memory#how-it-works), CHANGELOG
- Over-limit error text (sent to the model): "Error: this write left the memory index at MEMORY.md at 214 lines, over its 200-line read limit. The write succeeded, but everything past the limit is silently dropped each time the index is loaded — entries at the end are already invisible to readers. Rewrite it to under 140 lines now…" —— [errors#memory-index-is-over-its-read-limit](https://code.claude.com/docs/en/errors#memory-index-is-over-its-read-limit)
- Topic files are not loaded at startup; they are read on demand with file tools. The frontmatter `type` has four kinds, `user | feedback | project | reference`; the `modified` ISO timestamp is written by the CLI (v2.1.214).
- MEMORY.md content is injected through the same attachment channel as CLAUDE.md (`fn:Wst("AutoMem")` returns `Q2e()`, i.e. the `MEMORY.md` path; label text " (user's auto-memory, persists across conversations)", alongside " (project instructions, checked into the codebase)").
- Retention: the memory directory is excluded from `cleanupPeriodDays` cleanup; v2.1.228 fixed a bug that wrongly deleted the directory's contents. —— [claude-directory#cleaned-up-automatically](https://code.claude.com/docs/en/claude-directory#cleaned-up-automatically)

### 2.3 Background automatic extraction

Besides the model calling `Write` itself, there is a background "extractMemories" (v2.1.59 "Claude automatically saves useful context to auto-memory"): `fn:IAa` is triggered by `executeExtractMemories` after the main loop ends, under the condition `!i.agentId && Yvn()`, where `Yvn()` = GrowthBook flag `tengu_passport_quail` and (interactive or `tengu_slate_thimble`). In other words, whether background extraction runs in non-interactive/SDK mode is decided by a server-side rollout and cannot be configured locally; it is skipped when the session has already written memory files directly (`tengu_extract_memories_skipped_direct_write`).

### 2.4 Write instructions the agent receives

The official docs state explicitly that the system prompt is not published ([settings](https://code.claude.com/docs/en/settings): "Claude Code's system prompt isn't published"). Below are the templates from the CLI 2.1.233 binary; GrowthBook `tengu_stone_shell` chooses between the two variants:

Base variant `fn:$ad` (default):

````
# Memory
You have a persistent file-based memory at `<dir>`. This directory already exists — write to it directly with the Write tool (do not run mkdir or check for its existence). Each memory is one file holding one fact, with frontmatter:
```markdown
name: <short-kebab-case-slug>
description: <one-line summary, used to decide relevance during recall>
metadata:
  type: user | feedback | project | reference
<the fact; for feedback/project, follow with **Why:** and **How to apply:** lines. Link related memories with [[their-name]].>
```
…
After writing the file, add a one-line pointer in `MEMORY.md` (`- [Title](file.md) — hook`). `MEMORY.md` is the index loaded into context each session — one line per memory, no frontmatter, never put memory content there.
````

stone_shell variant `fn:Vad` / template `m2_` (excerpt):

```
# auto memory
You have a persistent, file-based memory at `{memory_dir}`.
The files there are lessons you saved from prior sessions, what you save there in this session is all that persists after the session is completed or if the user stops responding. …
A good memory is applicable, durable, and legible: …
You must NOT save a memory unless you have validated that it is applicable, durable, AND legible.
Check each reply before you send it — … did the user's latest message teach you a durable, applicable lesson? … If you've decided to write to your memory, you MUST make your memory write before treating your turn as finished …
```

With `exclude_dynamic_sections=True` the system prompt contains the static version `fn:Azs`: "You have a persistent, file-based memory system. The directory path is provided in your session context." The directory is given in the first user message.

## 3. Redirecting the memory directory

| Option | Semantics | Side effects | Source |
|---|---|---|---|
| `autoMemoryDirectory` (settings, v2.1.74) | Absolute path or `~/` prefix; relative paths, drive roots and anything containing NUL are rejected by `fn:und/dnd` and fall back to the default. Precedence (`fn:f1_`): `policySettings` > `flagSettings` (`--settings`) > (when `eBe()` is true, `localSettings` > `projectSettings`) > `userSettings`. `eBe()` is always true in non-interactive mode, so SDK sessions read this key from the project's `.claude/settings.json`. It is a fixed directory and is no longer derived per project. | Affects only memory; transcript / resume / settings locations are unchanged. The schema description says "Ignored if set in projectSettings (checked-in .claude/settings.json) for security", which is inconsistent with the trust gating in the code; the docs say it is subject to workspace trust. | [settings-reference#automemorydirectory](https://code.claude.com/docs/en/settings-reference#automemorydirectory), CHANGELOG 2.1.74, `fn:f1_` |
| `CLAUDE_CONFIG_DIR` | "Override the configuration directory (default: `~/.claude`). All settings, session history, and plugins are stored under this path." | Also moves `settings.json`, user-level `CLAUDE.md`/`rules/`, `projects/<slug>/*.jsonl` (transcripts, affecting `resume`/`continue` lookup), `projects/<slug>/memory/`, `agent-memory/`, plugins, `~/.claude.json` (`fn:Jiy`). Can only be set from the process environment, user or managed settings; since v2.1.251 project-level `env` no longer applies. In the SDK use `env={"CLAUDE_CONFIG_DIR": ...}`. User-level settings disappear with it as well; `user` in `setting_sources` reads the new directory. | [env-vars](https://code.claude.com/docs/en/env-vars), [claude-directory](https://code.claude.com/docs/en/claude-directory), hosting |
| `HOME` | No mention at all in the official docs; in code the default directory is taken from `os.homedir()` (`fn:En`), so changing `HOME` is equivalent to changing the whole config dir, but it also affects the shell/toolchain. | Not recommended. | — |
| `CLAUDE_CODE_PROJECT_DIR_NAME` (v2.1.234) | When set together with `CLAUDE_CONFIG_DIR`, `<config dir>/projects/<name>/` stores both transcripts and memory. | **The bundled CLI 2.1.233 on this machine does not contain this string; not supported**; requires SDK ≥ 0.2.140. | [sessions#name-the-project-directory-yourself](https://code.claude.com/docs/en/sessions#name-the-project-directory-yourself) |
| `CLAUDE_CODE_REMOTE_MEMORY_DIR` / `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` | Internal variables: the former replaces the `Rwe()` root (memory and user-level agent-memory move together); the latter sets the memory directory directly without `~` expansion, and also makes a custom system prompt get the memory section appended. | Undocumented, may change between versions; not something to rely on. | `fn:Rwe`, `fn:pnd`, `fn:Ig` |
| Other SDK options | `ClaudeAgentOptions` has no memory directory field; `setting_sources` does not affect memory loading; `cwd` determines the slug. `SessionStore` mirrors only transcripts, not memory. | — | [agent-sdk/python](https://code.claude.com/docs/en/agent-sdk/python), hosting |

Feasible path for ArcReel: the server already projects the project `.claude/` (CLAUDE.md mode variant) into the cwd; it can also project a `.claude/settings.json` that writes a per-project absolute `autoMemoryDirectory` (e.g. `<app_data_dir>/<project_name>/.claude/memory/`), with no need to change `CLAUDE_CONFIG_DIR`, leaving transcript and session_store behaviour unchanged.

## 4. Subagent `AgentDefinition.memory`

- Allowed values `user | project | local`; Python `AgentDefinition.memory: Literal["user","project","local"] | None = None` (`types.py:98`). Added in SDK 0.1.49; CLI frontmatter support since v2.1.33. —— [agent-sdk/python#agentdefinition](https://code.claude.com/docs/en/agent-sdk/python#agentdefinition), SDK CHANGELOG, CC CHANGELOG 2.1.33
- Documented paths: `user` → `~/.claude/agent-memory/<name-of-agent>/`; `project` → `.claude/agent-memory/<name-of-agent>/`; `local` → `.claude/agent-memory-local/<name-of-agent>/`. `project` is recommended. When enabled, Read/Write/Edit are opened automatically, and the first 200 lines / 25KB of that directory's `MEMORY.md` are loaded. —— [sub-agents#enable-persistent-memory](https://code.claude.com/docs/en/sub-agents#enable-persistent-memory)
- Code `fn:mRr(name, scope)`: the name is first sanitized by `Rzs` (`/[^a-zA-Z0-9\-_]/g → "-"`, `unknown` if empty); `project` → `join(Yt(), ".claude", "agent-memory", name)`, `local` → `join(Yt(), ".claude", "agent-memory-local", name)` (with `CLAUDE_CODE_REMOTE_MEMORY_DIR` set it moves to `<dir>/projects/<slug(git root)>/agent-memory-local/`), `user` → `join(Rwe(), "agent-memory", name)`. `Yt()` is the **current cwd**, with no git-root merging; `mRr` does not read `autoMemoryDirectory`, so redirecting the main memory does not affect subagent directories.
- Depends on the master switch: "if you turn auto memory off, with the `autoMemoryEnabled` setting or `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, the `memory` field has no effect" —— same sub-agents page
- The main session's auto memory does not enter non-fork subagents: "The main conversation's auto memory isn't loaded into subagents; the exception is a fork". —— [memory#how-it-works](https://code.claude.com/docs/en/memory#how-it-works)

## 5. Whether the main agent has a project/user scope switch

No. The main agent's directory has only the single resolution chain in §2.1: internal override → `autoMemoryDirectory` → `<config dir>/projects/<slug(git root??cwd)>/memory/`. The docs mark `projects/<project>/memory/` as "Global only", while `agent-memory/<name>/` is marked "Project and global" ([claude-directory](https://code.claude.com/docs/en/claude-directory)); the Scope column of the table on the memory page says "Per repository, shared across worktrees", and stresses "Auto memory is machine-local" ([memory#claude-md-vs-auto-memory](https://code.claude.com/docs/en/memory#claude-md-vs-auto-memory)). To get a memory location "inside the project directory", the only options are pointing `autoMemoryDirectory` at that project's absolute path (it can go in the project's `.claude/settings.json`, which is read in SDK non-interactive mode), or switching `CLAUDE_CONFIG_DIR` wholesale.

## Relevant CHANGELOG entries

| Version | Entry |
|---|---|
| 2.1.32 | Claude now automatically records and recalls memories as it works (first introduced) |
| 2.1.33 | `memory` frontmatter field for agents (`user`/`project`/`local`) |
| 2.1.50 | `CLAUDE_CODE_SIMPLE` fully strips session memory, etc. |
| 2.1.59 | Claude automatically saves useful context to auto-memory. Manage with /memory |
| 2.1.63 | Project configs & auto memory shared across git worktrees of the same repository |
| 2.1.74 | Added `autoMemoryDirectory` setting |
| 2.1.75 | last-modified timestamps to memory files |
| 2.1.77 | Fixed `--resume` truncation race with memory-extraction writes |
| 2.1.81 | `--bare` flag, auto-memory fully disabled |
| 2.1.83 | `MEMORY.md` index truncates at 25KB as well as 200 lines |
| 2.1.98 | `--exclude-dynamic-system-prompt-sections` in print mode |
| 2.1.186 | Reminder to compact `MEMORY.md` near the size limit |
| 2.1.210 / 2.1.211 | Over-limit write error; count only loaded content |
| 2.1.214 | ISO `modified` timestamp in memory frontmatter |
| 2.1.228 | Fixed session cleanup deleting contents inside memory folder |
| 2.1.234 | `CLAUDE_CODE_PROJECT_DIR_NAME` |
| 2.1.251 | Project-level `env` no longer sets `CLAUDE_CONFIG_DIR` |

The CHANGELOG has no entries introducing `autoMemoryEnabled` or `CLAUDE_CODE_DISABLE_AUTO_MEMORY`. Python SDK: 0.1.49 `AgentDefinition.memory`; 0.1.57 `exclude_dynamic_sections`; 0.1.60 fixed `setting_sources=[]` being treated as falsy; 0.2.139 bundles CLI 2.1.233; 0.2.140 bundles 2.1.235.

## Not verified / documentation gaps

- Whether `MEMORY.md` is still injected into the first user message via the CLAUDE.md attachment channel under a custom string `system_prompt`: the source shows the injection point is gated only by `Ig()` and the CLAUDE.md loading switch; not tested in practice.
- When `autoMemoryDirectory` is written in `projectSettings`, the schema description says "ignored", while the code `fn:f1_` reads it when `eBe()` is true; whether SDK sessions actually adopt the project-level value has not been tested (recommend asserting on the init message `memory_paths.auto` when implementing #2309).
- Whether background extractMemories actually runs in SDK mode depends on a server-side GrowthBook flag and cannot be determined locally.
- Overriding `HOME` has no official documentation; not tested.

## Source list

Official docs (all accessible): memory, settings, settings-reference, env-vars, sub-agents, sessions, claude-directory, cli-reference, headless, errors, agent-sdk/{modifying-system-prompts, sessions, subagents, python, typescript, hosting, claude-code-features, permissions, secure-deployment}, prefix `https://code.claude.com/docs/en/`.
CHANGELOG: `https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md`; `https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/CHANGELOG.md`.
Local: `.venv/lib/python3.12/site-packages/claude_agent_sdk/{types.py,_cli_version.py,_internal/transport/subprocess_cli.py,_bundled/claude}`; `server/agent_runtime/options_assembler.py`; `lib/infra/app_data_dir.py`; the probe script is described in this ticket's issue comments (scratchpad, not committed).
