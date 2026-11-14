---
status: accepted
---

# Server startup hard-fails when the kernel sandbox is unavailable; only Windows has a documented degraded mode

The security model of agent tools is built on the kernel sandbox (macOS Seatbelt / Linux bwrap); if detection fails and it silently degrades to running without a sandbox, the defences against out-of-bounds file access and outbound requests disappear without a sound. We decided to place the sandbox availability check at server startup (`check_sandbox_available`): if macOS lacks `sandbox-exec`, Linux lacks `bwrap`/`socat`, or bwrap exists but a trial run fails, it raises directly and the whole service refuses to start (fail-closed), rather than running degraded or only erroring when a session is created; the only exception is Windows, which natively has no sandbox — after a warning the sandbox is disabled, and the agent Bash tool switches to a code-level prefix allowlist.

## Consequences

- The deployment environment must have the sandbox dependencies installed first (macOS ships `sandbox-exec`; Linux needs `bwrap` + `socat`), otherwise the service cannot start. This is deliberate: better to refuse service than to run the agent without a sandbox.
- The Windows prefix allowlist is coarser-grained than the sandbox (the command prefixes it can allow are limited); production deployments are still recommended to use WSL2/Docker to get the full sandbox.
