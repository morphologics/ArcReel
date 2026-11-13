import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FullScreenToggle } from "./FullScreenToggle";

// jsdom has no Fullscreen API: stand in for the browser's
let fullscreenElement: Element | null = null;
const requestFullscreen = vi.fn(async () => {
  fullscreenElement = document.documentElement;
  document.dispatchEvent(new Event("fullscreenchange"));
});
const exitFullscreen = vi.fn(async () => {
  fullscreenElement = null;
  document.dispatchEvent(new Event("fullscreenchange"));
});

beforeEach(() => {
  fullscreenElement = null;
  Object.defineProperty(document, "fullscreenEnabled", { configurable: true, get: () => true });
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => fullscreenElement });
  Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exitFullscreen });
  Object.defineProperty(document.documentElement, "requestFullscreen", { configurable: true, value: requestFullscreen });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("FullScreenToggle", () => {
  it("puts the whole app in full screen, then offers the way back in the same place", async () => {
    render(<FullScreenToggle />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "全屏" }));
    });
    expect(requestFullscreen).toHaveBeenCalledTimes(1);

    const back = await screen.findByRole("button", { name: "退出全屏" });
    await act(async () => {
      fireEvent.click(back);
    });
    expect(exitFullscreen).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("button", { name: "全屏" })).toBeInTheDocument();
  });

  it("follows full screen ended elsewhere (Esc)", async () => {
    fullscreenElement = document.documentElement;
    render(<FullScreenToggle />);
    expect(screen.getByRole("button", { name: "退出全屏" })).toBeInTheDocument();

    act(() => {
      fullscreenElement = null;
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    expect(screen.getByRole("button", { name: "全屏" })).toBeInTheDocument();
  });

  it("is not shown where the browser offers no page full screen", () => {
    Object.defineProperty(document, "fullscreenEnabled", { configurable: true, get: () => false });
    const { container } = render(<FullScreenToggle />);
    expect(container).toBeEmptyDOMElement();
  });
});
