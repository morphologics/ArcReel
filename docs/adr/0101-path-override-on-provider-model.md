---
status: accepted
---

# Path overrides are recorded on the provider model, replacing the whole path per request of the endpoint

Some relay services keep the official protocol and only add a prefix to the path, change the version segment or rename the path, and different protocols of the same vendor are often mounted under different prefixes. Previously, such differences could only be adjusted by writing a different `/vN` into the base_url, and the remaining paths of Python endpoints were fixed by the SDK. Now path overrides are recorded on provider models of custom providers: each request sent by an invocation endpoint can have its whole path replaced individually, e.g. submit, poll and fetch result of a declarative definition. Path overrides live at the provider model level because the number of models actually attached is small, and configuring per model is most flexible.

## Rules

- Path overrides are open only to custom providers, apply to Python endpoints and declarative endpoints, and do not apply to ComfyUI endpoints.
- Each invocation endpoint names the requests it sends and gives default paths; path overrides are recorded by request name. Placeholders usable in a path are limited to values that the request itself already has, such as the API model name or task id.
- Path overrides are relative to the raw base_url value the provider entered. Requests with an override no longer go through the endpoint's base_url normalisation (e.g. automatically appending `/v1`); without an override, behaviour is unchanged.
- Path overrides only replace the path. If the relay service also rewrites the request or response schema, describe it with a declarative definition.
- When an override path carries a query, it replaces the original request's query entirely rather than merging with it; parameters of the original request that need to be kept (e.g. `alt=media` on Gemini download URLs) must be written into the override path.
- Resumption replays polling and fetching against the base_url at submission time, and path overrides also recognise requests by it. What is persisted is the normalised base_url, and the raw path that overrides are relative to is restored from the current configuration's "raw value → normalised value" delta. Changing the path prefix is supported only when that delta is unchanged, e.g. raw `/old` changed to `/new`, with the endpoint appending `/v1` to both.
- The raw path is not persisted separately, so when the normalisation delta changes, the override URL at submission cannot be restored unambiguously. For example, if raw `/old/v1` is changed to `/new`, the submitted value equals the normalised result of raw `/old`, and resumption may lose the raw version segment. Bailian normalisation keeps ordinary path prefixes, strips known suffixes and then appends `/api/v1`; changes to ordinary prefixes are likewise subject to the delta condition above, and not all raw paths are lost.
- Python endpoints uniformly rewrite paths by request name at the HTTP transport layer, rather than switching to a synthesised base_url.
- Path overrides are part of the custom invocation endpoint protocol and are frozen with it in the execution checkpoint; if the override has been changed at resumption, it is handled as protocol drift per `docs/adr/0007`.
- Model discovery and connectivity checks do not use path overrides; they still send requests using the provider's base_url and discovery format.
- A provider model newly attached to an endpoint has no path overrides and does not inherit them from sibling models; when switching to a different endpoint, existing overrides are discarded. After an endpoint definition is updated, path overrides still take precedence, and overrides for request names that no longer exist in the definition are ignored.

## Considered Options

- **Record one per "provider × invocation endpoint"**: models under the same provider attached to the same endpoint would not need repeated configuration, and the research also suggested this. But if models under the same endpoint need different paths, they could only be split into two providers.
- **"Copy as mine" endpoint definitions**: Python endpoints have no definition to copy. Custom invocation endpoints are global, so every relay service connected would add another nearly identical definition.
- **Only add a prefix, or give the endpoint its own base_url**: cannot change path names, and the individual requests of an async protocol cannot be adjusted separately.
- **Inherit overrides from sibling models when creating or re-attaching an endpoint**: when sibling models' overrides disagree, there is no value to go by; the rules for the two entry points, creation and re-attachment, would also be hard to unify.
- **Synthesise a base_url when possible, and rewrite at the transport only when not**: two mechanisms coexist, and every request would first have to decide which one to take.

## Consequences

- Multiple models under the same relay service each need the same override filled in.
- Request URLs in declarative definitions that do not start with `{{ base_url }}` (e.g. download URLs taken from a response) have no path to replace and do not accept path overrides.
