# Maintaining the provider API docs index

This directory holds navigation only: links to official sources, the topics ArcReel depends on, and stable code entry points. Runtime facts still belong to the registry, backends and endpoint declarations, and are never loaded back from the index.

## Change process

1. Locate the corresponding file by the canonical id in `PROVIDER_REGISTRY` / `ENDPOINT_REGISTRY`; one file per id.
2. Verify interfaces, capability and parameter constraints, and billing against official public pages. When plain fetching does not work, try Jina Reader, `agent-browser` or the user's browser in that order, still keeping the source official.
3. Update this id's official entry points, the pages ArcReel actually depends on, and the code `path::symbol`. Shared docs may be linked repeatedly; do not copy page content or values.
4. When adding or removing a registry entry, update the corresponding README in this directory as well; it is done when every canonical id can be reached from the README to an index of the same name, and the index reaches the official source and the code that actually consumes it.

Saving the minimal request/response fixtures needed by tests is a code contract, not a docs mirror; keep fixtures minimal and cite their source. The repository does not store third-party documentation text, complete parameter/price tables, or download scripts used to rebuild mirrors.
