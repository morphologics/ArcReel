# minimax

MiniMax text, image, video and speech media provider.

- Main entry: [MiniMax API overview](https://platform.minimax.io/docs/api-reference/api-overview)
- APIs and capabilities: [Text generation](https://platform.minimax.io/docs/guides/text-generation), [Chat Completions (OpenAI-compatible; thinking / reasoning_split parameters; thinking content is embedded in content inside `<think>` tags by default)](https://platform.minimax.io/docs/api-reference/text-openai-api), [Image generation](https://platform.minimax.io/docs/guides/image-generation), [Video generation](https://platform.minimax.io/docs/guides/video-generation), [Video generation V2](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create.md), [Synchronous speech synthesis T2A](https://platform.minimaxi.com/docs/api-reference/speech-t2a-http.md)
- Billing: [Pay-as-you-go pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo.md)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["minimax"]`, `lib/backends/text_backends/openai.py::OpenAITextBackend`, `lib/backends/image_backends/minimax.py::MiniMaxImageBackend`, `lib/backends/audio_backends/minimax.py::MiniMaxAudioBackend`, `lib/custom_provider/builtin_endpoints/minimax-hailuo-v1.json`, `lib/custom_provider/builtin_endpoints/minimax-hailuo-v1-fast.json`, `lib/custom_provider/builtin_endpoints/minimax-s2v-01.json`, `lib/custom_provider/builtin_endpoints/minimax-h3.json`
