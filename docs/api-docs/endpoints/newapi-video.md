# newapi-video

- Protocol: [Video generation API](https://doc.newapi.pro/api/generate-video/), [Kling and Jimeng formats](https://doc.newapi.pro/api/kling-jimeng/)
- Billing: [Model billing notes](https://doc.newapi.pro/guide/pricing/); actual prices are configured by the deployer
- Code: `lib/custom_provider/builtin_endpoints/newapi-video.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Task status and response shape: status strings are passed through from the underlying vendor and go through the shared normalizer `packages/arcreel-market-core/src/arcreel_market_core/video_backend_contract.py::normalize_provider_status`; status, video URL and metadata are probed using the union of the path tables in the definition's `poll.extract`, which supports both flat responses and responses wrapped in `data`
