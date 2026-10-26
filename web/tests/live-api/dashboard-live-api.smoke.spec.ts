import { expect, test } from "@playwright/test";

const topicId = "large-language-models";
const dashboardPath = `/app/topics/${topicId}/dashboard`;
const dashboardApiUrl = `http://127.0.0.1:8000/api/topics/${topicId}/dashboard`;

test("live dashboard API truth is rendered in the browser", async ({ page }) => {
  const dashboardRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes(`/api/topics/${topicId}/dashboard`)) {
      dashboardRequests.push(request.url());
    }
  });

  const dashboardResponsePromise = page.waitForResponse(
    (response) => response.url() === dashboardApiUrl && response.request().method() === "GET" && response.status() === 200,
  );

  await page.goto(dashboardPath);
  const dashboardResponse = await dashboardResponsePromise;
  const payload = await dashboardResponse.json();

  expect(dashboardRequests).toContain(dashboardApiUrl);
  expect(payload.reviewCoverage).toMatchObject({
    reviewedCount: 165,
    reviewedUniqueCount: 165,
    fullTextReviewedCount: 164,
    fullTextReviewedUniqueCount: 164,
    coveragePercentage: 31.5,
    totalPapers: 524,
  });
  expect(payload.researchOperations.coverage).toMatchObject({
    fullTextReviewedUniqueCount: 164,
    topicRelevantUniqueCount: 524,
  });

  await expect(page.getByRole("heading", { name: "Large Language Models", exact: true })).toBeVisible();

  const reviewCoverageCard = page
    .getByRole("heading", { name: "리뷰 커버리지" })
    .locator("xpath=ancestor::section[contains(@class, 'ai-card')][1]");
  await expect(reviewCoverageCard.getByText("31.5%")).toBeVisible();
  await expect(reviewCoverageCard.locator(".ai-review-coverage-grid").getByText("165", { exact: true })).toBeVisible();
  await expect(reviewCoverageCard.locator(".ai-review-coverage-grid").getByText("164", { exact: true })).toBeVisible();
  await expect(reviewCoverageCard.locator(".ai-review-coverage-grid").getByText("524", { exact: true })).toBeVisible();

  const operationsCard = page
    .getByRole("heading", { name: "Research Operations" })
    .locator("xpath=ancestor::section[contains(@class, 'ai-card')][1]");
  await expect(operationsCard.getByText("전문 검토 164 / 524")).toBeVisible();

  await expect(page.getByText("30.9%")).toHaveCount(0);
  await expect(page.getByText("전문 검토 98 / 524")).toHaveCount(0);
});
