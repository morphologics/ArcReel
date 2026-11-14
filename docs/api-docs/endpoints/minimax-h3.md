# minimax-h3

- Protocol: [Video generation V2](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create.md)
- Billing: [Pay-as-you-go pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo.md)
- Code: `lib/custom_provider/builtin_endpoints/minimax-h3.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Request shape: multimodal `content[]` carries the first frame, last frame, reference images and reference audio by role; same host as v1 but a different path prefix (`/v2`); the query response includes a time-limited download URL directly
