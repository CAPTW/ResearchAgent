import { existsSync, readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("v2 legacy boundary", () => {
  it("keeps active application entry points on the v2 shell only", () => {
    const app = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
    const main = readFileSync(resolve(process.cwd(), "src/main.tsx"), "utf8");

    expect(app).toContain('from "./v2/V2Shell"');
    expect(app).not.toMatch(/from "\.\/screens\//);
    expect(app).not.toContain("ProductShell");
    expect(main).toContain('import "./v2/v2.css"');
    expect(main).not.toContain("styles/index.css");
  });

  it("removes every tracked legacy screen, component and stylesheet file", () => {
    for (const directory of ["src/screens", "src/components", "src/styles"]) {
      const absolute = resolve(process.cwd(), directory);
      const files = existsSync(absolute)
        ? readdirSync(absolute, { recursive: true }).filter((entry) => typeof entry === "string" && entry.includes("."))
        : [];
      expect(files, `${directory} should contain no legacy files`).toEqual([]);
    }
  });
});
