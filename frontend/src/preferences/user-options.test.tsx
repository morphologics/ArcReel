import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API } from "@/api";
import { resetOpenReelProbeForTests } from "@/components/canvas/edit/open-in-openreel";
import { useAppStore } from "@/stores/app-store";

import {
  CLIP_DRAG_STORAGE_KEY,
  CONFIRM_DELETE_TRACK_STORAGE_KEY,
  EDITOR_OWN_TAB_STORAGE_KEY,
  useDefaultEditor,
  useStudioOptionsStore,
  useUserOption,
} from "./user-options";

function serveOpenReel(served: boolean) {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(served ? "<title>OpenReel</title>" : "<title>Studio</title>", { status: 200 }),
  );
}

beforeEach(() => {
  useStudioOptionsStore.setState(useStudioOptionsStore.getInitialState(), true);
  useAppStore.setState({ toast: null });
  resetOpenReelProbeForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("device options shared with OpenReel", () => {
  it("drag on clips writes `move` under the OpenReel key and removes the key for the default `pan`", () => {
    const { result } = renderHook(() => useUserOption("timeline_clip_drag"));
    expect(result.current[0]).toBe("pan");

    act(() => result.current[1]("move"));
    expect(window.localStorage.getItem(CLIP_DRAG_STORAGE_KEY)).toBe("move");
    expect(result.current[0]).toBe("move");

    act(() => result.current[1]("pan"));
    expect(window.localStorage.getItem(CLIP_DRAG_STORAGE_KEY)).toBeNull();
    expect(result.current[0]).toBe("pan");
  });

  it("reads anything but `move` as pan, as OpenReel does", () => {
    window.localStorage.setItem(CLIP_DRAG_STORAGE_KEY, "sideways");
    const { result } = renderHook(() => useUserOption("timeline_clip_drag"));
    expect(result.current[0]).toBe("pan");
  });

  it("confirm before deleting a track stores `off` and asks again once the key is gone", () => {
    const { result } = renderHook(() => useUserOption("confirm_delete_track"));
    expect(result.current[0]).toBe("ask");

    act(() => result.current[1]("off"));
    expect(window.localStorage.getItem(CONFIRM_DELETE_TRACK_STORAGE_KEY)).toBe("off");

    act(() => result.current[1]("ask"));
    expect(window.localStorage.getItem(CONFIRM_DELETE_TRACK_STORAGE_KEY)).toBeNull();
  });

  it("the editor's own tab is on by default and stores `off` when turned off", () => {
    const { result } = renderHook(() => useUserOption("editor_own_tab"));
    expect(result.current[0]).toBe("on");

    act(() => result.current[1]("off"));
    expect(window.localStorage.getItem(EDITOR_OWN_TAB_STORAGE_KEY)).toBe("off");
    expect(result.current[0]).toBe("off");
  });

  it("follows a change made in another tab", () => {
    const { result } = renderHook(() => useUserOption("timeline_clip_drag"));
    act(() => {
      window.localStorage.setItem(CLIP_DRAG_STORAGE_KEY, "move");
      window.dispatchEvent(new StorageEvent("storage", { key: CLIP_DRAG_STORAGE_KEY }));
    });
    expect(result.current[0]).toBe("move");
  });
});

describe("studio options saved on the server", () => {
  it("shows the default until the server answers, then the stored value", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({ mode: "easy" });
    const { result } = renderHook(() => useUserOption("mode"));
    expect(result.current[0]).toBe("advanced");
    await waitFor(() => expect(result.current[0]).toBe("easy"));
  });

  it("saves a change with only that key and shows it at once", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({});
    const update = vi.spyOn(API, "updateUserOptions").mockResolvedValue({ mode: "easy" });
    const { result } = renderHook(() => useUserOption("mode"));
    await waitFor(() => expect(useStudioOptionsStore.getState().status).toBe("ready"));

    act(() => result.current[1]("easy"));

    expect(result.current[0]).toBe("easy");
    expect(update).toHaveBeenCalledWith({ mode: "easy" });
  });

  it("a failed save goes back to the previous value and says so", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({ default_editor: "arcreel" });
    vi.spyOn(API, "updateUserOptions").mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useUserOption("default_editor"));
    await waitFor(() => expect(useStudioOptionsStore.getState().status).toBe("ready"));

    await act(async () => {
      result.current[1]("openreel");
    });

    await waitFor(() => expect(result.current[0]).toBe("arcreel"));
    expect(useAppStore.getState().toast).toMatchObject({ tone: "error", text: "选项未能保存。请检查网络后重试。" });
  });
});

describe("useDefaultEditor", () => {
  it("opens the built-in editor when OpenReel is chosen but this studio doesn't serve it", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({ default_editor: "openreel" });
    serveOpenReel(false);
    const { result } = renderHook(() => useDefaultEditor());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.editor).toBe("arcreel");
  });

  it("opens OpenReel when it is chosen and served", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({ default_editor: "openreel" });
    serveOpenReel(true);
    const { result } = renderHook(() => useDefaultEditor());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.editor).toBe("openreel");
  });

  it("is ready with the default, OpenReel, even when the options can't be loaded", async () => {
    vi.spyOn(API, "getUserOptions").mockRejectedValue(new Error("offline"));
    serveOpenReel(true);
    const { result } = renderHook(() => useDefaultEditor());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.editor).toBe("openreel");
  });

  it("a member who never chose gets OpenReel where it is served, the built-in editor elsewhere", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({});
    serveOpenReel(true);
    const served = renderHook(() => useDefaultEditor());
    await waitFor(() => expect(served.result.current.ready).toBe(true));
    expect(served.result.current.editor).toBe("openreel");
    served.unmount();

    resetOpenReelProbeForTests();
    useStudioOptionsStore.setState(useStudioOptionsStore.getInitialState(), true);
    serveOpenReel(false);
    const notServed = renderHook(() => useDefaultEditor());
    await waitFor(() => expect(notServed.result.current.ready).toBe(true));
    expect(notServed.result.current.editor).toBe("arcreel");
  });

  it("the built-in editor, once chosen, opens even where OpenReel is served", async () => {
    vi.spyOn(API, "getUserOptions").mockResolvedValue({ default_editor: "arcreel" });
    serveOpenReel(true);
    const { result } = renderHook(() => useDefaultEditor());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.editor).toBe("arcreel");
  });
});
