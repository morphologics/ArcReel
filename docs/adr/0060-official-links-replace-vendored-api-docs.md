---
status: accepted
---

# Third-party API documentation uses an index of official links rather than mirrored content

ArcReel no longer commits the body text of provider API and Claude Agent SDK documentation, complete parameter/pricing tables, or mirror download scripts to the repository; media providers and endpoints each maintain official links and stable code entry points under their canonical id, and SDK development consults the official online documentation and uses the official verifier. Runtime capabilities and values continue to be held by the registry, backend, and endpoint declarations; the index is responsible only for source discovery and verification navigation.

This choice reduces copy drift caused by upstream updates and the repository maintenance burden, at the cost of losing offline body text, stable line numbers, and complete reproduction of historical pages. The exact semantics of historical versions are instead evidenced by pinned-version upstream source code or this repository's contract tests; the minimal request/response fixtures needed by tests may be kept, but must not grow into copies of third-party documentation.
