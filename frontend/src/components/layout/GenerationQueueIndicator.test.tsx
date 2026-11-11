import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useTasksStore } from "@/stores/tasks-store";
import { makeTask } from "@/test/factories";
import { GenerationQueueIndicator } from "./GenerationQueueIndicator";

const now = new Date().toISOString();
const ref = { episode_title: "雨夜", episode_position: 1, item_id: "S03", item_position: 3, item_excerpt: "门开了", item_thumbnail: null };

beforeEach(() => {
  vi.restoreAllMocks();
  useProjectsStore.setState({
    currentProjectName: "demo",
    currentProjectData: { episodes: [{ episode: 1, title: "雨夜", script_file: "scripts/episode_1.json" }] } as never,
  });
});

function task(id: string, task_type: string, status: "running" | "queued" | "failed") {
  return makeTask({
    task_id: id,
    project_name: "demo",
    task_type,
    resource_id: "E1S03",
    resource_ref: ref,
    script_file: "episode_1.json",
    status,
    queued_at: now,
    started_at: status === "queued" ? null : now,
    finished_at: status === "failed" ? now : null,
    updated_at: now,
  });
}

describe("GenerationQueueIndicator", () => {
  it("shows only its own queue's tasks, counts running/active, cancels queued, opens the shot", () => {
    useTasksStore.setState({
      tasks: [task("a", "video", "running"), task("b", "video", "queued"), task("c", "storyboard", "failed")],
    });
    const cancel = vi.spyOn(API, "cancelTask").mockResolvedValue({ cancelled: [], skipped_terminal: [] });
    const onNavigate = vi.fn();
    render(<GenerationQueueIndicator kind="videos" onNavigate={onNavigate} />);

    const trigger = screen.getByRole("button", { name: "视频" });
    expect(trigger).toHaveTextContent("1/2");
    fireEvent.click(trigger);
    expect(screen.getAllByText(/雨夜 · #3 门开了/)).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(cancel).toHaveBeenCalledWith("b");

    fireEvent.click(screen.getAllByText(/雨夜 · #3/)[0]);
    expect(onNavigate).toHaveBeenCalledWith(expect.objectContaining({ type: "segment", id: "E1S03", route: "/episodes/1" }));
  });

  it("is hidden when its queue is empty", () => {
    useTasksStore.setState({ tasks: [task("a", "video", "running")] });
    const { container } = render(<GenerationQueueIndicator kind="storyboards" onNavigate={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("cancels a queued item from its right-click menu", async () => {
    useTasksStore.setState({ tasks: [task("q", "video", "queued")] });
    const cancel = vi.spyOn(API, "cancelTask").mockResolvedValue({ cancelled: [], skipped_terminal: [] });
    render(<GenerationQueueIndicator kind="videos" onNavigate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "视频" }));

    fireEvent.contextMenu(screen.getByText(/雨夜 · #3/));
    fireEvent.click(await screen.findByRole("menuitem", { name: /取消生成/ }));
    expect(cancel).toHaveBeenCalledWith("q");
  });
});
