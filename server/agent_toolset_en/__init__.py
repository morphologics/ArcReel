"""English reference copy of server/agent_toolset (not imported at runtime).

Agent toolset: one host-agnostic declaration per tool; the ArcReel Agent and external Agents each project it through a
thin adapter.

The declarations and the unified invocation entry point do not depend on any host SDK; the embedded adapter
(``embedded``) and the remote adapter (``remote``) are only responsible for how the project is determined and how
results are packed into their respective envelopes.
"""
