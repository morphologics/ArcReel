import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadBlob, safeFileName, saveDownloadAs } from "./download";

describe("downloadBlob", () => {
  it("defers URL revocation until the browser has started consuming the download", async () => {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:download"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    vi.useFakeTimers();
    try {
      downloadBlob(new Blob(["zip"]), "presentation.zip");

      expect(URL.revokeObjectURL).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(0);
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:download");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("safeFileName", () => {
  it("drops characters file systems reject and collapses whitespace", () => {
    expect(safeFileName('a/b:c*d?"e"<f>|g', "mp4")).toBe("a b c d e f g.mp4");
    expect(safeFileName("  extra   spaces  ", "mp4")).toBe("extra spaces.mp4");
  });

  it("falls back when nothing is left", () => {
    expect(safeFileName("///", "mp4", "final-cut")).toBe("final-cut.mp4");
  });
});

describe("saveDownloadAs", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("falls back to a plain download when the browser has no save dialog", async () => {
    const clicked: { href: string; download: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });
    await expect(saveDownloadAs("/api/v1/files/demo/x.mp4", "Demo - E1.mp4")).resolves.toBe(true);
    expect(clicked).toEqual([{ href: "/api/v1/files/demo/x.mp4", download: "Demo - E1.mp4" }]);
  });
});
