import { expect, type Page, type Route, test } from "@playwright/test";

import type {
  ApiTopicRecommendationsResponse,
  ApiTopicsOverviewResponse,
  ApiTopicsResponse,
} from "../../src/lib/api";
import { authFixture } from "../fixtures/large-language-models-dashboard";

const appOrigin = "http://127.0.0.1:5174";

const topicsFixture = {
  items: [
    {
      id: "large-language-models",
      name: "Large Language Models",
      description: "LLM 평가와 검색 증강 연구",
      updatedAt: "2026-05-07T09:00:00+09:00",
      isFavorite: true,
      status: "active",
    },
    {
      id: "extended-reality",
      name: "Extended Reality",
      description: "XR 상호작용과 공간 지능 연구",
      updatedAt: "2026-05-06T09:00:00+09:00",
      isFavorite: false,
      status: "active",
    },
  ],
} satisfies ApiTopicsResponse;

const portfolioOverviewFixture = {
  summary: {
    topicCount: 2,
    collectedPapers: 150,
    fullTextReviewedCount: 42,
  },
  items: [
    {
      id: "large-language-models",
      name: "Large Language Models",
      description: "LLM 평가와 검색 증강 연구",
      updatedAt: "2026-05-07T09:00:00+09:00",
      isFavorite: true,
      collectedPapers: 120,
      collectedDelta: 12,
      collectedSparkline: [84, 96, 108, 120],
      corePapers: 40,
      coveragePercentage: 30,
      fullTextReviewedCount: 36,
      totalPapers: 120,
      trendLabel: "상승",
      trendDeltaPercent: 14,
      degraded: false,
    },
    {
      id: "extended-reality",
      name: "Extended Reality",
      description: "XR 상호작용과 공간 지능 연구",
      updatedAt: "2026-05-06T09:00:00+09:00",
      isFavorite: false,
      collectedPapers: 30,
      collectedDelta: 4,
      collectedSparkline: [18, 22, 26, 30],
      corePapers: 10,
      coveragePercentage: 20,
      fullTextReviewedCount: 6,
      totalPapers: 30,
      trendLabel: "안정",
      trendDeltaPercent: 3,
      degraded: false,
    },
  ],
} satisfies ApiTopicsOverviewResponse;

const recommendationsFixture = {
  topicId: "large-language-models",
  newTopicCount: 1,
  gapCount: 1,
  coverageWarning: "추천 근거는 검토가 완료된 논문 범위로 제한됩니다.",
  items: [
    {
      id: "topic-evaluation",
      type: "new_topic",
      title: "Evaluation for LLM",
      question: "How should retrieval-grounded LLM evaluations be compared?",
      whyNow: "Evaluation signals recur across recent reviewed papers.",
      methodDirection: "Build a review-backed benchmark matrix.",
      expectedContribution: "A reproducible evaluation taxonomy.",
      gapStatement: "",
      noveltyRationale: "",
      confidence: "medium",
      supportingPaperCount: 2,
      suggestedQueries: ["llm evaluation", "retrieval grounded benchmark"],
      actionable: true,
    },
    {
      id: "gap-xr-evidence",
      type: "gap",
      title: "XR evaluation evidence gap",
      question: "",
      whyNow: "",
      methodDirection: "",
      expectedContribution: "",
      gapStatement: "Cross-device XR evidence remains sparse.",
      noveltyRationale: "Compare evaluation protocols across headsets.",
      confidence: "low",
      supportingPaperCount: 1,
      suggestedQueries: [],
      actionable: false,
    },
  ],
} satisfies ApiTopicRecommendationsResponse;

const expectedCreateTopicRequest = {
  title: "Evaluation for LLM",
  topic: "Evaluation for LLM",
  description: "How should retrieval-grounded LLM evaluations be compared?",
  queries: ["llm evaluation", "retrieval grounded benchmark"],
  categories: [],
  max_results: 60,
  top_papers: 8,
  weekly_top: 5,
};

type MockLedger = {
  requestLog: string[];
  createTopicRequests: unknown[];
  unexpectedApiRequests: string[];
  externalRequests: string[];
};

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function isApiLikePath(pathname: string) {
  return pathname.startsWith("/auth/") || pathname.startsWith("/api/") || pathname === "/topic-profiles";
}

async function installPortfolioRecommendationMocks(page: Page): Promise<MockLedger> {
  const ledger: MockLedger = {
    requestLog: [],
    createTopicRequests: [],
    unexpectedApiRequests: [],
    externalRequests: [],
  };

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const requestKey = `${request.method()} ${url.pathname}`;

    if (url.origin !== appOrigin) {
      ledger.externalRequests.push(requestKey);
      await route.abort("blockedbyclient");
      return;
    }

    if (!isApiLikePath(url.pathname)) {
      await route.continue();
      return;
    }

    ledger.requestLog.push(requestKey);
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204 });
      return;
    }
    if (requestKey === "GET /auth/me") {
      await fulfillJson(route, authFixture);
      return;
    }
    if (requestKey === "GET /api/topics/overview") {
      await fulfillJson(route, portfolioOverviewFixture);
      return;
    }
    if (requestKey === "GET /api/topics") {
      await fulfillJson(route, topicsFixture);
      return;
    }
    if (requestKey === "GET /api/topics/large-language-models/recommendations") {
      await fulfillJson(route, recommendationsFixture);
      return;
    }
    if (requestKey === "POST /topic-profiles") {
      ledger.createTopicRequests.push(request.postDataJSON());
      await fulfillJson(route, { items: [] });
      return;
    }

    ledger.unexpectedApiRequests.push(requestKey);
    await fulfillJson(route, { detail: `Unexpected smoke API request: ${requestKey}` }, 500);
  });

  return ledger;
}

function monitorBrowserErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    errors.push(error.message);
  });
  return errors;
}

test("portfolio route renders aggregate, chart, heatmap, and topic drill-down truth", async ({ page }) => {
  const browserErrors = monitorBrowserErrors(page);
  const ledger = await installPortfolioRecommendationMocks(page);

  await page.goto("/app/portfolio");

  await expect(page).toHaveURL(`${appOrigin}/app/portfolio`);
  await expect(page.getByRole("navigation", { name: "primary app navigation" }).getByRole("link", { name: "포트폴리오" }))
    .toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "토픽 포트폴리오" })).toBeVisible();
  const aggregate = page.getByLabel("포트폴리오 집계");
  await expect(aggregate.getByText("토픽 2개", { exact: true })).toBeVisible();
  await expect(aggregate.getByText("수집 논문 150편", { exact: true })).toBeVisible();
  await expect(aggregate.getByText("본문 검토 42편", { exact: true })).toBeVisible();

  const llmCard = page.getByRole("link", { name: "Large Language Models 토픽 대시보드로 이동" });
  const xrCard = page.getByRole("link", { name: "Extended Reality 토픽 대시보드로 이동" });
  await expect(llmCard).toBeVisible();
  await expect(llmCard.getByText("120", { exact: true })).toBeVisible();
  await expect(llmCard.getByText("40", { exact: true })).toBeVisible();
  await expect(llmCard.getByText("본문 검토 30% · 36/120편", { exact: true })).toBeVisible();
  await expect(xrCard).toBeVisible();
  await expect(xrCard.getByText("30", { exact: true })).toBeVisible();
  await expect(xrCard.getByText("10", { exact: true })).toBeVisible();
  await expect(xrCard.getByText("본문 검토 20% · 6/30편", { exact: true })).toBeVisible();
  await expect(xrCard).toHaveAttribute("href", "/app/topics/extended-reality");

  await expect(page.getByRole("img", { name: "토픽 분야 비교" })).toBeVisible();
  await expect(page.getByRole("img", { name: "토픽 지표 히트맵" })).toBeVisible();
  await expect(page.getByText("토픽이 없습니다.")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "다시 시도" })).toHaveCount(0);

  const portfolioTopicReads = ledger.requestLog.filter((request) => request.startsWith("GET /api/topics"));
  const overviewReads = portfolioTopicReads.filter((request) => request === "GET /api/topics/overview");
  expect(overviewReads.length).toBeGreaterThanOrEqual(1);
  expect(overviewReads.length).toBeLessThanOrEqual(2);
  expect(new Set(portfolioTopicReads)).toEqual(new Set(["GET /api/topics/overview"]));
  expect(ledger.requestLog.filter((request) => request === "GET /api/topics")).toHaveLength(0);
  expect(
    ledger.requestLog.filter((request) => /^GET \/api\/topics\/[^/]+\/dashboard$/.test(request)),
  ).toHaveLength(0);
  expect(ledger.requestLog.filter((request) => request.startsWith("POST "))).toHaveLength(0);
  expect(ledger.unexpectedApiRequests).toEqual([]);
  expect(ledger.externalRequests).toEqual([]);
  expect(browserErrors).toEqual([]);
});

test("recommendations route creates one topic through the browser mock with the exact request", async ({ page }) => {
  const browserErrors = monitorBrowserErrors(page);
  const ledger = await installPortfolioRecommendationMocks(page);

  await page.goto("/app/recommendations");

  await expect(page).toHaveURL(`${appOrigin}/app/recommendations`);
  await expect(page.getByRole("navigation", { name: "primary app navigation" }).getByRole("link", { name: "추천" }))
    .toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "새 연구 주제 추천" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "새 주제 후보" })).toBeVisible();

  const candidate = page.getByRole("article").filter({ hasText: "Evaluation for LLM" });
  await expect(candidate.getByText("신뢰도 medium", { exact: true })).toBeVisible();
  await expect(candidate.getByText(`왜 지금: ${recommendationsFixture.items[0].whyNow}`, { exact: true })).toBeVisible();
  await expect(candidate.getByText(`방법: ${recommendationsFixture.items[0].methodDirection}`, { exact: true })).toBeVisible();
  await expect(candidate.getByText("llm evaluation", { exact: true })).toBeVisible();
  await expect(candidate.getByText("근거 논문 2편", { exact: true })).toBeVisible();
  await expect(page.getByText("XR evaluation evidence gap", { exact: true })).toBeVisible();

  const createButton = candidate.getByRole("button", { name: "Evaluation for LLM 새 토픽으로 추가" });
  await expect(createButton).toBeEnabled();
  await createButton.click();

  await expect(page.getByText('"Evaluation for LLM" 토픽을 생성했습니다. 포트폴리오에서 확인하세요.')).toBeVisible();
  await expect(createButton).toBeEnabled();
  await expect.poll(() => ledger.createTopicRequests.length).toBe(1);
  expect(ledger.createTopicRequests).toEqual([expectedCreateTopicRequest]);
  expect(ledger.requestLog.filter((request) => request === "POST /topic-profiles")).toHaveLength(1);
  await expect(page.getByText("추천이 아직 없습니다.")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "다시 시도" })).toHaveCount(0);
  expect(ledger.unexpectedApiRequests).toEqual([]);
  expect(ledger.externalRequests).toEqual([]);
  expect(browserErrors).toEqual([]);
});
