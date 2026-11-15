import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { bindingFor, HOTKEYS_STORAGE_KEY, setHotkeyBinding } from "@/preferences/hotkeys";
import { useAppStore } from "@/stores/app-store";

import { HotkeysSection } from "./HotkeysSection";

function row(action: string) {
  return screen.getByRole("group", { name: action });
}

describe("HotkeysSection", () => {
  beforeEach(() => {
    useAppStore.setState({ toast: null });
  });

  it("按「修改」后按下新键即改为该键，存在本设备", async () => {
    const user = userEvent.setup();
    render(<HotkeysSection />);

    await user.click(screen.getByRole("button", { name: "修改「下一个分镜」的按键" }));
    expect(within(row("下一个分镜")).getByRole("status")).toHaveTextContent("按下新的组合键");
    fireEvent.keyDown(document.body, { key: "l" });

    expect(within(row("下一个分镜")).getByText("L")).toBeInTheDocument();
    expect(bindingFor("storyboard.next_shot")).toEqual({ key: "l" });
    expect(window.localStorage.getItem(HOTKEYS_STORAGE_KEY)).not.toBeNull();
  });

  it("录制中按 Esc 或「取消」不做修改", async () => {
    const user = userEvent.setup();
    render(<HotkeysSection />);

    await user.click(screen.getByRole("button", { name: "修改「下一个分镜」的按键" }));
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.getByRole("button", { name: "修改「下一个分镜」的按键" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "修改「下一个分镜」的按键" }));
    await user.click(within(row("下一个分镜")).getByRole("button", { name: "取消" }));

    expect(bindingFor("storyboard.next_shot")).toEqual({ key: "k" });
  });

  it("控件占用的键（如 Enter）不接受，提示加修饰键并继续等待", async () => {
    const user = userEvent.setup();
    render(<HotkeysSection />);

    await user.click(screen.getByRole("button", { name: "修改「保存分镜」的按键" }));
    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(within(row("保存分镜")).getByRole("status")).toHaveTextContent("请加上 ⌘ / Ctrl 或 Alt");
    fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
    expect(bindingFor("storyboard.save_shot")).toEqual({ key: "Enter", mod: true });
  });

  it("同一范围内两个操作同键时双方都提示冲突", () => {
    setHotkeyBinding("storyboard.next_shot", { key: "j" });
    render(<HotkeysSection />);

    expect(within(row("下一个分镜")).getByText("同时用于：上一个分镜")).toBeInTheDocument();
    expect(within(row("上一个分镜")).getByText("同时用于：下一个分镜")).toBeInTheDocument();
  });

  it("单项恢复默认只重置该操作", async () => {
    setHotkeyBinding("storyboard.next_shot", { key: "l" });
    setHotkeyBinding("storyboard.prev_shot", { key: "h" });
    const user = userEvent.setup();
    render(<HotkeysSection />);

    await user.click(screen.getByRole("button", { name: "将「下一个分镜」恢复为默认按键" }));

    expect(bindingFor("storyboard.next_shot")).toEqual({ key: "k" });
    expect(bindingFor("storyboard.prev_shot")).toEqual({ key: "h" });
    expect(screen.queryByRole("button", { name: "将「下一个分镜」恢复为默认按键" })).not.toBeInTheDocument();
  });

  it("全部恢复默认后可在提示里撤销", async () => {
    setHotkeyBinding("storyboard.next_shot", { key: "l" });
    const user = userEvent.setup();
    render(<HotkeysSection />);

    await user.click(screen.getByRole("button", { name: "全部恢复默认" }));
    expect(bindingFor("storyboard.next_shot")).toEqual({ key: "k" });
    expect(screen.getByRole("button", { name: "全部恢复默认" })).toBeDisabled();

    act(() => useAppStore.getState().toast?.action?.onClick());
    expect(bindingFor("storyboard.next_shot")).toEqual({ key: "l" });
  });

  it("列出固定按键，并提示平板只有接上键盘时快捷键才起作用", () => {
    render(<HotkeysSection />);

    expect(screen.getByRole("heading", { name: "固定按键" })).toBeInTheDocument();
    expect(screen.getByText("关闭对话框、菜单与 Agent 面板")).toBeInTheDocument();
    expect(screen.getByText(/只有接上键盘时快捷键才起作用/)).toBeInTheDocument();
  });
});
