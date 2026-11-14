# minimax-hailuo-v1

- Protocol: [MiniMax video generation](https://platform.minimax.io/docs/guides/video-generation)
- Billing: [Pay-as-you-go pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo.md)
- Code: `lib/custom_provider/builtin_endpoints/minimax-hailuo-v1.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Retrieval: the query response returns a `file_id`, which is then exchanged via `/v1/files/retrieve` for a time-limited `file.download_url` (the definition's `result` section)
