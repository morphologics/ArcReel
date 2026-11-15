import { describe, expect, it } from "vitest";
import { resizedSize } from "./image-resize";

describe("resizedSize", () => {
  it.each([
    // The A4 scan sample from the official docs: both sides within 1568 px, but the visual tokens exceed the budget.
    // 官方文档的 A4 扫描件样例：两边都在 1568 px 内，但视觉 token 超预算。
    { label: "A4 scan", input: [1075, 1520], expected: [924, 1307] },
    // The long-side limit seems to allow 1568x882, but the token budget actually decides.
    // 长边限制看似给到 1568×882，实际由 token 预算决定。
    { label: "1080p screenshot", input: [1920, 1080], expected: [1456, 819] },
    // A tall, narrow phone screenshot: only here does the side-length limit take over.
    // 细长的手机截屏：这里才轮到边长限制收口。
    { label: "phone screenshot", input: [1179, 2556], expected: [723, 1568] },
  ])("matches the documented sample for a $label", ({ input, expected }) => {
    expect(resizedSize(input[0], input[1])).toEqual(expected);
  });

  it.each([
    [800, 600],
    [1568, 28],
    [1, 1],
  ])("returns %ix%i unchanged when it already fits both limits", (width, height) => {
    expect(resizedSize(width, height)).toEqual([width, height]);
  });

  it("keeps every resized size within the edge and token limits", () => {
    const [width, height] = resizedSize(4000, 3000);

    expect(Math.max(width, height)).toBeLessThanOrEqual(1568);
    expect(Math.ceil(width / 28) * Math.ceil(height / 28)).toBeLessThanOrEqual(1568);
  });

  it("honours the high-resolution tier limits when they are passed", () => {
    expect(resizedSize(1075, 1520, 2576, 4784)).toEqual([1075, 1520]);
  });
});
