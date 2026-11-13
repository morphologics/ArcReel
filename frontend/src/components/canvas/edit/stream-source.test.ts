import { describe, expect, it } from "vitest";
import { bufferedAhead, nextPiece } from "./stream-source";

function videoWith(ranges: [number, number][]): HTMLVideoElement {
  const buffered = {
    length: ranges.length,
    start: (i: number) => ranges[i][0],
    end: (i: number) => ranges[i][1],
  };
  return { buffered } as unknown as HTMLVideoElement;
}

describe("bufferedAhead", () => {
  it("measures the continuous picture from a time to the end of its buffered range", () => {
    const video = videoWith([
      [0, 12],
      [30, 50],
    ]);
    expect(bufferedAhead(video, 4)).toBe(8);
    expect(bufferedAhead(video, 35)).toBe(15);
    expect(bufferedAhead(video, 20)).toBe(0);
    expect(bufferedAhead(video, 50)).toBe(0);
  });

  it("tolerates a seek landing a hair before a range (frame boundary in floating point)", () => {
    expect(bufferedAhead(videoWith([[10, 20]]), 9.99)).toBeCloseTo(10.01);
  });
});

describe("nextPiece", () => {
  const pieces = [0, 4, 9, 12].map((start, index, starts) => ({
    start,
    duration: (starts[index + 1] ?? 15) - start,
    url: `/p${index}.mp4`,
  }));

  it("takes the missing piece at the playhead, then onwards, then the ones before it", () => {
    expect(nextPiece(pieces, new Set(), 10)).toBe(2);
    expect(nextPiece(pieces, new Set([2]), 10)).toBe(3);
    expect(nextPiece(pieces, new Set([2, 3]), 10)).toBe(0);
    expect(nextPiece(pieces, new Set([0, 2, 3]), 10)).toBe(1);
    expect(nextPiece(pieces, new Set([0, 1, 2, 3]), 10)).toBe(-1);
  });

  it("starts at the first piece from the beginning", () => {
    expect(nextPiece(pieces, new Set(), 0)).toBe(0);
  });
});
