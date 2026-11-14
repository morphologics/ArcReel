# Agent memory: official definitions, content boundaries and write best practices

> Purpose: provide first-hand grounding for terminology, content boundaries, write wording and user-interaction precedents for the "Agent memory" spec of ArcReel's embedded agent (map https://github.com/ArcReel/ArcReel/issues/2306 ).
> Corresponding issue: https://github.com/ArcReel/ArcReel/issues/2308
> Scope: only first-hand Anthropic sources are accepted — Claude Code docs (code.claude.com), Claude platform docs (platform.claude.com), Agent SDK docs, the Anthropic engineering blog and research articles, the Claude Help Center (support.claude.com), the official Claude blog (claude.com/blog; the old anthropic.com/news now 308-redirects here), the Claude Code CHANGELOG and npm publish times. Third-party transcriptions of system prompts and community articles outside the Cookbook are not accepted.
> Research date: 2026-09-03. Most doc pages show no date themselves; the recency anchor is the highest version number mentioned on the page or a CHANGELOG entry (version dates taken from the `time` field of the npm registry `@anthropic-ai/claude-code`). Material older than 2026 is dated in the text.
> Conclusion marking: anything the official sources do not state explicitly is marked "unverified", and is not replaced by inference.

---

## 1. Conclusions at a glance

| Question in the ticket | One-sentence conclusion |
| --- | --- |
| 1 Definition and classification | Anthropic splits cross-session knowledge into two complementary mechanisms: "instructions written by people (CLAUDE.md)" and "notes Claude writes itself (auto memory)"; the memory tool is the same kind of file-based memory at the platform layer, persisted by the client; the transcript is just session history, and the official docs state clearly that its role and retention policy differ from memory's. |
| 2 What to remember and what not | Auto memory records only four kinds (user / feedback / project / reference), skipping anything derivable from the code and anything CLAUDE.md already says; the structure is a `MEMORY.md` index (one line per entry; only the first 200 lines or 25KB are loaded at startup) + topic files read on demand. |
| 3 Write triggers and wording | The auto memory system prompt for the Claude Code main session is not published; there are three public pieces of wording to align with: the "MEMORY PROTOCOL / ASSUME INTERRUPTION" the API automatically injects for the memory tool, the reinforcing sentences given in the docs ("keep it coherent and organized / Only write down information relevant to <topic>"), and the index-compaction error text returned to the model when over the limit. |
| 4 Precedents for user-visible, editable memory | Claude Code: memory is plain Markdown that users can edit or delete at any time; `/memory` provides the entry point and switch; the docs say explicitly "you can edit or delete it, but Claude will keep updating it". Claude.ai: since 2026-08-25 memory is a list of individual Topics that can be read, edited and deleted on the settings page, and in chat you can say "remember / change / forget", effective from the next conversation. Neither side promises that "Claude will not overwrite user edits". |

---

## 2. Question 1: official definitions and classification

### 2.1 Claude Code: two complementary mechanisms

The memory page of the Claude Code docs defines the two carriers of cross-session knowledge right at the top (https://code.claude.com/docs/en/memory , page mentions v2.1.239, accessed 2026-09-03):

> Each Claude Code session begins with a fresh context window. Two mechanisms carry knowledge across sessions: **CLAUDE.md files**: instructions you write to give Claude persistent context · **Auto memory**: notes Claude writes itself based on your corrections and preferences

> Claude Code has two complementary memory systems. Both are loaded at the start of every conversation. Claude treats them as context, not enforced configuration.

Comparison table verbatim (same page):

| Dimension | CLAUDE.md | Auto memory |
| --- | --- | --- |
| Who writes it | You | Claude |
| What it contains | Instructions and rules | Learnings and patterns |
| Scope | Project, user, or org | Per repository, shared across worktrees |
| Loaded into | Every session | Every session (first 200 lines or 25KB) |
| Use for | Coding standards, workflows, project architecture | Your preferences, corrections you give Claude, project context Claude can't derive from the code |

**The four CLAUDE.md scopes** (same page, in load order from broadest to narrowest): Managed policy (org level, deployed by IT) → User (`~/.claude/CLAUDE.md`, personal across projects) → Project (`./CLAUDE.md` or `./.claude/CLAUDE.md`, shared with the team via source control) → Local (`./CLAUDE.local.md`, personal for this project, added to `.gitignore`). All files are concatenated into the context rather than overriding each other; the injected form is "a user message after the system prompt" ("CLAUDE.md content is delivered as a user message after the system prompt, not as part of the system prompt itself.").

**Location and structure of auto memory** (same page):

> Each project gets its own memory directory at `~/.claude/projects/<project>/memory/`. The `<project>` path is derived from the git repository, so all worktrees and subdirectories within the same repo share one auto memory directory. Outside a git repo, the project root is used instead.

> The directory contains a `MEMORY.md` index and one topic file per memory

> `MEMORY.md` acts as an index of the memory directory. Claude reads and writes files in this directory throughout your session, using `MEMORY.md` to keep track of what's stored where.

> Auto memory is machine-local. … Files are not shared across machines or cloud environments.

The location can be changed: the `autoMemoryDirectory` setting "is read from any settings scope: user, project, local, policy, or `--settings`", and the value must be an absolute path or start with `~/` (CHANGELOG 2.1.74, npm 2026-03-11); `CLAUDE_CODE_PROJECT_DIR_NAME` together with `CLAUDE_CONFIG_DIR` can set the `<project>` directory name (v2.1.234+). Switches: `autoMemoryEnabled` (any settings scope) or the environment variable `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; starting with `--bare` also turns it off entirely (CHANGELOG 2.1.81, npm 2026-03-20).

**Under the Agent SDK** (https://code.claude.com/docs/en/agent-sdk/claude-code-features , table "What settingSources does not control"):

> Auto memory at `~/.claude/projects/<project>/memory/` — Loaded into the system prompt at session start. The agent writes new memories there with the standard `Write` and `Edit` tools rather than a dedicated memory tool, so those tools must be enabled for the agent to save memories — To disable: Set `autoMemoryEnabled: false` in settings, or `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in `env`

> Do not rely on default `query()` options for multi-tenant isolation. Because the inputs above are read regardless of `settingSources`, an SDK process can pick up host-level configuration and per-directory memory. For multi-tenant deployments, run each tenant in its own filesystem and set `settingSources: []` plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in `env`.

That is, under the SDK auto memory is enabled by default, is not controlled by `setting_sources`, and writes depend on the `Write` / `Edit` tools. The SDK overview also lists it as a capability ("Skills, commands, and memory — Load automatically from your project's `.claude/` and from `~/.claude/`, same as Claude Code", https://code.claude.com/docs/en/agent-sdk/overview ).

**Subagent memory** is part of auto memory: `AgentDefinition.memory: 'user' | 'project' | 'local'`, stored in `~/.claude/agent-memory/<name>/`, `.claude/agent-memory/<name>/`, `.claude/agent-memory-local/<name>/`; "if you turn auto memory off … the `memory` field has no effect" (https://code.claude.com/docs/en/sub-agents ). The main session's auto memory is not injected into subagents ("The main conversation's auto memory isn't loaded into subagents; the exception is a fork", memory page).

**After compaction**: auto memory, like the project-root CLAUDE.md, is "Re-injected from disk" (https://code.claude.com/docs/en/context-window#what-survives-compaction ).

### 2.2 Platform layer: the memory tool

Definition (https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool , page undated; the example model is `claude-opus-5`, so it is the current 2026 version):

> The memory tool lets Claude store and retrieve information across conversations in a directory of memory files. Claude can create, read, update, and delete files that persist between sessions, building up knowledge over time without keeping everything in the context window.

> Memory supports just-in-time context retrieval. Rather than loading all relevant information up front, an agent records what it learns in memory files and reads them back on demand.

> The memory tool operates client-side: Claude requests file operations, and your application executes them. You control where and how the data is stored through your own infrastructure.

Key points: tool definition `{"type": "memory_20250818", "name": "memory"}`; commands `view / create / str_replace / insert / delete / rename`; conventional root directory `/memories` ("The `/memories` path is a prefix that your handler maps onto real storage"); the current docs state the memory tool itself no longer needs a beta header ("the memory tool itself doesn't require a beta header"), and when combined with context editing use `anthropic-beta: context-management-2025-06-27`; supported on "all Claude 4 and later models". It first shipped as a public beta with Sonnet 4.5 on 2025-09-29 (https://claude.com/blog/context-management ).

Relationship to Claude Code auto memory: both are "file-based, read-on-demand" memory; the difference is who executes the file operations — auto memory is written by Claude Code to a local directory with the standard `Write` / `Edit`; with the memory tool your application executes the operations and decides the storage. The Cookbook overview of 2026-03-20: "Claude Code employs multiple of these strategies in production: compaction for long conversations and two complementary memory systems for cross-session persistence. Our API offers first-party implementations of all three: server-side compaction, context editing (which includes tool-result clearing), and the memory tool." (https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools )

### 2.3 The boundary between session transcripts and memory

Agent SDK sessions page (https://code.claude.com/docs/en/agent-sdk/sessions ):

> A session is the conversation history the SDK accumulates while your agent works. It contains your prompt, every tool call the agent made, every tool result, and every response. The SDK writes it to disk automatically so you can return to it later.

> Sessions persist the **conversation**, not the filesystem.

The hosting page lists three kinds of local state separately: Session transcripts (`~/.claude/projects/`), `CLAUDE.md` memory files, Working-directory artifacts, and says explicitly "**Transcripts only**: `SessionStore` mirrors transcripts, not `CLAUDE.md` memory files or other working-directory artifacts." (https://code.claude.com/docs/en/agent-sdk/hosting ). The retention policies also differ: "Claude Code deletes old session transcripts after the `cleanupPeriodDays` retention period, but excludes the files in the memory directory from that retention sweep. `MEMORY.md` and topic files stay until you or Claude edits or deletes them." (memory page; before v2.1.228 a bug could delete them by mistake, https://code.claude.com/docs/en/claude-directory ).

### 2.4 The engineering blog's classification: memory is one of three long-horizon strategies

"Effective context engineering for AI agents" (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents , 2025-09-29):

> To enable agents to work effectively across extended time horizons, we've developed a few techniques that address these context pollution constraints directly: compaction, structured note-taking, and multi-agent architectures.

> Structured note-taking, or agentic memory, is a technique where the agent regularly writes notes persisted to memory outside of the context window. These notes get pulled back into the context window at later times.

> Compaction maintains conversational flow for tasks requiring extensive back-and-forth; Note-taking excels for iterative development with clear milestones; Multi-agent architectures handle complex research and analysis where parallel exploration pays dividends.

"Building effective agents" (2024-12-19) only lists memory as one of the LLM's augmentations ("an LLM enhanced with augmentations such as retrieval, tools, and memory"), with no dedicated definition. No new 2026 engineering article on anthropic.com specifically about memory was found (two domain-restricted searches found nothing); a related one is "Long-running Claude for scientific computing" (https://www.anthropic.com/research/long-running-Claude , 2026-03-23), which calls the progress file "the agent's portable long-term memory, acting as a sort of lab notes".

### 2.5 Claude.ai: product memory

Help Center (https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context , Intercom relative time "Updated today"):

> Claude saves memory as a set of individual topics as you chat, rather than summarizing conversations after they end.

> Each project has its own separate memory space and dedicated project summary, so the context within each of your projects is focused, relevant, and separate from other projects or non-project chats.

It and "searching past chats" are two independent switches ("Generate memory from chats" and "Search and reference chats"); the latter is a RAG tool call. The old version (2025-09 to 2026-07) synthesized a "memory summary" every 24 hours; since 2026-07-10 it has been individual Topics (https://support.claude.com/en/articles/12138966-release-notes ). Claude Code memory and Claude.ai memory are two independent systems; the Help Center lists them separately and does not mention any link between them (https://support.claude.com/en/articles/14554000-claude-code-power-user-tips : "This is separate from your user-level ~/.claude/CLAUDE.md and project-level ./CLAUDE.md files, which you maintain by hand.").

---

## 3. Question 2: what to remember and what not; progressive disclosure and capacity

### 3.1 Auto memory records only four kinds

Memory page verbatim:

> As it works, Claude saves four kinds of notes for itself. Claude records the kind as a `type` field in the memory file's frontmatter:
> · `user`: your role, expertise, and working preferences
> · `feedback`: corrections you give Claude and approaches you confirm
> · `project`: ongoing work, deadlines, and decisions that Claude can't derive from the code or git history
> · `reference`: where to find information outside the project, such as an issue tracker or dashboard

> Claude skips anything it can derive from the codebase, such as architecture, file paths, or debugging fixes. It also skips anything your CLAUDE.md files already say.

> Claude doesn't save something every session. It decides what's worth remembering based on whether the information would be useful in a future conversation.

Mapping of the ticket's four-way split "preferences vs facts vs progress vs one-off instructions":

| Ticket category | Official position |
| --- | --- |
| Preferences | Remember: `user` (role, expertise, working preferences) and `feedback` (corrections and confirmed approaches) |
| Facts | Remember only what the agent cannot derive: `project` (decisions, deadlines) and `reference` (external pointers); anything derivable from code / git, or already in CLAUDE.md, is always skipped |
| Progress | The `project` kind allows recording "ongoing work", but the official examples are deadlines and decisions; Claude Code does not single out production-progress content. ArcReel has decided that progress is carried by the server-side step view and is not recorded in memory |
| One-off instructions | No such wording officially; the criterion is "useful in a future conversation", which requirements valid only for the current operation do not meet |

A user's spoken request to "remember X" goes to auto memory; writing into CLAUDE.md must be asked for explicitly:

> When you ask Claude to remember something, like "always use pnpm, not npm" or "remember that the API tests require a local Redis instance," Claude saves it to auto memory. To add instructions to CLAUDE.md instead, ask Claude directly, like "add this to CLAUDE.md," or edit the file yourself via `/memory`.

Note that the claude-directory page has an older position ("Claude saves notes as it works: build commands, debugging insights, architecture notes."), which conflicts with the memory page's "skips architecture / debugging fixes"; both are official text, and this note follows the memory page while recording the discrepancy as is.

### 3.2 What CLAUDE.md (instructions) should contain

Memory page: "Keep it to facts Claude should hold in every session: build commands, conventions, project layout, "always do X" rules. If an entry is a multi-step procedure or only matters for one part of the codebase, move it to a skill or a path-scoped rule instead."; when to add: "Claude makes the same mistake a second time / A code review catches something Claude should have known about this codebase / You type the same correction or clarification into chat that you typed last session / A new teammate would need the same context to be productive".

Best-practices page (https://code.claude.com/docs/en/best-practices ; the old anthropic.com/engineering/claude-code-best-practices now 308-redirects): "Keep it concise. For each line, ask: *"Would removing this cause Claude to make mistakes?"* If not, cut it. Bloated CLAUDE.md files cause Claude to ignore your actual instructions!"; the Exclude column includes "Anything Claude can figure out by reading code / … / Information that changes frequently / Long explanations or tutorials". The `#` shortcut was removed in CHANGELOG 2.0.70 ("Removed # shortcut for quick memory entry (tell Claude to edit your CLAUDE.md instead)").

### 3.3 Recommended structure for progressive disclosure and capacity constraints

Structure: index + topic files; one line per index entry; topic files read on demand.

> The first 200 lines of `MEMORY.md`, or the first 25KB, whichever comes first, are loaded at the start of every conversation. Content beyond that threshold is not loaded at session start. Claude keeps `MEMORY.md` concise by moving detailed notes into separate topic files.

> This limit applies only to `MEMORY.md`. … Claude Code doesn't load topic files such as `user_role.md` or `feedback_testing.md` at startup. Claude reads them on demand using its standard file tools when it needs the information.

Topic files carry frontmatter (`name` / `description` / `type`; Claude Code v2.1.214+ automatically adds an ISO `modified` timestamp, "Claude Code never adds frontmatter to a file that has none"). Example from the claude-directory page:

```
---
name: Debugging patterns
description: Auth token rotation and database connection troubleshooting for this project
type: reference
---
```

How the capacity constraint is enforced (memory page; CHANGELOG 2.1.83 added 25KB, 2.1.186 added the near-limit reminder, 2.1.210 added the over-limit error, 2.1.211 counts only actually loaded content):

> After Claude writes to `MEMORY.md`, Claude Code measures the file against the 200-line and 25KB read limits. If the file is near a limit, Claude Code reminds Claude to shorten it: keep one line per entry, move detail into topic files, and merge or drop stale entries. If the file is over a limit, the write still succeeds, but Claude Code returns an error telling Claude to rewrite the index, because everything past the limit is dropped on the next load.

Size guidance on the CLAUDE.md side: per file "target under 200 lines"; over 4 MiB the whole file is skipped; `/doctor` (v2.1.206+) suggests trimming content "that can be derived from the code".

Corresponding guidance on the memory tool side: `view` truncates files over 16,000 characters, "Consider capping how many characters the `view` command returns, and let Claude page through the rest with `view_range`."; "Track memory file sizes and cap how large a file can grow." / "Periodically delete memory files that haven't been accessed in a long time." (memory-tool page). The Cookbook (https://platform.claude.com/cookbook/tool-use-memory-cookbook ; the page is dated 2025-05-22 but its title mentions Sonnet 4.6, so the date is questionable) checklist: "Store task-relevant patterns, not conversation history / Organize with clear directory structure / Use descriptive file names / Periodically review and clean up memory"; the don'ts: "Store sensitive information (passwords, API keys, PII) / Let memory grow unbounded / Store everything indiscriminately".

### 3.4 Claude.ai's content boundaries (as the product's position on "what not to remember")

Remembered (11817273): "Your role, projects, and professional context / The people and places in your work and life / Communication preferences and working style / Technical preferences and coding style / Project details and ongoing work".

Not remembered by default (2026-08-25, https://claude.com/blog/claudes-memory-works-everywhere-and-you-decide-whats-in-it ): "By default, Claude does not store topics related to personal or sensitive subject matter, like your health, race, ethnicity, religious beliefs, politics, gender identity, and other similar areas."; users can turn on "Include sensitive topics in memory", and each time such content is saved a notice appears above the input box.

Never remembered (same source): "Some information is never saved to memory, even if you ask. This includes government ID numbers, criminal history, financial account numbers, and immigration status. Claude will let you know when it can't save something for this reason."

"One-off requests are not remembered": Claude.ai has no official statement to this effect; unverified.

---

## 4. Question 3: write trigger timing and wording

### 4.1 The auto memory system prompt for the Claude Code main session: not published

> Claude Code's system prompt isn't published. To give Claude standing instructions, use `CLAUDE.md` files or the `--append-system-prompt` flag. (https://code.claude.com/docs/en/settings#system-prompt )

The official docs only describe the effect: "Saved 2 memories" / "Recalled 2 memories" appearing in the UI means Claude is reading or writing `~/.claude/projects/<project>/memory/` (memory page); a subagent's system prompt "includes instructions for reading and writing to the memory directory … with instructions to curate `MEMORY.md` if it exceeds that limit" (sub-agents page), and its text is likewise unpublished. Third-party transcriptions on the web are not first-hand sources and are not used in this note.

CHANGELOG timeline (npm dates): 2.1.32 (2026-02-05) "Claude now automatically records and recalls memories as it works" first appears; 2.1.59 (2026-02-25) "Claude automatically saves useful context to auto-memory. Manage with /memory"; 2.1.63 (2026-02-28) shared across worktrees; 2.1.75 (2026-03-13) added last-modified to help distinguish old from new; 2.1.186 (2026-06-22) reminder to compact the index near the limit; 2.1.210 (2026-07-14) over-limit became an explicit error.

### 4.2 Officially visible wording sent to the model (three places)

**(a) The protocol the API automatically injects for the memory tool** (memory-tool page, "When the memory tool is present in your request's `tools`, the API automatically adds this instruction to the system prompt. You don't need to send it yourself"):

```
IMPORTANT: ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE.
MEMORY PROTOCOL:
1. Use the `view` command of your `memory` tool to check for earlier progress.
2. ... (work on the task) ...
   - As you make progress, record status / progress / thoughts etc in your memory.
ASSUME INTERRUPTION: Your context window might be reset at any moment, so you risk losing any progress that is not recorded in your memory directory.
```

**(b) Optional reinforcing sentences given in the docs** (same page):

> Note: when editing your memory folder, always try to keep its content up-to-date, coherent and organized. You can rename or delete files that are no longer relevant. Do not create new files unless necessary.

> You can also guide what Claude writes to memory. For example: "Only write down information relevant to <topic> in your memory system."

For the first sentence the docs note "Claude's tool description already tells it to keep the memory directory organized, so you don't need to repeat that instruction"; add it only if things are still messy.

**(c) The error text Claude Code returns to the model when over the limit** (https://code.claude.com/docs/en/errors#memory-index-is-over-its-read-limit ; "Claude Code delivers the error to Claude after the write rather than printing it as a banner in your terminal"):

```
Error: this write left the memory index at MEMORY.md at 214 lines, over its 200-line read limit. The write succeeded, but everything past the limit is silently dropped each time the index is loaded — entries at the end are already invisible to readers. Rewrite it to under 140 lines now: keep one line per entry, move detail into topic files, and merge or drop stale entries.
```

The text of the near-limit "milder reminder" is not published.

### 4.3 Official patterns for trigger timing

- **Record as you go, assume interruption at any time**: the memory tool protocol requires "As you make progress, record" and "ASSUME INTERRUPTION"; with context editing, when nearing the clearing threshold "Claude receives an automatic warning to preserve important information", so Claude writes what needs keeping into memory first (https://platform.claude.com/docs/en/build-with-claude/context-editing ).
- **Multi-session projects follow three phases, "initialize / read at start / update at end"** (memory-tool page, current 2026 version): "For software projects that span multiple agent sessions, set up memory files deliberately instead of writing them ad hoc as work progresses." → "Initializer session … Subsequent sessions: Each new session opens by reading those memory files. … End-of-session update: Before a session ends, it updates the progress log with what was completed and what remains." (this pattern targets progress-type memory; ArcReel has decided progress does not go into memory, so it is for comparison only)
- **Claude Code's criterion**: it does not write every session, "based on whether the information would be useful in a future conversation"; when the user explicitly says "remember", it writes.
- **System prompt of the official Anthropic SDK example** (https://github.com/anthropics/anthropic-sdk-python/blob/main/examples/memory/basic.py , code, not docs): "DO NOT just store the conversation history / No need to mention your memory tool or what you are writing in it to the user, unless they ask / Store facts about the user and their preferences / Before responding, check memory to adjust technical depth and response style appropriately / Keep memories up-to-date - remove outdated info, add new details as you learn them".

### 4.4 Alignment points for the write guidance in ArcReel's `CLAUDE.<mode>.md`

Combining the official wording above, a creative-domain guidance section can cover these aspects and only these (the wording is for the spec to decide; only elements with official grounding are listed here):

1. **Criterion**: remember only content that is "useful in future sessions and cannot be derived by the agent from project data / instruction files" (memory page).
2. **What to remember**: the creator's preferences and corrections (aspect ratio, voice-over, style taboos, etc., corresponding to the official `user` / `feedback`), decisions that appear only in conversation (`project`), external pointers (`reference`).
3. **What not to remember**: production progress, one-off instructions, facts readable from project data such as project.json, rules already written in `CLAUDE.<mode>.md`; sensitive personal information (Claude.ai and Cookbook position).
4. **Form**: one line per index entry, details go into topic files, merge stale entries promptly, don't create new files unless necessary (memory page + memory-tool reinforcing sentence).
5. **Scope limit**: the sentence pattern "Only write down information relevant to <topic>" can narrow the scope to the creative domain (memory-tool page).
6. **No need to repeat instructions that already exist natively**: the native system prompt already contains read/write and curation instructions; the guidance only adds domain content, avoiding the "over-specification" failure mode in best-practices.

---

## 5. Question 4: official precedents for user-visible / editable memory

### 5.1 Claude Code `/memory`

> The `/memory` command lists your CLAUDE.md, CLAUDE.local.md, and other memory file locations across user and project scopes … It also lets you toggle auto memory on or off and provides an option to open the auto memory folder. Select any file to open it in your editor; selecting one that doesn't exist yet creates it first. To check which files actually loaded into the current session, run `/context`. (memory page)

> Auto memory files are plain markdown you can edit or delete at any time. Run `/memory` to browse and open memory files from within a session. (same page)

> Run `/memory` and select the auto memory folder to browse what Claude has saved. Everything is plain markdown you can read, edit, or delete. (same page)

Interaction form: file list + opening the whole file in an external editor (since v2.1.216 it no longer waits for the editor to close); file names in the "Saved N memories" notification are clickable to open (CHANGELOG 2.1.86). No per-entry UI, no per-entry confirmation.

### 5.2 The official principle for "the user edited a file the agent wrote"

The claude-directory page's description of `MEMORY.md` is the only direct statement:

> Claude creates and updates this file as it works; you do not write it yourself. It acts as an index that Claude reads at the start of every session, pointing to topic files for detail. You can edit or delete it, but Claude will keep updating it.

Topic files: "You never create these yourself. Claude reads a topic file back only when the current task relates to it."

Supporting facts: after a user edit, Claude adds the `modified` field as usual the next time it writes ("Any file that has frontmatter gets the field the next time Claude writes it"); files are kept until "you or Claude edits or deletes them". There is **no** merge-protection promise such as "respect user edits, don't restore entries the user deleted"; unverified.

### 5.3 Claude.ai's per-entry editable memory (since 2026-08-25)

> See exactly what Claude remembers about you in Settings > Memory. Everything Claude remembers is listed under Topics. Select any topic to read it, then use the edit icon to change it or select "Delete" to remove it. Fix something in one topic and the change applies to every conversation from then on. (11817273)

> You can also update memory directly from a chat. Tell Claude what you'd like it to remember, change, or forget, and the update applies to your next conversation. (same)

The settings page also has a natural-language edit box "Tell Claude what to change or remove" (https://support.claude.com/en/articles/12123587-import-and-export-your-memory-from-claude ). Pause keeps existing memory but stops reading and writing; Reset permanently deletes all memory including project memory and cannot be undone. Deleting a conversation does not delete the memory entries it produced (new version), but they can be deleted individually. On Team / Enterprise the Owner controls the switch, "Owners can't view or edit a user's individual memories", and when the organization turns it off "all existing memory entries for all users are deleted immediately".

There is likewise **no** promise that "Claude will not overwrite user edits"; the release notes wording is "entries that Claude reads and updates during your conversations" (2026-07-10).

### 5.4 Mapping the precedents to ArcReel

| ArcReel decision already made | Official precedent |
| --- | --- |
| Directory file list + full-text Markdown editing | Claude Code `/memory`: file list + open the whole file for editing |
| Second confirmation for "clear" at each level | Claude.ai Reset memory: "cannot be undone", requires clicking again to confirm |
| Agent writes autonomously, user can edit, no per-entry confirmation | Consistent on both sides; both accept "the user edited it, Claude keeps updating it afterwards" |
| Two levels: user memory / project memory | Claude Code user / project scopes; Claude.ai cross-conversation memory / project memory isolation |

---

## 6. Points that could not be verified

- The text of the auto memory system prompt injected into the Claude Code main session (officially stated as unpublished); the text of the near-limit "milder reminder".
- The merge / protection principle when user edits conflict with Claude's writes (no statement from either Claude Code or Claude.ai).
- `CLAUDE_MEMORY_STORES` (team memory stores) mentioned in CHANGELOG 2.1.172 is not described on any doc page.
- Publication dates of the memory-tool doc page and the original claude-code-best-practices post; the date on the Cookbook memory page contradicts its content.
- "One-off requests are not remembered" has no verbatim statement in Claude Code / Claude.ai; it can only be inferred from the "useful in a future conversation" criterion.

## 7. Source list

Claude Code docs: memory, best-practices, settings, settings-reference, commands, sub-agents, errors, claude-directory, glossary, context-window (all https://code.claude.com/docs/en/… ); CHANGELOG https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md .
Agent SDK docs: overview, sessions, modifying-system-prompts, claude-code-features, hosting, subagents (https://code.claude.com/docs/en/agent-sdk/… ).
Platform docs: memory-tool, context-editing, compaction (https://platform.claude.com/docs/en/… ); Cookbook tool-use-memory-cookbook, tool-use-context-engineering-context-engineering-tools.
Engineering / research articles: effective-context-engineering-for-ai-agents (2025-09-29), building-effective-agents (2024-12-19), effective-harnesses-for-long-running-agents (2025-11-26), research/long-running-Claude (2026-03-23).
Claude product: https://claude.com/blog/memory (2025-09-11, including the 2025-10-23 update), https://claude.com/blog/claudes-memory-works-everywhere-and-you-decide-whats-in-it (2026-08-25), https://claude.com/blog/context-management (2025-09-29); Help Center 11817273, 12123587, 12260368 (incognito), 12138966 (release notes), 14554000.
Code: https://github.com/anthropics/anthropic-sdk-python/blob/main/examples/memory/basic.py .

---

## 8. Draft GLOSSARY.md entries

Proposed for the "Agent runtime" section, placed before the "SDK transcript" entry; the existing "long-term preferences are carried by agent memory" in the "Instructions" entry can stay as is and will point to this entry. Only meaning and Avoid are written, no implementation.

```markdown
**Agent memory**:
Natural-language notes about the creator and the project that the agent persists across sessions, writes autonomously, and that the creator can view and edit. It records only preferences, corrections, decisions and external pointers that the agent cannot derive from project data or instruction files and that remain useful in future sessions; it has two levels, project memory and user memory.
_Avoid_: treating formal project data, the SDK transcript / session event log, one-off instructions or production progress as memory; calling human-written instruction files such as `CLAUDE.<mode>.md` memory.

**Project memory**:
Agent memory that travels with the project and takes effect only in that project's sessions; it carries the project's stylistic direction, confirmed approaches and the creator's corrections for this project.
_Avoid_: reusing it across projects; writing it into formal project data; confusing it with project settings.

**User memory**:
Agent memory owned by a user and in effect for all of that user's projects; it carries the creator's identity/background and long-term working preferences.
_Avoid_: binding it to a service instance or host directory; writing content that holds only for one particular project.
```
