import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useProjectsStore } from "@/stores/projects-store";
import { AssetMentionMenu, assetMention, insertAtSelection } from "./AssetMentionMenu";

describe("insertAtSelection", () => {
  it("inserts at the cursor, or in place of a selection, and puts the caret after it", () => {
    expect(insertAtSelection("ab", "@[X]", 1, 1)).toEqual({ value: "a@[X]b", caret: 5 });
    expect(insertAtSelection("a--b", "@[X]", 1, 3)).toEqual({ value: "a@[X]b", caret: 5 });
    expect(insertAtSelection("ab", "@[X]", 9, 9)).toEqual({ value: "ab@[X]", caret: 6 });
  });

  it("writes derivatives as Character/Derivative", () => {
    expect(assetMention("The Woman", "Angel Look")).toBe("@[The Woman/Angel Look]");
    expect(assetMention("Wine Glass")).toBe("@[Wine Glass]");
  });
});

describe("AssetMentionMenu", () => {
  it("offers Insert character and Insert prop on right-click", async () => {
    useProjectsStore.setState({
      currentProjectData: {
        characters: { "The Woman": { derivatives: { "Angel Look": {} } } },
        props: { "Wine Glass": {} },
      } as never,
    });
    const onChange = vi.fn();
    render(
      <AssetMentionMenu value="She lifts " onChange={onChange}>
        <textarea aria-label="prompt" defaultValue="She lifts " />
      </AssetMentionMenu>,
    );
    fireEvent.contextMenu(screen.getByLabelText("prompt"));
    expect(await screen.findByRole("menuitem", { name: /插入角色/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /插入道具/ })).toBeInTheDocument();
  });
});
