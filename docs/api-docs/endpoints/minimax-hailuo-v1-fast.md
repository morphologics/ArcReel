# minimax-hailuo-v1-fast

- Protocol: [MiniMax video generation](https://platform.minimax.io/docs/guides/video-generation)
- Billing: [Pay-as-you-go pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo.md)
- Code: `lib/custom_provider/builtin_endpoints/minimax-hailuo-v1-fast.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Difference from minimax-hailuo-v1: same request shape, but 2.3-Fast accepts only image-to-video, so the definition declares the first frame as a required input and `text_to_video` is derived as false from that
