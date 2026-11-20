import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { API } from "@/api";
import type { ClientErrorReport } from "@/types/client-errors";

import { DiagnosticsSection } from "./DiagnosticsSection";

const REQUEST: ClientErrorReport = {
  id: 2,
  created_at: "2026-11-20T10:00:05Z",
  kind: "request",
  message: "HTTP 500 Internal Server Error",
  detail: { page: "/app/projects/demo", method: "POST", api_path: "/api/v1/projects/demo/edit-timelines", status: 500 },
  user_agent: "probe/1.0",
  app_build: "index-abc123",
};

const TOAST: ClientErrorReport = {
  id: 1,
  created_at: "2026-11-20T10:00:00Z",
  kind: "toast",
  message: "无法打开编辑器",
  detail: { page: "/app/projects/demo/edit", source: "toast", stack: "at openEditor (a.tsx:1:2)" },
  user_agent: null,
  app_build: null,
};

describe("DiagnosticsSection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists the reports newest first with kind, message and path; details expand", async () => {
    vi.spyOn(API, "listClientErrors").mockResolvedValue({ entries: [REQUEST, TOAST], kept: 1000 });
    const user = userEvent.setup();
    render(<DiagnosticsSection />);

    const list = await screen.findByRole("list", { name: "错误记录" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("请求失败");
    expect(rows[0]).toHaveTextContent("HTTP 500 Internal Server Error");
    expect(rows[0]).toHaveTextContent("POST /api/v1/projects/demo/edit-timelines · 500");
    expect(rows[1]).toHaveTextContent("错误提示");
    expect(rows[1]).toHaveTextContent("无法打开编辑器");

    await user.click(within(rows[1]).getByRole("button", { name: "详情" }));
    expect(rows[1]).toHaveTextContent("at openEditor (a.tsx:1:2)");
  });

  it("shows the empty state, and Refresh loads again", async () => {
    const list = vi
      .spyOn(API, "listClientErrors")
      .mockResolvedValueOnce({ entries: [], kept: 1000 })
      .mockResolvedValueOnce({ entries: [TOAST], kept: 1000 });
    const user = userEvent.setup();
    render(<DiagnosticsSection />);

    expect(await screen.findByText("暂无错误记录。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "清空" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText("无法打开编辑器")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("Clear asks first; Cancel keeps the reports, confirming deletes them", async () => {
    vi.spyOn(API, "listClientErrors").mockResolvedValue({ entries: [REQUEST, TOAST], kept: 1000 });
    const clear = vi.spyOn(API, "clearClientErrors").mockResolvedValue({ deleted: 2 });
    const user = userEvent.setup();
    render(<DiagnosticsSection />);
    await screen.findByRole("list", { name: "错误记录" });

    await user.click(screen.getByRole("button", { name: "清空" }));
    let dialog = await screen.findByRole("alertdialog", { name: "清空错误记录？" });
    expect(dialog).toHaveTextContent("将删除全部 2 条错误记录");
    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(clear).not.toHaveBeenCalled();
    expect(screen.getByRole("list", { name: "错误记录" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "清空" }));
    dialog = await screen.findByRole("alertdialog", { name: "清空错误记录？" });
    await user.click(within(dialog).getByRole("button", { name: "清空" }));
    expect(clear).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("暂无错误记录。")).toBeInTheDocument();
  });

  it("a failed load shows inline", async () => {
    vi.spyOn(API, "listClientErrors").mockRejectedValue(new Error("HTTP 502"));
    render(<DiagnosticsSection />);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载错误记录：HTTP 502");
  });
});
