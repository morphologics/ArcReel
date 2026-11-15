import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { useOnboardingStore } from "@/stores/onboarding-store";

import { GeneralSection } from "./GeneralSection";

describe("GeneralSection", () => {
  beforeEach(() => {
    useOnboardingStore.setState(useOnboardingStore.getInitialState(), true);
  });

  it("重看引导会重新开始新手引导", async () => {
    const user = userEvent.setup();
    render(<GeneralSection />);

    await user.click(screen.getByRole("button", { name: "重看引导" }));

    expect(useOnboardingStore.getState().active).toBe(true);
  });
});
