---
status: accepted
---

# Media cache uses file mtime as content-addressed cache-bust; versioned/parameterised files are set immutable

A session-level revision counter causes re-downloads per counter on page refresh, re-entering across sessions and virtual-scroll remounts, and a new session's `?v=0` may correspond to changed content. We decided the frontend uses the file mtime (nanoseconds) as the `?v=` parameter of asset URLs (asset fingerprint) instead of a counter, and the file route sets `Cache-Control: public, max-age=31536000, immutable` on requests that carry `?v=` or whose path contains `versions/` — "content unchanged → URL unchanged → disk cache hit" holds for all the scenarios above.

## Consequences

- The fingerprint has to be sent down with the project API and SSE events, and the frontend maintains a fingerprint store.
- Once the `immutable` header is sent, the client caches long-term; precisely because of this, the fingerprint must change with content, and the immutable historical files under `versions/` are a natural fit.
