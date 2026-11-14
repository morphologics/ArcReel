# ArcReel Custom Endpoint Definition Format

The server-side shared validator is the final arbiter for saving, preview requests, check responses and trial runs. The current full JSON Schema is at
[`packages/arcreel-market-core/src/arcreel_market_core/endpoint_definition/schema.json`](https://github.com/ArcReel/ArcReel/blob/main/packages/arcreel-market-core/src/arcreel_market_core/endpoint_definition/schema.json);
write a minimal definition first, then fix it based on the field paths and error codes returned by `validate`.

## Minimal shape

```json
{
  "kind": "declarative",
  "schema_version": "1.2.0",
  "meta": {"name": "Demo Video", "author": "user", "version": "1.0.0"},
  "auth": {"headers": {"Authorization": "Bearer {{ api_key }}"}},
  "submit": {
    "method": "POST",
    "url": "{{ base_url }}/videos",
    "body": {"model": "{{ model }}", "prompt": "{{ prompt }}", "duration": "{{ duration }}"},
    "extract": {"task_id": ["$.id"], "error": ["$.error.message", "$.message"]}
  },
  "poll": {
    "method": "GET",
    "url": "{{ base_url }}/videos/{{ task_id }}",
    "extract": {
      "status": ["$.status"],
      "video_url": ["$.output.url"],
      "error": ["$.error.message", "$.message"]
    }
  },
  "status_map": {
    "queued": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed"
  },
  "capabilities": {"text_to_video": true}
}
```

## Constraints

- `auth` may be empty; when non-empty, at least one place references `{{ api_key }}`, and credentials may only appear in `auth.headers` or
  `auth.query`. Ordinary request headers, body and URL must not carry credentials separately.
- Template variables for video definitions are `base_url`, `api_key` (auth only), `model`, `prompt`, `duration`,
  `duration_seconds`, `aspect_ratio`, `resolution`, `generate_audio`, `seed`, `width`, `height`,
  `task_id`, `result_id` and `inputs.<name>`. A whole-string single placeholder keeps its original type; when the value is null the containing field is removed.
- Assets are declared in `inputs`; `source` is `start_image`, `end_image`, `reference_images` or
  `reference_audio_files`, and `encoding` is `data_uri` or `base64`. List assets are expanded with `$each`; optional objects
  are guarded with `$when`. `capabilities` must match the assets actually referenced by submit in both directions.
- `enum_maps` maps ArcReel parameter values to provider values; `defaults` only fills ArcReel-side defaults for optional parameters.
- `extract` is a JSONPath array ordered by priority. Paths start at `$` and use only child segments and simple filters;
  no recursive descent, unions, slices with step, or functions. When a response field contains a JSON string, use
  `{"path":"$.data","json_decode":true,"then":["$.id"]}`.
- `status_map` targets are only `queued`, `running`, `succeeded`, `failed`. If a second fetch is needed after success,
  extract `result_id` in poll and add a `result` section that gets `video_url` from `task_id` / `result_id`.
- Image definitions write `"media_type": "image"`; omitting it means video. Template variables for image definitions are only `base_url`, `api_key` (auth only),
  `model`, `prompt`, `aspect_ratio`, `resolution`, `seed`, `width`, `height`, `task_id` and `result_id`;
  `capabilities` declares at least one of `text_to_image` or `image_to_image`, and both may be declared; the image path goes in `image_url` (image address)
  or `image_b64` (base64 inline in the response; both a bare string and `data:image/...;base64,` are accepted), at least one of them; when both are written the address is taken first;
  with a `result` section they go in `result.extract`. Validation fails when an image definition contains video-only items such as duration, audio, first/end frames or `usage`.
- Image definition assets accept only `source: "reference_images"`, encoded as `data_uri` or `base64`, expanded via `$each` in submit.
  Declaring `image_to_image` also requires a positive `max_reference_images` and referencing the reference image asset in submit;
  when `text_to_image` is also declared, the reference image input cannot be required. Image editing instructions are sent with `prompt`.

## Test inputs

`parameters.json` uses the same names as the API, for example:

```json
{"model":"provider-model","prompt":"short test","duration_seconds":5,"aspect_ratio":"9:16","generate_audio":false}
```

`credentials-ref.json` should preferably only reference a saved provider:

```json
{"provider_id":"custom-1"}
```

Asset arguments map one-to-one to multipart fields: `--start-image` -> `start_image`, `--end-image` ->
`end_image`, `--reference-images` -> `reference_images`, `--reference-audio-files` ->
`reference_audio_files`. The last two may be passed repeatedly with multiple files; preview requests and trial runs use the same set of arguments.
