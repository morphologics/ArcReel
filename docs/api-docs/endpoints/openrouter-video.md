# openrouter-video

- Protocol: [Video generation guide](https://openrouter.ai/docs/guides/overview/multimodal/video-generation), [Submit a job](https://openrouter.ai/docs/api/api-reference/video-generation/submit-a-video-generation-request), [Poll a job](https://openrouter.ai/docs/api/api-reference/video-generation/poll-video-generation-status)
- Model capabilities: the durations, resolutions and aspect ratios each model supports are as reported by `GET /api/v1/videos/models`
- Billing: [Models](https://openrouter.ai/models); once a job completes, the response reports the actual cost in `usage.cost`
- Code: `lib/custom_provider/builtin_endpoints/openrouter-video.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Output: `unsigned_urls` point to OpenRouter's own `/api/v1/videos/{jobId}/content`; they are not presigned, so downloading requires the same credentials as polling; since they share the polling origin, the runtime downloads them with credentials
