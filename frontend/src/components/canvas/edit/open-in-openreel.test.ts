import { afterEach, describe, expect, it } from "vitest";
import { STUDIO_TAB, exposeStudioNavigate } from "./open-in-openreel";

type NavigateWindow = Window & { youreelNavigate?: (url: string) => boolean };

describe("exposeStudioNavigate", () => {
  let undo: (() => void) | null = null;
  afterEach(() => {
    undo?.();
    undo = null;
    window.name = "";
    window.history.replaceState(null, "", "/");
  });

  it("names the tab and moves it in-app to a studio address", () => {
    undo = exposeStudioNavigate();
    expect(window.name).toBe(STUDIO_TAB);
    const go = (window as NavigateWindow).youreelNavigate;
    expect(go?.(`${window.location.origin}/app/projects/demo/episodes/3?shot=E3S2`)).toBe(true);
    expect(window.location.pathname).toBe("/app/projects/demo/episodes/3");
    expect(window.location.search).toBe("?shot=E3S2");
  });

  it("keeps a tab name that is already set", () => {
    window.name = "something-else";
    undo = exposeStudioNavigate();
    expect(window.name).toBe("something-else");
  });

  it("refuses other origins and paths outside the app", () => {
    undo = exposeStudioNavigate();
    const go = (window as NavigateWindow).youreelNavigate;
    expect(go?.("https://example.com/app/projects/x")).toBe(false);
    expect(go?.(`${window.location.origin}/api/v1/projects`)).toBe(false);
    expect(window.location.pathname).toBe("/");
  });

  it("is removed by its undo", () => {
    undo = exposeStudioNavigate();
    undo();
    undo = null;
    expect((window as NavigateWindow).youreelNavigate).toBeUndefined();
  });
});
