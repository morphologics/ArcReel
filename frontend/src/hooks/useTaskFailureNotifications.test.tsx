import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useTaskFailureNotifications } from "@/hooks/useTaskFailureNotifications";
import { useTasksStore } from "@/stores/tasks-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useAppStore } from "@/stores/app-store";
import type { ProjectData, TaskItem } from "@/types";

function Harness({ project }: { project: string }) {
  useTaskFailureNotifications(project);
  return null;
}

function task(overrides: Partial<TaskItem>): TaskItem {
  return {
    task_id: "t1",
    project_name: "demo",
    task_type: "storyboard",
    media_type: "image",
    resource_id: "E1S01",
    resource_type: null,
    script_file: "ep1.json",
    payload: {},
    status: "running",
    result: null,
    error_message: "boom",
    cancelled_by: null,
    provider_id: null,
    provider_job_id: null,
    source: "webui",
    queued_at: "",
    started_at: null,
    finished_at: null,
    updated_at: "",
    ...overrides,
  };
}

const PROJECT = {
  episodes: [{ episode: 1, title: "E1", script_file: "scripts/ep1.json" }],
} as unknown as ProjectData;

describe("useTaskFailureNotifications", () => {
  beforeEach(() => {
    useAppStore.setState(useAppStore.getInitialState(), true);
    // connected=true: simulate the first successful poll having completed so the baseline can be established.
    // connected=true：模拟首个成功 poll 已完成、基线可建立。
    useTasksStore.setState({ tasks: [], connected: true });
    useProjectsStore.setState({ currentProjectName: "demo", currentProjectData: PROJECT });
  });

  it("pushes one clickable notification when a task transitions to failed", async () => {
    useTasksStore.setState({ tasks: [task({ status: "running" })] });
    render(<Harness project="demo" />);

    act(() => {
      useTasksStore.setState({ tasks: [task({ status: "failed" })] });
    });

    await waitFor(() => {
      expect(useAppStore.getState().workspaceNotifications).toHaveLength(1);
    });
    const note = useAppStore.getState().workspaceNotifications[0];
    expect(note.tone).toBe("error");
    expect(note.target).toEqual({
      type: "segment",
      id: "E1S01",
      route: "/episodes/1",
      highlight_style: "flash",
    });
  });

  it("tells the user the result went to the trash when the target was deleted during generation", async () => {
    useTasksStore.setState({ tasks: [task({ status: "running" }), task({ task_id: "t2", status: "running" })] });
    render(<Harness project="demo" />);

    act(() => {
      useTasksStore.setState({
        tasks: [
          task({ status: "succeeded", result: { target_deleted: true, trash_record_id: "r1" } }),
          task({ task_id: "t2", status: "succeeded", result: { file_path: "storyboards/a.png" } }),
        ],
      });
    });

    await waitFor(() => {
      expect(useAppStore.getState().workspaceNotifications).toHaveLength(1);
    });
    const note = useAppStore.getState().workspaceNotifications[0];
    expect(note.tone).toBe("warning");
    expect(note.text).toContain("项目回收站");
  });

  it("does not notify for tasks already failed on first observation", () => {
    useTasksStore.setState({ tasks: [task({ status: "failed" })] });
    render(<Harness project="demo" />);
    expect(useAppStore.getState().workspaceNotifications).toHaveLength(0);
  });

  // With 3-second polling a task may fail outright between two polls, already failed when first observed by the frontend.
  // Such new tasks first appearing after the baseline is established should notify, or fast background failures would be missed.
  // 3 秒轮询下，任务可能在两次 poll 之间直接失败，首次被前端观测到时已是 failed。
  // 基线建立后才首次出现的这类新任务应通知，否则后台快速失败会被漏报。
  it("notifies for a brand-new task that appears already-failed after the baseline", async () => {
    // Baseline: the first successful poll already has one historical failed task (absorbed by the baseline, no notification)
    // 基线：首个成功 poll 已有一条历史 failed（被基线吸收，不通知）
    useTasksStore.setState({ tasks: [task({ task_id: "old", status: "failed" })] });
    render(<Harness project="demo" />);
    expect(useAppStore.getState().workspaceNotifications).toHaveLength(0);
    // Next poll: a new task_id appears directly as failed (fast failure between two polls)
    // 下一轮 poll：一个新 task_id 直接以 failed 出现（两次 poll 间快速失败）
    act(() => {
      useTasksStore.setState({
        tasks: [
          task({ task_id: "old", status: "failed" }),
          task({ task_id: "fast", status: "failed", resource_id: "E1S02" }),
        ],
      });
    });
    await waitFor(() => expect(useAppStore.getState().workspaceNotifications).toHaveLength(1));
    expect(useAppStore.getState().workspaceNotifications[0].target).toMatchObject({ id: "E1S02" });
  });

  it("ignores tasks from other projects", () => {
    useTasksStore.setState({ tasks: [task({ project_name: "other", status: "running" })] });
    render(<Harness project="demo" />);
    act(() => {
      useTasksStore.setState({ tasks: [task({ project_name: "other", status: "failed" })] });
    });
    expect(useAppStore.getState().workspaceNotifications).toHaveLength(0);
  });

  // Regression: the worker does not clean up failed tasks, so later polls keep bringing back the same failed record;
  // it should notify only once (only on the non-failed -> failed transition).
  // 回归：worker 不清理失败任务，后续 poll 会反复带回同一条 failed 记录，
  // 只应通知一次（仅在 non-failed → failed 转变时）。
  it("notifies only once for the same failed task across repeated updates", async () => {
    useTasksStore.setState({ tasks: [task({ status: "running" })] });
    render(<Harness project="demo" />);
    act(() => {
      useTasksStore.setState({ tasks: [task({ status: "failed" })] });
    });
    await waitFor(() => expect(useAppStore.getState().workspaceNotifications).toHaveLength(1));
    // The same failed task reappearing in the next poll should not notify again
    // 同一 failed 任务在下一轮 poll 再次出现，不应再通知
    act(() => {
      useTasksStore.setState({ tasks: [task({ status: "failed" })] });
    });
    expect(useAppStore.getState().workspaceNotifications).toHaveLength(1);
  });

  // Regression: switching projects (A->B->A) simulates the user leaving and returning to the same project. useTasksSSE's real timing is
  // cleanup setConnected(false) -> new poll -> setTasks(new project) + setConnected(true).
  // After switching back to A, historical failures already notified via transition should not be pushed again as fresh failures.
  // 回归：切换项目（A→B→A）模拟用户离开再回到同一项目。useTasksSSE 的真实时序是
  // cleanup setConnected(false) → 新 poll → setTasks(新项目) + setConnected(true)。
  // 切回 A 后已通过 transition 通知过的历史 failed 不应被当作 fresh failure 再推一遍。
  it("does not re-notify historical failed tasks when leaving and returning to the project", async () => {
    useTasksStore.setState({ tasks: [task({ status: "running" })], connected: true });
    const { rerender } = render(<Harness project="demo" />);
    act(() => {
      useTasksStore.setState({ tasks: [task({ status: "failed" })] });
    });
    await waitFor(() => expect(useAppStore.getState().workspaceNotifications).toHaveLength(1));

    // Switch to another project: projectName changes first, then useTasksSSE cleanup+setup simulates
    // setConnected(false) -> setTasks(empty) + setConnected(true)
    // 切到另一个项目：projectName 先变，然后 useTasksSSE cleanup+setup 模拟
    // setConnected(false) → setTasks(空) + setConnected(true)
    act(() => {
      rerender(<Harness project="other" />);
    });
    act(() => {
      useTasksStore.setState({ connected: false });
    });
    act(() => {
      useTasksStore.setState({ tasks: [], connected: true });
    });

    // Switch back to demo: same useTasksSSE timing, and tasks return to demo's (still containing the same failed one)
    // 切回 demo：同样的 useTasksSSE 时序，tasks 重新回到 demo 的（仍含同一条 failed）
    act(() => {
      rerender(<Harness project="demo" />);
    });
    act(() => {
      useTasksStore.setState({ connected: false });
    });
    act(() => {
      useTasksStore.setState({ tasks: [task({ status: "failed" })], connected: true });
    });

    expect(useAppStore.getState().workspaceNotifications).toHaveLength(1);
  });

  // Regression: switching to a new project, historical failures already in it should be absorbed by the baseline and not pushed.
  // 回归：切到一个新项目，新项目里已有的历史 failed 应被基线吸收、不推送。
  it("does not notify when switching to a project whose tasks are already failed", async () => {
    useTasksStore.setState({
      tasks: [task({ task_id: "demo-old", status: "succeeded" })],
      connected: true,
    });
    const { rerender } = render(<Harness project="demo" />);

    act(() => {
      rerender(<Harness project="other" />);
    });
    act(() => {
      useTasksStore.setState({ connected: false });
    });
    act(() => {
      useTasksStore.setState({
        tasks: [task({ task_id: "other-old", project_name: "other", status: "failed" })],
        connected: true,
      });
    });

    expect(useAppStore.getState().workspaceNotifications).toHaveLength(0);
  });

  // Regression: a project's first poll should establish the baseline even with no tasks, or when a later task fails fast between two polls
  // and is failed on first observation, seeded=false means it never reaches the isFreshFailure channel and is missed forever.
  // 回归：项目首轮 poll 即使无任务也应建立基线，否则后续 task 在两 poll 间快速失败、
  // 首次被观测即 failed 时，因 seeded=false 永远走不进 isFreshFailure 通道，永久漏报。
  it("notifies for fast failures in a project whose first poll had no tasks", async () => {
    useTasksStore.setState({ tasks: [], connected: true });
    render(<Harness project="demo" />);
    // There were no tasks before, but the first poll established the baseline. In a later poll a new task appears directly as failed.
    // 此前没有任何 task，但首轮 poll 已建立基线。后续 poll 一个新 task 直接以 failed 出现。
    act(() => {
      useTasksStore.setState({
        tasks: [task({ task_id: "fast", status: "failed", resource_id: "E1S03" })],
      });
    });
    await waitFor(() => expect(useAppStore.getState().workspaceNotifications).toHaveLength(1));
    expect(useAppStore.getState().workspaceNotifications[0].target).toMatchObject({ id: "E1S03" });
  });

  // Regression: switching projects while connected=false (e.g. network jitter + the user switching to another project); when the network
  // recovers the first successful poll must not be misjudged as a transitional commit, or it never seeds, and the next fast-failing
  // task would be missed forever.
  // 回归：connected=false 期间切换项目（如网络抖动 + 用户切到别的项目），随后网络
  // 恢复时第一个成功 poll 不能被误判为过渡 commit，否则永远不 seed，下一轮快速失败
  // 任务将被永久漏报。
  it("notifies for fast failures after switching project while disconnected", async () => {
    useTasksStore.setState({ tasks: [], connected: true });
    const { rerender } = render(<Harness project="demo" />);

    // Network down
    // 网络断
    act(() => {
      useTasksStore.setState({ connected: false });
    });
    // Switch project while offline (projectName changed, but the effect returns early because connected=false)
    // 断网期间切换项目（projectName 变了，但 effect 因 connected=false early return）
    act(() => {
      rerender(<Harness project="other" />);
    });
    // Network recovers; the first successful poll returns empty tasks (the other project has no tasks now)
    // 网络恢复，首个成功 poll 返回空 tasks（other 项目当前没任务）
    act(() => {
      useTasksStore.setState({ tasks: [], connected: true });
    });
    // Next poll: a new task fails fast between two polls and is failed on first observation
    // 下一轮 poll：一个新任务在两 poll 间快速失败，首次被观测即 failed
    act(() => {
      useTasksStore.setState({
        tasks: [task({ task_id: "fast-after-reconnect", project_name: "other", status: "failed", resource_id: "E1S04" })],
      });
    });
    await waitFor(() => expect(useAppStore.getState().workspaceNotifications).toHaveLength(1));
    expect(useAppStore.getState().workspaceNotifications[0].target).toMatchObject({ id: "E1S04" });
  });

  it("builds a reference_unit target for reference_video failures", async () => {
    useTasksStore.setState({
      tasks: [task({ task_id: "r1", task_type: "reference_video", resource_id: "E1U1", status: "running" })],
    });
    render(<Harness project="demo" />);
    act(() => {
      useTasksStore.setState({
        tasks: [task({ task_id: "r1", task_type: "reference_video", resource_id: "E1U1", status: "failed" })],
      });
    });
    await waitFor(() => {
      expect(useAppStore.getState().workspaceNotifications[0]?.target).toEqual({
        type: "reference_unit",
        id: "E1U1",
        route: "/episodes/1",
      });
    });
  });
});
