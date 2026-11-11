import { describe, expect, it } from "vitest";
import { promptSummary } from "./version-summary";

describe("promptSummary", () => {
  it("skips the shared style and reference header lines", () => {
    expect(promptSummary("Style: 油画三渲二画风\nVisual style: 暗调\n\nA woman opens the door in the rain")).toBe(
      "A woman opens the door in the rain",
    );
  });

  it("falls back to the first line when there is nothing else", () => {
    expect(promptSummary("Style: 油画")).toBe("Style: 油画");
    expect(promptSummary(undefined)).toBeNull();
  });
});
