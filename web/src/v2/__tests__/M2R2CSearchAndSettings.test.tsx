import { describe, expect, it } from "vitest";

import { DEFAULT_APPEARANCE, formatTimestamp, type AppearancePreferences } from "../appearance";

describe("R2C search/settings contracts", () => {
  it("uses one parser-derived normalization contract for search examples", async () => {
      const module = await import("../../lib/api");
    expect(module.normalizeSearchQuery("ＳＨＡＲＥＤ\n  Systems")).toBe("shared systems");
    expect(module.SEARCH_SYNTAX_CONTRACT.boolean.interpretation).toBe("literal");
    expect(module.SEARCH_SYNTAX_CONTRACT.wildcard.interpretation).toBe("literal");
  });

  it("formats locale, 12-hour, and 24-hour timestamps without changing the instant", () => {
    const instant = "2026-02-01T13:05:00Z";
    expect(formatTimestamp(instant, "12h", "en-US")).toMatch(/PM|AM/);
    expect(formatTimestamp(instant, "24h", "en-US")).not.toMatch(/PM|AM/);
    expect(formatTimestamp(instant, "locale", "en-US")).toBeTruthy();
  });

  it("keeps appearance defaults backward compatible while exposing timeFormat", () => {
    const preferences: AppearancePreferences = { ...DEFAULT_APPEARANCE, timeFormat: "24h" };
    expect(preferences.timeFormat).toBe("24h");
    expect(DEFAULT_APPEARANCE.timeFormat).toBe("locale");
  });
});
