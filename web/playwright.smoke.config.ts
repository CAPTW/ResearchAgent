import { defineConfig, devices } from "@playwright/test";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const outputRoot = path.join(os.tmpdir(), "xrml-playwright-smoke-output");
// Prefer an installed browser on Windows so smoke validation does not need a browser download.
const localBrowserChannel =
  process.env.PLAYWRIGHT_BROWSER_CHANNEL?.trim() || (process.platform === "win32" ? "msedge" : "");

export default defineConfig({
  testDir: "./tests/smoke",
  outputDir: outputRoot,
  reporter: "line",
  timeout: 45_000,
  workers: 1,
  fullyParallel: false,
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1536, height: 1024 },
    baseURL: "http://127.0.0.1:5174",
    colorScheme: "dark",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
    screenshot: "off",
    video: "off",
    trace: "off",
    ...(localBrowserChannel ? { channel: localBrowserChannel } : {}),
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5174 --strictPort",
    cwd: webRoot,
    url: "http://127.0.0.1:5174/app/",
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
