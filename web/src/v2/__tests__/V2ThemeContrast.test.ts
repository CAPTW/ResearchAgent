import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

type Rgb = readonly [number, number, number];

const CSS = readFileSync(resolve(process.cwd(), "src/v2/v2.css"), "utf8");

function block(selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = CSS.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`));
  if (!match) throw new Error(`Missing semantic token block: ${selector}`);
  return match[1];
}

function rgbVariables(source: string) {
  const result: Record<string, Rgb> = {};
  for (const match of source.matchAll(/--([\w-]+):\s*rgb\((\d+)\s+(\d+)\s+(\d+)\)/g)) {
    result[match[1]] = [Number(match[2]), Number(match[3]), Number(match[4])];
  }
  return result;
}

function luminance(rgb: Rgb) {
  const [red, green, blue] = rgb.map((value) => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return (0.2126 * red) + (0.7152 * green) + (0.0722 * blue);
}

function contrast(left: Rgb, right: Rgb) {
  const [lighter, darker] = [luminance(left), luminance(right)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

describe("M1 semantic contrast matrix", () => {
  const base = rgbVariables(block(":root"));
  const themes = ["light", "dark", "solarized-light", "solarized-dark"] as const;
  const accents = ["blue", "cyan", "violet", "green", "amber"] as const;

  it.each(themes)("keeps %s shell text and navigation contrast accessible", (theme) => {
    const values = theme === "dark"
      ? base
      : { ...base, ...rgbVariables(block(`:root[data-theme=\"${theme}\"]`)) };

    expect(contrast(values["text-primary"], values["surface-panel"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(values["text-muted"], values["surface-panel"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(values["nav-text"], values["surface-sidebar"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(values["nav-text-active"], values["nav-bg-active"])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(accents)("keeps %s primary, hover, and focus states distinguishable", (accent) => {
    const values = accent === "blue"
      ? base
      : { ...base, ...rgbVariables(block(`:root[data-accent=\"${accent}\"]`)) };

    expect(contrast(values["accent-primary"], values["accent-on-color"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(values["accent-hover"], values["accent-on-color"])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(values["focus-ring"], base["surface-panel"])).toBeGreaterThanOrEqual(3);
    expect(contrast(values["focus-ring"], rgbVariables(block(':root[data-theme="light"]'))["surface-panel"])).toBeGreaterThanOrEqual(3);
  });

  it("keeps reduced-motion, forced-color, and visible-focus fallbacks in the stylesheet", () => {
    expect(CSS).toContain(":focus-visible");
    expect(CSS).toContain(':root[data-motion="reduced"]');
    expect(CSS).toContain("@media (forced-colors: active)");
    expect(CSS).toContain("outline: 2px solid Highlight");
  });
});
