import { defineConfig, devices } from "@playwright/test";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(webRoot, "..");
const outputRoot = path.join(os.tmpdir(), "xrml-l7c-playwright-output");
const isWindows = process.platform === "win32";

const backendCommand = isWindows
  ? "set PYTHONDONTWRITEBYTECODE=1&& set AUTH_MODE=disabled&& set TOPIC_SCHEDULER_ENABLED=0&& python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
  : "PYTHONDONTWRITEBYTECODE=1 AUTH_MODE=disabled TOPIC_SCHEDULER_ENABLED=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8000";

const frontendCommand = isWindows
  ? "set VITE_API_BASE_URL=http://127.0.0.1:8000&& npm run dev -- --host 127.0.0.1"
  : "VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1";

export default defineConfig({
  testDir: "./tests/live-api",
  outputDir: outputRoot,
  reporter: "line",
  timeout: 60_000,
  workers: 1,
  fullyParallel: false,
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1536, height: 1024 },
    baseURL: "http://127.0.0.1:5173",
    colorScheme: "dark",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
    channel: "chrome",
    screenshot: "off",
    video: "off",
    trace: "off",
  },
  webServer: [
    {
      command: backendCommand,
      cwd: repoRoot,
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: frontendCommand,
      cwd: webRoot,
      url: "http://127.0.0.1:5173/app/",
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
