import { fireEvent, render, renderHook } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  findClashes,
  formatBinding,
  HOTKEYS_STORAGE_KEY,
  recordKey,
  resetHotkeyBinding,
  setHotkeyBinding,
  useHotkeyBinding,
  useHotkeys,
  type HotkeyHandlers,
} from "./hotkeys";

const noMods = { metaKey: false, ctrlKey: false, altKey: false, shiftKey: false };

function Storyboard({ handlers }: { handlers: HotkeyHandlers }) {
  const rootRef = useRef<HTMLDivElement>(null);
  useHotkeys("storyboard", { rootRef, handlers });
  return (
    <div ref={rootRef}>
      <button type="button">shot</button>
      <textarea aria-label="prompt" />
    </div>
  );
}

describe("recordKey", () => {
  it("turns a key press into a binding with its modifiers", () => {
    expect(recordKey({ ...noMods, key: "L" })).toEqual({ kind: "binding", binding: { key: "l" } });
    expect(recordKey({ ...noMods, key: "s", ctrlKey: true, shiftKey: true })).toEqual({
      kind: "binding",
      binding: { key: "s", mod: true, shift: true },
    });
  });

  it("waits on a modifier alone, stops on Esc and refuses keys the widgets use", () => {
    expect(recordKey({ ...noMods, key: "Shift", shiftKey: true })).toEqual({ kind: "pending" });
    expect(recordKey({ ...noMods, key: "Escape" })).toEqual({ kind: "cancel" });
    expect(recordKey({ ...noMods, key: "Enter" })).toEqual({ kind: "reserved" });
    expect(recordKey({ ...noMods, key: "ArrowLeft" })).toEqual({ kind: "reserved" });
    expect(recordKey({ ...noMods, key: "ArrowLeft", altKey: true })).toEqual({
      kind: "binding",
      binding: { key: "ArrowLeft", alt: true },
    });
  });
});

describe("formatBinding", () => {
  it("shows ⌘ on Apple and Ctrl elsewhere", () => {
    expect(formatBinding({ key: "s", mod: true }, true)).toEqual(["⌘", "S"]);
    expect(formatBinding({ key: "s", mod: true }, false)).toEqual(["Ctrl", "S"]);
    expect(formatBinding({ key: " " }, false)).toEqual(["Space"]);
  });
});

describe("per-device bindings", () => {
  it("stores only changed bindings and drops one set back to its default", () => {
    const { result } = renderHook(() => useHotkeyBinding("storyboard.next_shot"));
    expect(result.current).toEqual({ key: "k" });

    setHotkeyBinding("storyboard.next_shot", { key: "l" });
    expect(JSON.parse(window.localStorage.getItem(HOTKEYS_STORAGE_KEY) ?? "{}")).toEqual({
      "storyboard.next_shot": { key: "l" },
    });

    setHotkeyBinding("storyboard.next_shot", { key: "k" });
    expect(window.localStorage.getItem(HOTKEYS_STORAGE_KEY)).toBeNull();
  });

  it("reports a clash within the same scope", () => {
    expect(findClashes("storyboard.next_shot")).toEqual([]);
    setHotkeyBinding("storyboard.next_shot", { key: "j" });
    expect(findClashes("storyboard.next_shot")).toEqual(["storyboard.prev_shot"]);
    expect(findClashes("storyboard.prev_shot")).toEqual(["storyboard.next_shot"]);
    resetHotkeyBinding("storyboard.next_shot");
    expect(findClashes("storyboard.prev_shot")).toEqual([]);
  });
});

describe("useHotkeys", () => {
  it("fires the action on its custom key and no longer on the default", () => {
    setHotkeyBinding("storyboard.next_shot", { key: "l" });
    const next = vi.fn();
    render(<Storyboard handlers={{ "storyboard.next_shot": next }} />);

    fireEvent.keyDown(document.body, { key: "k" });
    expect(next).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(document.body, { key: "l" })).toBe(false);
    expect(next).toHaveBeenCalledTimes(1);
  });

  it("an action without a handler leaves its key alone", () => {
    render(<Storyboard handlers={{ "storyboard.next_shot": undefined }} />);
    expect(fireEvent.keyDown(document.body, { key: "k" })).toBe(true);
  });

  it("ignores keys another handler took and keys with extra modifiers", () => {
    const next = vi.fn();
    render(<Storyboard handlers={{ "storyboard.next_shot": next }} />);
    const taken = new KeyboardEvent("keydown", { key: "k", bubbles: true, cancelable: true });
    taken.preventDefault();
    document.body.dispatchEvent(taken);
    fireEvent.keyDown(document.body, { key: "k", shiftKey: true });
    fireEvent.keyDown(document.body, { key: "k", altKey: true });
    expect(next).not.toHaveBeenCalled();
  });

  it("a save rebound to a plain key is typed, not run, inside a text field", () => {
    setHotkeyBinding("storyboard.save_shot", { key: "q" });
    const save = vi.fn();
    const { getByLabelText } = render(<Storyboard handlers={{ "storyboard.save_shot": save }} />);

    expect(fireEvent.keyDown(getByLabelText("prompt"), { key: "q" })).toBe(true);
    expect(save).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: "q" });
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("outside the scope the save key does nothing but still blocks the browser's save", () => {
    const save = vi.fn();
    render(<Storyboard handlers={{ "storyboard.save_shot": save }} />);
    const outside = document.createElement("input");
    document.body.append(outside);
    try {
      expect(fireEvent.keyDown(outside, { key: "s", ctrlKey: true })).toBe(false);
      expect(save).not.toHaveBeenCalled();
    } finally {
      outside.remove();
    }
  });
});
