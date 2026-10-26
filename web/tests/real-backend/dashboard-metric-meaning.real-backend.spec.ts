import path from "node:path";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("dashboard shows validated monthly distribution and local metric meaning", async ({ page, context, backend }) => {
  const ledger = await installLedger(context, page);
  await page.goto(`/app/topics/${topicA}/dashboard`);
  const metrics = page.getByRole("region", { name: "연구 지표" });
  await expect(metrics.getByText("현재 집계 · 중복 제거")).toBeVisible();
  const response = await page.request.get(`/api/topics/${topicA}/dashboard`);
  expect(response.ok()).toBe(true);
  const dashboard = await response.json();
  expect(dashboard.analytics.artifactVersion).toBe("topic-analytics-v3");
  expect(dashboard.trends.labels.every((label: string) => /^(?!0000)\d{4}-(0[1-9]|1[0-2])$/.test(label))).toBe(true);
  expect(dashboard.warnings.some((warning: string) => warning.includes("실행월"))).toBe(true);
  // This corpus has real local paper records but no triage scores.
  expect(dashboard.analytics.coverage.find((item: { coverageId: string }) => item.coverageId === "triage_score").covered).toBe(0);
  await expect(metrics.locator("article").filter({ hasText: "평균 선별 점수" })).toContainText("데이터 부족");
  const collected = metrics.locator("article").filter({ hasText: "수집된 논문" });
  await expect(collected.locator("strong")).toHaveText(dashboard.metrics.collectedPapers.value.toLocaleString());
  const latest = metrics.locator("article").filter({ hasText: "최근 집계 월" });
  await expect(latest.locator("strong")).toHaveText(dashboard.trends.labels.at(-1));
  await expect(metrics).not.toContainText(/지난 7일|트렌드 지수|not-a-d/);
  await expect(page.getByRole("heading", { name: "수집 논문의 월별 분포" })).toBeVisible();
  await page.screenshot({ path: path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, "dashboard-metrics-wide.png"), fullPage: true, animations: "disabled" });
  await page.reload();
  await expect(collected.locator("strong")).toHaveText(dashboard.metrics.collectedPapers.value.toLocaleString());
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(latest).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, "dashboard-metrics-narrow.png"), fullPage: true, animations: "disabled" });
  expect(ledger.mutationRequests).toEqual([]);
  await assertHygiene(ledger);
  backend.writeJourneyResult({ journey: "dashboard metric meaning", migrationIds: [],
    assertions: { validatedDates: true, reloadPreservesCounts: true, narrowNoOverflow: true }, timing: {},
    hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length },
  });
});
