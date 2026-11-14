---
status: accepted
---

# Agent file protection in two layers: the kernel sandbox governs the Bash subprocess tree; built-in file tools go through a PreToolUse hook

The kernel sandbox (`SandboxSettings.filesystem.denyRead/denyWrite`) only constrains the Bash tool and all subprocesses it spawns; the SDK's built-in Read/Write/Edit/Glob/Grep do not go through Bash and execute directly in the main process, which the kernel sandbox cannot cover. We decided to add a second layer for built-in file tools with an application-level PreToolUse hook (`_is_path_allowed`: sensitive files, cross-project reads/writes, code extensions, cwd out of bounds), with both layers covering path rules from the same source. The interception point must be the PreToolUse hook rather than `can_use_tool`: in the SDK permission chain, Read/Glob/Grep are first let through by allow rules and never reach `can_use_tool`.

## Consequences

- Reviews of changes involving agent file access must confirm separately: whether the Bash path can be intercepted (sandbox profile), and whether the built-in tool path can be intercepted (hook) — either layer on its own has a bypass.
- `docs/adr/0003` is the concrete application of this two-layer setup to "project JSON writes" (denyWrite and `_check_write_access`, two layers from the same source).
