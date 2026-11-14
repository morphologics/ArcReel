---
status: accepted
---

> The Windows fallback Bash allowlist now only allows the `python .claude/skills/` entry point and no longer allows ffmpeg and ffprobe. Token-boundary matching is kept as a general rule; `ffmpegX` in the body was the example at the time; the three rejection rules and the decision to enforce secrets in three layers remain in effect.

# Provider secrets banned from the process environment: startup assertion + agent env override + Bash env -u scrubbing

If provider secrets get into `os.environ`, they are inherited wholesale by agent subprocesses, which is a large leak surface, and a process-level single value conflicts with multiple credentials/per-session switching. We decided secrets are stored only in the DB, enforced in three layers: ① startup assertion — if `os.environ` contains any real provider secret (`PROVIDER_SECRET_KEYS`), it fails fast and refuses to start; deployments that configure keys via environment variables will not be able to start, and this is deliberate; ② agent subprocess env — Anthropic credentials are injected per session from the DB (see `docs/adr/0017`), and all other provider variables are overridden with empty strings; ③ Bash commands are wrapped by a PreToolUse hook into `env -u … sh -c`, dynamically unsetting based on a fixed list + pattern scanning such as `*_API_KEY`/`*_AUTH_TOKEN`, intercepting any sensitive variable still inherited from the host environment.

## Consequences

- When adding a provider, its env variable names must be registered in `lib/config/env_keys.py` (the startup assertion/env override/Bash scrubbing share this list; pattern scanning can only cover regularly named variables).
- ③ is a POSIX mechanism (depending on `env`/`sh`) and the wrapping is only performed when the sandbox is available (macOS/Linux); the hook only rewrites the command and makes no permission decision; allowing is done by later steps of the permission chain (allowed_tools). Under the native Windows fallback the hook skips wrapping, and the original command is judged by `can_use_tool`'s Bash allowlist, with three rejection rules: token-boundary matching (blocks prefix collisions like `ffmpegX`), shell metachar interception (semicolon, `&`, pipe, angle brackets, backtick, `$`, newline — blocks chaining/pipes/redirection/command substitution), and `..` path traversal interception (including obfuscated forms like quoted `".."` and backslash-escaped `.\.`, blocking escapes from the skills directory to run arbitrary scripts); among these, the `python` entry point is further restricted to `.claude/skills/<skill>/scripts/<script>.py`. For operational troubleshooting, the rejection message states which rule was hit. Layers ① and ② are platform-independent.
- Complementary to `docs/adr/0017`: 0017 covers "how to inject" Anthropic credentials; this one covers "how not to leak" all provider secrets.
