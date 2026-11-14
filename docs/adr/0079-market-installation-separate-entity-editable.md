---
status: accepted
---

# The installation record is a separate entity; installed endpoints can be edited in place and are not locked

Installing an invocation endpoint from the market produces an ordinary custom invocation endpoint row, plus a binding saying "which entry of which market source it came from". We decided that **this binding is stored as a separate entity, one-to-one with the endpoint, recording the source market source's canonical key, the entry slug, and the version and definition digest at install time**, and is not placed on the `custom_endpoint` table: that table's invariant is "every column other than `definition` is a read-only mirror derived from the definition" (`docs/adr/0067`), and an installation record is not a mirror but a fact with its own lifecycle. The source is a canonical key string rather than a foreign key, so deleting a market source does not erase the origin of installed endpoints. "Installed" recognises only installation records: a local definition with the same author and the same name does not count as installed; overwriting an existing definition is open only to endpoints without an installation record, and installation records are never transferred across sources.

**Installed endpoints can be edited in place and are not locked.** Built-in invocation endpoints are read-only plus "copy as mine", and the copy does not follow upstream; market endpoints do not follow that pattern, because changing one request header would mean copying and then re-attaching the model rows. The cost is that local modifications get overwritten by updates; this is handled by a "modified" state: if the install digest differs from the current definition, the endpoint is marked modified, and before updating the user is warned and can export first. Status is therefore two orthogonal axes: the market axis (latest / update available / unavailable in market) and the local axis (modified or not). **There is no action to detach the installation record**; an installed endpoint can only be updated, deleted, or kept in use with its badge.

**"Update available" only compares whether the version in the installation record equals the version in the index**; it does not look at the local definition's version number, nor compare content: refreshing reads only the index rather than fetching each definition, and the index does not carry a payload digest. Changing content under the same version will not be judged as an available update; this is intentional. The official repository requires a version bump for content changes through submission review; for third-party sources, users bear the risk.

## Revision of the earlier import decision

When the share file format was finalised, it was decided that "imports do not record their origin". This ADR revises that: **the install origin is recorded, but outside the endpoint definition**; the definition itself remains the sole truth of the protocol, and exported bytes are unchanged. Imports from local files continue not to record an origin.

## Consequences

- Installation is a single-transaction server interface, not orchestrated step by step by the frontend: only a single transaction can guarantee uniqueness of the installation record and that "the digest in the record is the definition that was stored".
- Uninstalling means deleting the endpoint; the installation record is cascade-deleted.
- When prompt / style templates enter the market in future, they will have their own installation records rather than reusing the endpoint's.
