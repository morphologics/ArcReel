import { fireEvent, render, screen } from "@testing-library/react";
import { ContextMenu } from "@base-ui/react/context-menu";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { DropdownMenuContent } from "@/components/ui/dropdown-menu";
import { StoryboardGenerateItems, VideoRegenerateItems } from "./VideoRegenerateItems";

// the model list is loaded once per page: one fixture for every test
const CANDIDATES = {
  image: { default: ["runware/seedream", "runware/flux"], buckets: { i2i: ["runware/seedream"] } },
  video: { default: [], buckets: { i2v: ["runware/minimax", "runware/seedance"] } },
  provider_names: { runware: "Runware" },
  model_names: {
    "runware/minimax": "MiniMax H3",
    "runware/seedance": "Seedance",
    "runware/seedream": "Seedream 4.5",
    "runware/flux": "FLUX",
  },
};

describe("VideoRegenerateItems", () => {
  it("names the configured model with its cost and regenerates with it", async () => {
    vi.spyOn(API, "getModelCandidates").mockResolvedValue(CANDIDATES as never);
    vi.spyOn(API, "quoteVideoModels").mockResolvedValue({
      current: "runware/minimax",
      quotes: {
        "runware/minimax": { cost: { amount: 0.5, currency: "USD", unpriced_reason: null }, duration_supported: true, supported_durations: [6] },
        "runware/seedance": { cost: { amount: 0.3, currency: "USD", unpriced_reason: null }, duration_supported: false, supported_durations: [5, 10] },
      },
    });
    const onGenerate = vi.fn();
    render(
      <ContextMenu.Root open>
        <ContextMenu.Trigger render={<div />}>video</ContextMenu.Trigger>
        <DropdownMenuContent>
          <VideoRegenerateItems
            projectName="demo"
            segmentId="E1S01"
            scriptFile="episode_1.json"
            durationSeconds={6}
            hasVideo
            disabled={false}
            onGenerate={onGenerate}
          />
        </DropdownMenuContent>
      </ContextMenu.Root>,
    );

    const item = await screen.findByRole("menuitem", { name: /MiniMax H3.*0\.50/ });
    fireEvent.click(item);
    expect(onGenerate).toHaveBeenCalledWith(null);
    expect(API.quoteVideoModels).toHaveBeenCalledWith(
      "demo",
      "E1S01",
      { script_file: "episode_1.json", duration_seconds: 6, models: ["runware/minimax", "runware/seedance"] },
      expect.anything(),
    );
  });

  it("generates the storyboard with the configured image model, priced for the shot's lane", async () => {
    vi.spyOn(API, "getModelCandidates").mockResolvedValue(CANDIDATES as never);
    vi.spyOn(API, "quoteStoryboardModels").mockResolvedValue({
      current: "runware/seedream",
      lane: "i2i",
      quotes: {
        "runware/seedream": { cost: { amount: 0.04, currency: "USD" } },
        "runware/flux": { unavailable: "image_bucket_capability" },
      },
    });
    const onGenerate = vi.fn();
    render(
      <ContextMenu.Root open>
        <ContextMenu.Trigger render={<div />}>image</ContextMenu.Trigger>
        <DropdownMenuContent>
          <StoryboardGenerateItems
            projectName="demo"
            segmentId="E1S01"
            scriptFile="episode_1.json"
            hasImage={false}
            disabled={false}
            onGenerate={onGenerate}
          />
        </DropdownMenuContent>
      </ContextMenu.Root>,
    );

    fireEvent.click(await screen.findByRole("menuitem", { name: /生成分镜.*Seedream 4\.5.*0\.04/ }));
    expect(onGenerate).toHaveBeenCalledWith(null);
  });

  it("flat (the Generate button's ▾): only the other models, no repeat of the configured one", async () => {
    vi.spyOn(API, "getModelCandidates").mockResolvedValue(CANDIDATES as never);
    vi.spyOn(API, "quoteVideoModels").mockResolvedValue({
      current: "runware/minimax",
      quotes: {
        "runware/minimax": { cost: { amount: 0.5, currency: "USD", unpriced_reason: null }, duration_supported: true, supported_durations: [6] },
        "runware/seedance": { cost: { amount: 0.3, currency: "USD", unpriced_reason: null }, duration_supported: true, supported_durations: [6] },
      },
    });
    const onGenerate = vi.fn();
    render(
      <ContextMenu.Root open>
        <ContextMenu.Trigger render={<div />}>video</ContextMenu.Trigger>
        <DropdownMenuContent>
          <VideoRegenerateItems
            projectName="demo"
            segmentId="E1S01"
            scriptFile="episode_1.json"
            durationSeconds={6}
            hasVideo
            disabled={false}
            onGenerate={onGenerate}
            flat
          />
        </DropdownMenuContent>
      </ContextMenu.Root>,
    );

    fireEvent.click(await screen.findByRole("menuitem", { name: /Seedance.*0\.30/ }));
    expect(onGenerate).toHaveBeenCalledWith("runware/seedance");
    expect(screen.queryByRole("menuitem", { name: /MiniMax H3/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  });
});
