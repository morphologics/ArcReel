---
name: adapt-custom-endpoint
description: Use when the user asks to integrate into ArcReel a video or image provider that uses a JSON submit-then-poll protocol, or asks to write, validate, test or save a custom endpoint definition.
---

# Adapt a Custom Endpoint

Use the `scripts/custom_endpoint.py` shipped with this skill to call the ArcReel HTTP API; the shared validator and the API are the only way to validate and save definitions.

## Workflow

1. Read the provider documentation URL the user gives; if it cannot be read, ask the user to paste submit, task query and response examples.
   Confirm the protocol is the JSON request/response "submit-then-poll" form. Signature auth, multipart requests, or routing that switches by asset
   cannot be expressed by the first-phase definition; state the gap directly.
2. When you need to write or fix a definition, read [Definition format](references/definition-format.md), and in the working directory create the definition JSON,
   the test parameters JSON, and the real response samples provided by the provider. For credentials, prefer referencing an ArcReel saved custom provider
   `provider_id`; NEVER write an API Key into project files, command arguments or replies.
3. Run `scripts/custom_endpoint.py` in the current skill directory and validate with the `validate <definition.json>` subcommand.
   Fix until `errors` is empty. When `schema_version.level` is not `direct` or there are still `warnings`, fix them or explain to the user why they are accepted and their impact; use `hints` to prepare the endpoint URL and model needed for testing.
4. Run `check-response` on the provider's submit and poll responses separately; when the definition includes `result`, also check the fetch response.
5. Run `preview-request` and check the URL, method, masked headers, body and asset summary. When the definition has required assets,
   attach the corresponding files with `--start-image` / `--end-image` / `--reference-images` / `--reference-audio-files`.
6. **A trial run makes a real request to the provider and may incur charges. You MUST ask the user before calling it.** Only after explicit consent run
   `trial-run ... --confirm-cost` (reusing the asset arguments from the preview), then query with `trial-status <run-id>` until a terminal state,
   and relay the request, response, extracted values and errors. Continue when the status is `succeeded`; stop on other terminal states, continuing only if the user explicitly asks to save an unverified definition.
7. Validate again. When there is no endpoint of the same lineage, create a new one with `save`; when there is a duplicate, by default save a copy and tell the user.
   **Only overwriting an existing endpoint MUST be asked back to the user**; only after explicit consent use
   `save ... --endpoint-id <id> --confirm-overwrite`. Saving is complete when `ce-<id>` is returned.

## Connection

Run the script from the current workspace root. The script reads `.arcreel/settings.json` created by `setup-arcreel-skills`, derives the same instance's `/api/v1` address from
`mcp_url`, and authenticates with `api_key`; when the configuration is missing or invalid, run the setup skill first;
do not fall back to localhost.

When `ARCREEL_EMBEDDED_AGENT=1` is set, the script prefers `ARCREEL_API_BASE` and `ARCREEL_API_TOKEN`; that JWT
is valid for 15 minutes and is not renewed; when an API call returns 401, tell the user to reopen the session to get a new token.
Local deployments with `AUTH_ENABLED=false` may leave the token empty.
