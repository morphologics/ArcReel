import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useRenderReadyToasts } from "@/hooks/useRenderReadyToasts";
import { useAppStore } from "@/stores/app-store";
import { useTasksStore } from "@/stores/tasks-store";
import { makeTask } from "@/test/factories";

function Harness() {
  useRenderReadyToasts("demo");
  return null;
}

beforeEach(() => {
  useAppStore.setState(useAppStore.getInitialState(), true);
  useTasksStore.setState({ tasks: [], connected: true });
});

describe("useRenderReadyToasts", () => {
  it("toasts when a render it saw running succeeds, not for renders already done on load", () => {
    useTasksStore.setState({
      tasks: [makeTask({ task_id: "old", project_name: "demo", task_type: "render_final_cut", status: "succeeded" })],
    });
    render(<Harness />);
    expect(useAppStore.getState().toast).toBeNull();

    act(() =>
      useTasksStore.setState({
        tasks: [makeTask({ task_id: "r1", project_name: "demo", task_type: "render_final_cut", status: "running" })],
      }),
    );
    expect(useAppStore.getState().toast).toBeNull();

    act(() =>
      useTasksStore.setState({
        tasks: [makeTask({ task_id: "r1", project_name: "demo", task_type: "render_final_cut", status: "succeeded" })],
      }),
    );
    expect(useAppStore.getState().toast).toMatchObject({ tone: "success" });
  });
});
