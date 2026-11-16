import { describe, expect, it } from "vitest";
import i18n, { i18nReady } from "@/i18n";

describe("the page's declared language follows the studio's", () => {
  it("sets <html lang> on a language change (zh as zh-CN)", async () => {
    await i18nReady;
    const before = i18n.language;
    await i18n.changeLanguage("ru");
    expect(document.documentElement.lang).toBe("ru");
    await i18n.changeLanguage("zh");
    expect(document.documentElement.lang).toBe("zh-CN");
    await i18n.changeLanguage("en");
    expect(document.documentElement.lang).toBe("en");
    await i18n.changeLanguage(before);
  });
});
