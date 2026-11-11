import { fireEvent, render, screen } from "@testing-library/react";
import { ContextMenu } from "@base-ui/react/context-menu";
import { describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { DropdownMenuContent } from "@/components/ui/dropdown-menu";
import { VideoRegenerateItems } from "./VideoRegenerateItems";

describe("VideoRegenerateItems", () => {
  it("names the configured model with its cost and regenerates with it", async () => {
    vi.spyOn(API, "getModelCandidates").mockResolvedValue({
      image: { default: [], buckets: {} },
      video: { default: [], buckets: { i2v: ["runware/minimax", "runware/seedance"] } },
      provider_names: { runware: "Runware" },
      model_names: { "runware/minimax": "MiniMax H3", "runware/seedance": "Seedance" },
    });
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
});
