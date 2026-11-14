---
status: accepted
---

# Custom provider endpoints are not restricted by scheme

Neither a custom provider's `base_url` nor the request URL in a custom call endpoint definition is restricted by scheme; `http://` is accepted just like `https://`. Self-hosted services, relay endpoints within a LAN, and local gateways on `http://localhost` are explicitly supported use cases, and requiring TLS for them would make this whole class of endpoints unusable.

When a user configures credentials for an `http://` endpoint, those credentials (request headers or query parameters rendered from the `auth` section) travel in plaintext over the link. This risk is borne by the user for the endpoints they configure: ArcReel does not judge on the user's behalf whether a given intranet address needs TLS.

The behavior is consistent across all paths in the product — creating and updating providers, model discovery, connection tests, and the declarative runtime's submit / poll / second fetch and artifact download are never routed differently by scheme. No gate is placed on any single one of these paths: rejecting `http://` in only one place would cause splits like "model discovery passes but actually running a video is rejected", where the same configuration yields different conclusions at different entry points.

The propagation scope of credentials is still bound by same-origin constraints: cross-origin redirects always strip the request headers and query credentials rendered from the `auth` section, and artifact URLs get credentials attached only when they are same-origin with an endpoint already accessed with credentials in this call. This constraint is independent of scheme and holds under both schemes.
