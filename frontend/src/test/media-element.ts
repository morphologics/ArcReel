import { vi } from "vitest";

/**
 * jsdom's HTMLMediaElement does not implement playback: play/pause/load throw "Not implemented".
 * Call this before rendering components with a `<video>`/`<audio>` player to replace them all with side-effect-free
 * stubs; when a player uses a new media element method, add it only here. Returns the spies, for asserting the call
 * objects (`mock.contexts`). Restored by `vi.restoreAllMocks()`.
 *
 * jsdom 的 HTMLMediaElement 不实现播放：play/pause/load 会抛 "Not implemented"。
 * 渲染带 `<video>`/`<audio>` 播放器的组件前调用，统一替换为无副作用的桩；
 * 播放器用到新的媒体元素方法时只在这里补。返回各个 spy，供断言调用对象（`mock.contexts`）。
 * 随 `vi.restoreAllMocks()` 还原。
 */
export function stubMediaElementPlayback() {
  return {
    play: vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(),
    pause: vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined),
    load: vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined),
  };
}
