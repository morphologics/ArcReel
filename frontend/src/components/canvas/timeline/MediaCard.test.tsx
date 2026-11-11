import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useShotMediaStore } from "@/stores/shot-media-store";
import { DEMO_PROJECT_NAME } from "@/onboarding/demo-project";
import { useProjectsStore } from "@/stores/projects-store";
import { MediaCard } from "./MediaCard";

function renderCard(props: Partial<Parameters<typeof MediaCard>[0]> = {}) {
  return render(
    <MediaCard
      kind="storyboard"
      projectName="demo"
      segmentId="E1S01"
      assetPath={null}
      aspectRatio="9:16"
      {...props}
    />,
  );
}

describe("MediaCard storyboard image", () => {
  afterEach(() => useProjectsStore.setState({ assetFingerprints: {} }));

  it("offers 640 and 1280 thumbnails to the card and keeps the original for the full-screen preview", () => {
    useProjectsStore.setState({ assetFingerprints: { "storyboards/E1S01.png": 5 } });
    renderCard({ assetPath: "storyboards/E1S01.png" });

    const card = screen.getByRole("img", { name: /分镜图$/ });
    expect(card).toHaveAttribute(
      "srcset",
      "/api/v1/files/demo/storyboards/E1S01.png?v=5&w=640 640w, /api/v1/files/demo/storyboards/E1S01.png?v=5&w=1280 1280w",
    );
    expect(card).toHaveAttribute("src", "/api/v1/files/demo/storyboards/E1S01.png?v=5");

    fireEvent.click(screen.getByRole("button", { name: /分镜图」的大图$/ }));
    const preview = screen.getAllByRole("img", { name: /分镜图$/ }).find((img) => !img.hasAttribute("srcset"));
    expect(preview).toHaveAttribute("src", "/api/v1/files/demo/storyboards/E1S01.png?v=5");
  });
});

describe("MediaCard upload", () => {
  it("invokes onUpload with the selected file", () => {
    const onUpload = vi.fn();
    const { container } = renderCard({ onUpload });
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    const file = new File(["x"], "board.png", { type: "image/png" });
    fireEvent.change(input!, { target: { files: [file] } });
    expect(onUpload).toHaveBeenCalledWith(file);
  });

  it("hides upload entry when onUpload is not provided", () => {
    const { container } = renderCard();
    expect(container.querySelector('input[type="file"]')).toBeNull();
  });

  it("disables upload button while generating", () => {
    renderCard({ onUpload: vi.fn(), generating: true });
    expect(screen.getByRole("button", { name: "上传分镜图" })).toBeDisabled();
  });

  it("disables upload button when a sibling upload is in flight (uploadDisabled)", () => {
    renderCard({ onUpload: vi.fn(), uploadDisabled: true });
    expect(screen.getByRole("button", { name: "上传分镜图" })).toBeDisabled();
  });

  it("accepts video formats for the video card", () => {
    const { container } = renderCard({ kind: "video", onUpload: vi.fn() });
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input?.accept).toContain(".mp4");
  });

  it("disables the generate CTA when a sibling upload is in flight (uploadDisabled)", () => {
    const { getByRole } = renderCard({ onGenerate: vi.fn(), uploadDisabled: true });
    expect(getByRole("button", { name: /生成分镜/ })).toBeDisabled();
  });
});

describe("MediaCard in the demo workbench", () => {
  afterEach(() => {
    useProjectsStore.setState(useProjectsStore.getInitialState(), true);
  });

  // 四个入口的回调/开关全部给足：只有演示态判定本身能把它们关掉
  function renderCardWithAllEntries() {
    return renderCard({
      assetPath: "storyboards/E1S01_v1.png",
      onUpload: vi.fn(),
      onRestore: vi.fn(),
      onGenerate: vi.fn(),
      editScriptFile: "episode_1.json",
    });
  }

  it("hides the write entries and the version entry from the same judgement", () => {
    useProjectsStore.setState({ currentProjectName: DEMO_PROJECT_NAME });

    const { container, queryByRole } = renderCardWithAllEntries();

    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(queryByRole("button", { name: /版本/ })).not.toBeInTheDocument();
    expect(queryByRole("button", { name: /编辑/ })).not.toBeInTheDocument();
    expect(queryByRole("button", { name: /重新生成分镜/ })).not.toBeInTheDocument();
  });

  it("keeps all four entries outside the demo workbench", () => {
    useProjectsStore.setState({ currentProjectName: "demo" });

    const { container, getByRole } = renderCardWithAllEntries();

    expect(container.querySelector('input[type="file"]')).not.toBeNull();
    expect(getByRole("button", { name: /版本/ })).toBeInTheDocument();
    expect(getByRole("button", { name: /局部修改/ })).toBeInTheDocument();
    expect(getByRole("button", { name: /重新生成分镜/ })).toBeInTheDocument();
  });
});

describe("MediaCard version history", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    useShotMediaStore.setState({ mode: { storyboard: "preview", video: "preview" }, viewing: { storyboard: null, video: null } });
  });

  it("lists the versions in History mode and previews a picked one with Make current", async () => {
    useProjectsStore.setState({ currentProjectName: "demo" });
    vi.spyOn(API, "getVersions").mockResolvedValue({
      resource_type: "storyboards",
      resource_id: "E1S01",
      current_version: 2,
      versions: [
        { version: 1, filename: "a", created_at: "2026-11-10T06:02:00Z", file_size: 2048, is_current: false, file_url: "/v1.png",
          generation: { provider: "runware", model: "seedream", cost_amount: 0.04, currency: "USD" } },
        { version: 2, filename: "b", created_at: "2026-11-11T06:02:00Z", file_size: 2048, is_current: true, file_url: "/v2.png", source: "manual_upload" },
      ],
    } as never);
    const restore = vi.spyOn(API, "restoreVersion").mockResolvedValue({ success: true } as never);
    renderCard({ assetPath: "storyboards/E1S01.png", onRestore: vi.fn() });

    fireEvent.click(screen.getByRole("button", { name: "版本历史" }));
    expect(await screen.findByText(/seedream/)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.04/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "预览 v1" }));
    expect(await screen.findByText("正在查看 v1")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /v1/ })).toHaveAttribute("src", "/v1.png");

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "设为当前" })));
    expect(restore).toHaveBeenCalledWith("demo", "storyboards", "E1S01", 1);
  });
});

describe("MediaCard storyboard / video tabs", () => {
  afterEach(() => useShotMediaStore.setState({ tab: "storyboard" }));

  it("switches to the other pane from the title and from the view icons", () => {
    renderCard();
    fireEvent.click(screen.getByTitle("切换到视频"));
    expect(useShotMediaStore.getState().tab).toBe("video");
    useShotMediaStore.setState({ tab: "storyboard" });
    fireEvent.click(screen.getByRole("button", { name: "视频" }));
    expect(useShotMediaStore.getState().tab).toBe("video");
  });
});
