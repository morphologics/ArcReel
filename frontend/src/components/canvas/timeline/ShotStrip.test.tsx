import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore } from "@/stores/projects-store";
import { makeNarrationSegment } from "@/test/factories";
import { ShotStrip } from "./ShotStrip";

const ep1 = [makeNarrationSegment({ segment_id: "E1S01" }), makeNarrationSegment({ segment_id: "E1S02" })];
const ep2 = [makeNarrationSegment({ segment_id: "E2S01" })];

function seed() {
  useProjectsStore.setState({
    currentProjectData: {
      episodes: [
        { episode: 1, title: "雨夜", script_file: "scripts/episode_1.json" },
        { episode: 2, title: "码头", script_file: "scripts/episode_2.json" },
        { episode: 3, title: "未写" },
      ],
    } as never,
    currentScripts: {
      "episode_1.json": { episode: 1, content_mode: "narration", segments: ep1 } as never,
      "episode_2.json": { episode: 2, content_mode: "narration", segments: ep2 } as never,
    },
  });
}

afterEach(() => {
  useProjectsStore.setState(useProjectsStore.getInitialState(), true);
  useAppStore.setState({ scrollTarget: null });
  window.history.replaceState(null, "", "/");
});

describe("ShotStrip", () => {
  it("shows every episode's shots in one strip, the current episode's selected shot marked", () => {
    seed();
    render(<ShotStrip segments={ep1} selectedIndex={1} onSelect={vi.fn()} contentMode="narration" projectName="demo" episode={1} />);

    const strip = screen.getByRole("navigation", { name: "分镜列表" });
    const groups = within(strip).getAllByRole("region");
    expect(groups).toHaveLength(3);
    expect(within(groups[0]).getAllByRole("button")).toHaveLength(2);
    expect(within(groups[1]).getAllByRole("button")).toHaveLength(1);
    expect(within(groups[2]).getByText("尚无剧本")).toBeInTheDocument();
    expect(within(groups[0]).getAllByRole("button")[1]).toHaveAttribute("aria-current", "true");
  });

  it("selects a shot of the current episode, and opens another episode with its shot selected", () => {
    seed();
    const onSelect = vi.fn();
    render(<ShotStrip segments={ep1} selectedIndex={0} onSelect={onSelect} contentMode="narration" projectName="demo" episode={1} />);
    const groups = within(screen.getByRole("navigation", { name: "分镜列表" })).getAllByRole("region");

    fireEvent.click(within(groups[0]).getAllByRole("button")[1]);
    expect(onSelect).toHaveBeenCalledWith(1);

    fireEvent.click(within(groups[1]).getAllByRole("button")[0]);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(window.location.pathname).toBe("/episodes/2");
    expect(useAppStore.getState().scrollTarget).toMatchObject({ type: "segment", id: "E2S01" });
  });

  it("shows only the current episode when it isn't in the project's episode list", () => {
    render(<ShotStrip segments={ep1} selectedIndex={0} onSelect={vi.fn()} contentMode="narration" projectName="demo" />);
    expect(within(screen.getByRole("navigation", { name: "分镜列表" })).getAllByRole("region")).toHaveLength(1);
  });
});
