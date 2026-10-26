import { defineConfig, devices } from "@playwright/test";

const localBrowserChannel = process.env.XRML_PLAYWRIGHT_CHANNEL?.trim();

export default defineConfig({
  testDir: "./tests/visual",
  snapshotDir: "./tests/visual/__snapshots__",
  timeout: 30_000,
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      maxDiffPixelRatio: 0.01,
    },
  },
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1536, height: 1024 },
    baseURL: "http://127.0.0.1:5173",
    colorScheme: "dark",
    locale: "ko-KR",
    timezoneId: "Asia/Seoul",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1",
    url: "http://127.0.0.1:5173/app/",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
        ...(localBrowserChannel ? { channel: localBrowserChannel } : {}),
      },
    },
  ],
});
