import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";
import { makeTask } from "@/test/factories";
import * as renderDownloads from "@/components/canvas/edit-render/render-downloads";
import { RenderQueueIndicator } from "./RenderQueueIndicator";

const now = new Date().toISOString();

beforeEach(() => {
  vi.restoreAllMocks();
  useProjectsStore.setState({
    currentProjectName: "demo",
    currentProjectData: { episodes: [{ episode: 5, title: "雨夜" }] } as never,
  });
  vi.spyOn(API, "listEditTimelines").mockResolvedValue({
    timelines: [{ id: "tl1", name: "初剪", episode: 5 }] as never,
  });
});

describe("RenderQueueIndicator", () => {
  it("is hidden without renders", () => {
    useTasksStore.setState({ tasks: [makeTask({ project_name: "demo", task_type: "storyboard" })] });
    const { container } = render(<RenderQueueIndicator onOpenEpisode={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("counts running of active renders and lists them with their timeline; Cancel only while queued", async () => {
    const task = (id: string, status: "running" | "queued" | "succeeded") =>
      makeTask({
        task_id: id,
        project_name: "demo",
        task_type: "render_final_cut",
        status,
        payload: { timeline_id: "tl1", narration: "with_narration", subtitles: "burned_subtitles" },
        queued_at: now,
        started_at: status === "queued" ? null : now,
        finished_at: status === "succeeded" ? now : null,
        updated_at: now,
      });
    useTasksStore.setState({ tasks: [task("a", "running"), task("b", "queued"), task("c", "succeeded")] });
    const cancel = vi.spyOn(API, "cancelTask").mockResolvedValue({ cancelled: [], skipped_terminal: [] });
    const onOpen = vi.fn();
    render(<RenderQueueIndicator onOpenEpisode={onOpen} />);

    const trigger = screen.getByRole("button", { name: "出片队列" });
    expect(trigger).toHaveTextContent("1/2");
    fireEvent.click(trigger);

    expect((await screen.findAllByText(/初剪/)).length).toBe(3);
    const cancels = screen.getAllByRole("button", { name: "取消" });
    expect(cancels).toHaveLength(1);
    fireEvent.click(cancels[0]);
    expect(cancel).toHaveBeenCalledWith("b");

    fireEvent.click(screen.getAllByText(/初剪/)[0]);
    expect(onOpen).toHaveBeenCalledWith(5);
  });

  it("offers Download on the newest finished render of a version only", async () => {
    const done = (id: string, queuedAt: string) =>
      makeTask({
        task_id: id,
        project_name: "demo",
        task_type: "render_final_cut",
        resource_id: "tl1.with_narration.burned_subtitles",
        status: "succeeded",
        payload: { timeline_id: "tl1", narration: "with_narration", subtitles: "burned_subtitles" },
        queued_at: queuedAt,
        started_at: queuedAt,
        finished_at: now,
        updated_at: now,
      });
    useTasksStore.setState({ tasks: [done("old", "2026-01-01T00:00:00Z"), done("new", now)] });
    const download = vi.spyOn(renderDownloads, "downloadFinalCut").mockResolvedValue("started");
    render(<RenderQueueIndicator onOpenEpisode={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "出片队列" }));

    const buttons = await screen.findAllByRole("button", { name: "下载" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(download).toHaveBeenCalledWith(
      expect.objectContaining({ timelineId: "tl1", timelineName: "初剪", subtitles: "burned_subtitles", fileStem: "demo - E5" }),
    );
  });
});
