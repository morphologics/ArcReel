import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useProgressiveImage, withWidth } from "./useProgressiveImage";

describe("useProgressiveImage (YouReel)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the resized copy, then the original once it has loaded", () => {
    const images: { onload: (() => void) | null; src: string }[] = [];
    vi.stubGlobal("Image", class { onload: (() => void) | null = null; src = ""; constructor() { images.push(this); } });
    const { result } = renderHook(() => useProgressiveImage("/api/v1/files/p/a.png?v=3", 640));
    expect(result.current).toBe("/api/v1/files/p/a.png?v=3&w=640");
    act(() => images[0].onload?.());
    expect(result.current).toBe("/api/v1/files/p/a.png?v=3");
  });

  it("adds or replaces the width, and leaves data: addresses alone", () => {
    expect(withWidth("/files/a.png", 320)).toBe("/files/a.png?w=320");
    expect(withWidth("/files/a.png?w=160", 320)).toBe("/files/a.png?w=320");
    expect(withWidth("/files/a.png?v=1&w=160", 320)).toBe("/files/a.png?v=1&w=320");
    expect(withWidth("data:image/svg+xml,x", 320)).toBe("data:image/svg+xml,x");
  });
});
