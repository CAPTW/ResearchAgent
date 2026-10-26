import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const baseURL = process.env.XRML_REAL_BACKEND_BASE_URL?.trim();
const outputDir = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR?.trim();

if (baseURL !== "http://127.0.0.1:8002") {
  throw new Error("XRML_REAL_BACKEND_BASE_URL must be exactly http://127.0.0.1:8002");
}
if (!outputDir || !path.isAbsolute(outputDir)) {
  throw new Error("XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR must be an absolute OS-temp path");
}

const localBrowserChannel =
  process.env.PLAYWRIGHT_BROWSER_CHANNEL?.trim() || (process.platform === "win32" ? "msedge" : "");

export default defineConfig({
  testDir: "./tests/real-backend",
  outputDir,
  reporter: "line",
  timeout: 45_000,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1536, height: 1024 },
    baseURL,
    colorScheme: "dark",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
    screenshot: "off",
    video: "off",
    trace: "off",
    ...(localBrowserChannel ? { channel: localBrowserChannel } : {}),
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
