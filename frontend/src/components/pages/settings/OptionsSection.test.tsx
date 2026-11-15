import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API } from "@/api";
import { resetOpenReelProbeForTests } from "@/components/canvas/edit/open-in-openreel";
import i18n from "@/i18n";
import { CLIP_DRAG_STORAGE_KEY, EDITOR_OWN_TAB_STORAGE_KEY, useStudioOptionsStore } from "@/preferences/user-options";

import { OptionsSection } from "./OptionsSection";

function serveOpenReel(served: boolean) {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(served ? "<title>OpenReel</title>" : "<title>Studio</title>", { status: 200 }),
  );
}

describe("OptionsSection", () => {
  beforeEach(() => {
    useStudioOptionsStore.setState(useStudioOptionsStore.getInitialState(), true);
    resetOpenReelProbeForTests();
    vi.spyOn(API, "getUserOptions").mockResolvedValue({});
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage("zh");
  });

  it("选中界面语言后立即切换整页文案", async () => {
    serveOpenReel(false);
    const user = userEvent.setup();
    render(<OptionsSection />);

    const language = screen.getByRole("combobox", { name: "界面语言" });
    expect(language).toHaveTextContent("中文");

    await user.click(language);
    await user.click(await screen.findByRole("option", { name: "English" }));

    expect(await screen.findByRole("combobox", { name: "Interface language" })).toHaveTextContent("English");
    expect(i18n.language).toBe("en");
  });

  it("选浅色后立即切换，重新打开设置仍是浅色", async () => {
    serveOpenReel(false);
    document.documentElement.classList.add("dark");
    const user = userEvent.setup();
    const { unmount } = render(<OptionsSection />);

    const theme = screen.getByRole("combobox", { name: "主题" });
    expect(theme).toHaveTextContent("跟随系统");

    await user.click(theme);
    await user.click(await screen.findByRole("option", { name: "浅色" }));

    expect(document.documentElement).not.toHaveClass("dark");
    unmount();
    render(<OptionsSection />);
    expect(screen.getByRole("combobox", { name: "主题" })).toHaveTextContent("浅色");
  });

  it("工作室不提供 OpenReel 时不列出 OpenReel 的选项，默认编辑器只有内置编辑器", async () => {
    serveOpenReel(false);
    const user = userEvent.setup();
    render(<OptionsSection />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

    expect(screen.queryByRole("heading", { name: "OpenReel 编辑器" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "默认编辑器" }));
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).getAllByRole("option").map((option) => option.textContent)).toEqual(["内置编辑器"]);
  });

  it("工作室提供 OpenReel 时可选它作默认编辑器，并保存到工作室", async () => {
    serveOpenReel(true);
    const update = vi.spyOn(API, "updateUserOptions").mockResolvedValue({ default_editor: "openreel" });
    const user = userEvent.setup();
    render(<OptionsSection />);

    expect(await screen.findByRole("heading", { name: "OpenReel 编辑器" })).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "默认编辑器" }));
    await user.click(await screen.findByRole("option", { name: "OpenReel" }));

    expect(update).toHaveBeenCalledWith({ default_editor: "openreel" });
    expect(screen.getByRole("combobox", { name: "默认编辑器" })).toHaveTextContent("OpenReel");
  });

  it("片段拖动改为立即移动后写入 OpenReel 读取的键", async () => {
    serveOpenReel(true);
    const user = userEvent.setup();
    render(<OptionsSection />);

    await user.click(await screen.findByRole("combobox", { name: "在片段上拖动" }));
    await user.click(await screen.findByRole("option", { name: "立即移动片段" }));

    expect(window.localStorage.getItem(CLIP_DRAG_STORAGE_KEY)).toBe("move");
  });

  it("单独标签页开关默认开启，关闭后存在本设备", async () => {
    serveOpenReel(false);
    const user = userEvent.setup();
    render(<OptionsSection />);

    const toggle = screen.getByRole("switch", { name: "在单独的标签页中打开编辑器" });
    expect(toggle).toBeChecked();
    await user.click(toggle);

    expect(toggle).not.toBeChecked();
    expect(window.localStorage.getItem(EDITOR_OWN_TAB_STORAGE_KEY)).toBe("off");
  });

  it("模式默认是进阶，并说明简易模式的作用", async () => {
    serveOpenReel(false);
    render(<OptionsSection />);

    expect(screen.getByRole("combobox", { name: "模式" })).toHaveTextContent("进阶");
    expect(screen.getByText(/简易模式会随功能逐步适配/)).toBeInTheDocument();
  });
});
