# v2-video-generations

- Protocol: [AIMLAPI video generation](https://docs.aimlapi.com/api-references/video-models)
- Scope: ArcReel implements only the common subset of `/v2/video/generations` shared by multiple relay services; the exact model fields are still governed by the official docs of whichever relay service is connected.
- Billing: [AIMLAPI pricing](https://aimlapi.com/ai-ml-api-pricing); the shared protocol has no unified pricing across relay services
- Code: `lib/custom_provider/builtin_endpoints/v2-video-generations.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Task status: under the shared protocol each relay service spells status strings differently, so they go through the shared normalizer `packages/arcreel-market-core/src/arcreel_market_core/video_backend_contract.py::normalize_provider_status`; this protocol has no expiry semantics, so the definition's `status_map` folds `expired` into `failed` on top of that
