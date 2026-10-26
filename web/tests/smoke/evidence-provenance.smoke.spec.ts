import { expect, type Page, type Route, test } from "@playwright/test";

import type {
  ApiTopicsOverviewResponse,
  ApiTopicsResponse,
  EvidenceItemDetailResponse,
  EvidenceSearchResponse,
  EvidenceSearchResultItem,
} from "../../src/lib/api";
import { authFixture } from "../fixtures/large-language-models-dashboard";

const appOrigin = "http://127.0.0.1:5174";
const queryText = "spatial anchors";
const paperTitle = "Spatial Evidence Systems";
const maliciousSnippet = "<img src=x onerror=alert('smoke')> spatial anchor evidence";

const portfolioFixture = {
  summary: {
    topicCount: 1,
    collectedPapers: 12,
    fullTextReviewedCount: 4,
  },
  items: [
    {
      id: "topic-alpha",
      name: "Spatial Computing",
      description: "Deterministic local smoke topic",
      updatedAt: "2026-07-01T00:00:00Z",
      isFavorite: true,
      collectedPapers: 12,
      collectedDelta: 2,
      collectedSparkline: [6, 8, 10, 12],
      corePapers: 4,
      coveragePercentage: 50,
      fullTextReviewedCount: 4,
      totalPapers: 12,
      trendLabel: "안정",
      trendDeltaPercent: 2,
      degraded: false,
    },
  ],
} satisfies ApiTopicsOverviewResponse;

const topicsFixture = {
  items: [
    {
      id: "topic-alpha",
      name: "Spatial Computing",
      description: "Deterministic local smoke topic",
      updatedAt: "2026-07-01T00:00:00Z",
      isFavorite: true,
      status: "active",
    },
  ],
} satisfies ApiTopicsResponse;

const links = {
  topic: "/topics/topic-alpha/papers",
  run: "/runs/run-a",
  report: "/reports/run-a",
  paper: "javascript:alert('blocked')",
};

const occurrences = [
  {
    runId: "run-a",
    rawPaperId: "2601.00001v1",
    title: paperTitle,
    topicId: "topic-alpha",
    topicName: "Spatial Computing",
    runStatus: "completed",
    paperStatus: "reviewed",
    theme: "spatial anchors",
    score: 95,
    published: "2026-01-01",
    entryUrl: "https://example.test/2601.00001v1",
    pdfUrl: "https://example.test/2601.00001v1.pdf",
    updatedAt: "2026-07-01T00:00:00Z",
    links: {
      topic: "/topics/topic-alpha/papers",
      run: "/runs/run-a",
      report: "/reports/run-a",
      paper: "/runs/run-a?focus_paper_id=2601.00001v1",
    },
  },
  {
    runId: "run-b",
    rawPaperId: "2601.00001v2",
    title: `${paperTitle} v2`,
    topicId: "topic-beta",
    topicName: "Embodied AI",
    runStatus: "completed",
    paperStatus: "reviewed",
    theme: "embodied evidence",
    score: 91,
    published: "2026-02-01",
    entryUrl: "https://example.test/2601.00001v2",
    pdfUrl: "https://example.test/2601.00001v2.pdf",
    updatedAt: "2026-07-02T00:00:00Z",
    links: {
      topic: "/topics/topic-beta/papers",
      run: "/runs/run-b",
      report: "/reports/run-b",
      paper: "/runs/run-b?focus_paper_id=2601.00001v2",
    },
  },
] as const;

const paperItem = {
  resultId: "paper:2601.00001",
  resultKind: "paper",
  canonicalPaperId: "2601.00001",
  paperIdAliases: ["2601.00001v1", "2601.00001v2"],
  title: paperTitle,
  titleAuthority: "latest_review",
  safeSnippet: {
    text: maliciousSnippet,
    sourceField: "textExcerpt",
    truncated: false,
  },
  matchedFields: ["title", "textExcerpt"],
  matchedCorpusKinds: ["paper", "review", "evidence"],
  matchReason: "canonical paper and persisted review match",
  rankTier: 1,
  rankComponents: {
    tier: 1,
    tokenCoverage: 2,
    effectiveTimestamp: "2026-07-02T00:00:00Z",
    kindOrder: 0,
  },
  topicIds: ["topic-alpha", "topic-beta"],
  reviewSummary: {
    topicId: "topic-alpha",
    source: "provider_output_class_proof",
    providerId: "codex",
    level: "full_text_reviewed",
    reviewedAt: "2026-07-02T00:00:00Z",
    reviewModel: "codex_sdk_python/output_class_live",
    inlineEvidenceSpanCount: 2,
  },
  inlineEvidenceSpanCount: 2,
  runEvidenceCount: 1,
  occurrenceCount: 3,
  occurrences: [...occurrences],
  occurrencesTruncated: true,
  provenancePreview: [
    {
      edgeId: "preview-paper-review",
      edgeKind: "paper_has_review",
      fromNodeId: "paper-node",
      toNodeId: "review-node",
      truthLevel: "exact_persisted",
    },
  ],
  statuses: ["full_text_reviewed", "provider_proof_backed", "degraded", "unverified_reference"],
  warnings: ["provider_ownership_unproven"],
  links,
} satisfies EvidenceSearchResultItem;

const reportItem = {
  resultId: "report_section:run-a:findings",
  resultKind: "report_section",
  canonicalPaperId: null,
  paperIdAliases: [],
  title: "Evidence Findings",
  titleAuthority: "report_section",
  safeSnippet: {
    text: "Bounded report section summary",
    sourceField: "sectionTitle",
    truncated: false,
  },
  matchedFields: ["sectionTitle"],
  matchedCorpusKinds: ["report_section"],
  matchReason: "report section title match",
  rankTier: 4,
  rankComponents: {
    tier: 4,
    tokenCoverage: 1,
    effectiveTimestamp: "2026-07-03T00:00:00Z",
    kindOrder: 2,
  },
  topicIds: ["topic-alpha"],
  reviewSummary: null,
  inlineEvidenceSpanCount: 0,
  runEvidenceCount: 0,
  occurrenceCount: 0,
  occurrences: [],
  occurrencesTruncated: false,
  provenancePreview: [],
  statuses: ["missing_artifact"],
  warnings: ["report_provenance_not_indexed"],
  links: { topic: "/topics/topic-alpha/papers", run: "/runs/run-a", report: "/reports/run-a", paper: null },
} satisfies EvidenceSearchResultItem;

const emptyFilters: EvidenceSearchResponse["query"]["filters"] = {
  topicIds: [],
  runIds: [],
  corpusKinds: [],
  reviewSources: [],
  providerIds: [],
  reviewLevels: [],
  hasEvidence: null,
};

function makeSearchFixture(submitted: boolean): EvidenceSearchResponse {
  return {
    query: {
      raw: submitted ? queryText : "",
      normalized: submitted ? queryText : "",
      tokens: submitted ? ["spatial", "anchors"] : [],
      filters: submitted
        ? {
            ...emptyFilters,
            corpusKinds: ["paper"],
            reviewLevels: ["full_text_reviewed"],
            hasEvidence: true,
          }
        : emptyFilters,
    },
    page: {
      limit: 25,
      returnedCount: 2,
      total: 2,
      totalRelation: "exact",
      nextCursor: null,
    },
    facets: {
      corpusKinds: { paper: 1, review: 1, evidence: 1, report_section: 1 },
      reviewLevels: { full_text_reviewed: 1 },
      reviewSources: { provider_output_class_proof: 1 },
      providers: { codex: 1 },
      statuses: { degraded: 1, full_text_reviewed: 1, unverified_reference: 1 },
    },
    items: [paperItem, reportItem],
  };
}

const detailFixture = {
  resultId: paperItem.resultId,
  resultKind: "paper",
  summary: paperItem,
  occurrenceCount: 3,
  occurrences: [...occurrences],
  occurrencesTruncated: true,
  selectedOccurrenceRunId: "run-a",
  provenanceNodes: [
    {
      nodeId: "topic-node",
      nodeKind: "topic",
      stableId: "topic-alpha",
      label: "Spatial Computing topic",
      topicId: "topic-alpha",
      portableRef: "project:data/topics/topic-alpha.json",
      status: "current",
    },
    {
      nodeId: "run-node",
      nodeKind: "run",
      stableId: "run-a",
      label: "Alpha run",
      topicId: "topic-alpha",
      runId: "run-a",
      portableRef: "run:run-a",
      status: "current",
    },
    {
      nodeId: "paper-node",
      nodeKind: "paper",
      stableId: "2601.00001",
      label: paperTitle,
      paperId: "2601.00001",
      status: "full_text_reviewed",
    },
    {
      nodeId: "review-node",
      nodeKind: "review",
      stableId: "provider-review-1",
      label: "Provider review",
      topicId: "topic-alpha",
      runId: "run-a",
      portableRef: "project:data/reviews/provider-review-1.json",
      status: "provider_proof_backed",
    },
    {
      nodeId: "proof-node",
      nodeKind: "provider_proof",
      stableId: "proof-1",
      label: "Portable provider proof",
      portableRef: "C:\\Users\\smoke\\proof.json",
      status: "unverified_reference",
    },
  ],
  provenanceEdges: [
    {
      edgeId: "edge-1",
      edgeKind: "paper_has_review",
      fromNodeId: "paper-node",
      toNodeId: "review-node",
      truthLevel: "exact_persisted",
    },
    {
      edgeId: "edge-2",
      edgeKind: "run_contains_paper",
      fromNodeId: "run-node",
      toNodeId: "paper-node",
      truthLevel: "exact_indexed_projection",
    },
    {
      edgeId: "edge-3",
      edgeKind: "review_references_artifact",
      fromNodeId: "review-node",
      toNodeId: "proof-node",
      truthLevel: "exact_portable_ref",
    },
    {
      edgeId: "edge-4",
      edgeKind: "run_topic_matches_profile",
      fromNodeId: "run-node",
      toNodeId: "topic-node",
      truthLevel: "derived_deterministic",
    },
  ],
  artifactVerifications: [
    {
      portableRef: "project:data/reviews/provider-review-1.json",
      refKind: "project",
      status: "current",
      exists: true,
      expectedHashPresent: true,
      hashMatches: true,
    },
    {
      portableRef: "project:data/reviews/stale.json",
      refKind: "project",
      status: "stale",
      exists: true,
      expectedHashPresent: true,
      hashMatches: false,
    },
    {
      portableRef: "C:\\Users\\smoke\\artifact.json",
      refKind: "absolute",
      status: "unverified_reference",
      exists: false,
      expectedHashPresent: false,
    },
  ],
  warnings: ["malformed_provider_report"],
  links,
} satisfies EvidenceItemDetailResponse;

type ResponseClass = "mock-200" | "mock-500" | "blocked";

type RequestRecord = {
  method: string;
  pathname: string;
  query: Record<string, string[]>;
  responseClass: ResponseClass;
  expected: boolean;
};

type SmokeLedger = {
  records: RequestRecord[];
  mutationRequests: string[];
  unexpectedApiRequests: string[];
  offOriginRequests: string[];
  sensitiveEndpointRequests: string[];
};

function normalizeQuery(url: URL) {
  return Object.fromEntries(
    [...new Set(url.searchParams.keys())]
      .sort()
      .map((key) => [key, url.searchParams.getAll(key)]),
  );
}

function isApiLikePath(pathname: string) {
  return pathname.startsWith("/auth/") || pathname.startsWith("/api/") || pathname === "/topic-profiles";
}

function isSensitiveEndpoint(pathname: string) {
  return /\/(?:oauth|mcp|tunnel|providers?)(?:\/|$)|\/\.well-known\//i.test(pathname);
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function isSubmittedSearch(url: URL) {
  const keys = [...new Set(url.searchParams.keys())].sort();
  return (
    JSON.stringify(keys) ===
      JSON.stringify(["corpus_kinds", "has_evidence", "q", "review_levels"]) &&
    url.searchParams.get("q") === queryText &&
    url.searchParams.get("corpus_kinds") === "paper" &&
    url.searchParams.get("review_levels") === "full_text_reviewed" &&
    url.searchParams.get("has_evidence") === "true"
  );
}

async function installEvidenceMocks(page: Page): Promise<SmokeLedger> {
  const ledger: SmokeLedger = {
    records: [],
    mutationRequests: [],
    unexpectedApiRequests: [],
    offOriginRequests: [],
    sensitiveEndpointRequests: [],
  };

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const requestKey = `${method} ${url.pathname}`;

    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      ledger.mutationRequests.push(requestKey);
    }
    if (isSensitiveEndpoint(url.pathname)) {
      ledger.sensitiveEndpointRequests.push(requestKey);
    }
    if (url.origin !== appOrigin) {
      ledger.offOriginRequests.push(requestKey);
      ledger.records.push({
        method,
        pathname: url.pathname,
        query: normalizeQuery(url),
        responseClass: "blocked",
        expected: false,
      });
      await route.abort("blockedbyclient");
      return;
    }
    if (!isApiLikePath(url.pathname)) {
      await route.continue();
      return;
    }
    if (method === "OPTIONS") {
      await route.fulfill({ status: 204 });
      return;
    }

    const expectedRecord = () =>
      ledger.records.push({
        method,
        pathname: url.pathname,
        query: normalizeQuery(url),
        responseClass: "mock-200",
        expected: true,
      });

    if (requestKey === "GET /auth/me") {
      expectedRecord();
      await fulfillJson(route, authFixture);
      return;
    }
    if (requestKey === "GET /api/topics/overview") {
      expectedRecord();
      await fulfillJson(route, portfolioFixture);
      return;
    }
    if (requestKey === "GET /api/topics") {
      expectedRecord();
      await fulfillJson(route, topicsFixture);
      return;
    }
    if (requestKey === "GET /api/evidence/search" && [...url.searchParams.keys()].length === 0) {
      expectedRecord();
      await fulfillJson(route, makeSearchFixture(false));
      return;
    }
    if (requestKey === "GET /api/evidence/search" && isSubmittedSearch(url)) {
      expectedRecord();
      await fulfillJson(route, makeSearchFixture(true));
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/evidence/items/${encodeURIComponent(paperItem.resultId)}` &&
      [...url.searchParams.keys()].length === 0
    ) {
      expectedRecord();
      await fulfillJson(route, detailFixture);
      return;
    }

    ledger.unexpectedApiRequests.push(`${requestKey}${url.search}`);
    ledger.records.push({
      method,
      pathname: url.pathname,
      query: normalizeQuery(url),
      responseClass: "mock-500",
      expected: false,
    });
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

test("searches cross-corpus evidence and opens provenance detail without live requests", async ({ page }) => {
  const browserErrors = monitorBrowserErrors(page);
  const ledger = await installEvidenceMocks(page);

  await page.goto("/app/portfolio");
  await expect(page).toHaveURL(`${appOrigin}/app/portfolio`);
  await expect(page.getByRole("heading", { name: "토픽 포트폴리오" })).toBeVisible();

  const primaryNavigation = page.getByRole("navigation", { name: "primary app navigation" });
  const provenanceLink = primaryNavigation.getByRole("link", { name: "근거 탐색" });
  const guideLink = primaryNavigation.getByRole("link", { name: "사용 가이드" });
  await expect(provenanceLink).toBeVisible();
  await expect(guideLink).toHaveAttribute("href", "/app/evidence");
  await provenanceLink.click();

  await expect(page).toHaveURL(`${appOrigin}/app/provenance`);
  await expect(provenanceLink).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "근거·출처 탐색", level: 1 })).toBeVisible();
  await expect(page.getByText(/로컬·내부용 읽기 전용 탐색입니다/)).toBeVisible();
  await expect(page.getByText("총 2건 · 현재 2건", { exact: true })).toBeVisible();

  const evidenceReadsBeforeTyping = ledger.records.filter(
    (record) => record.pathname === "/api/evidence/search",
  ).length;
  await page.getByLabel("검색어").fill(queryText);
  const corpusGroup = page.getByRole("group", { name: "코퍼스 종류" });
  const paperCorpusCheckbox = corpusGroup.getByRole("checkbox", { name: "논문" });
  await paperCorpusCheckbox.click();
  await expect(paperCorpusCheckbox).toBeChecked();
  await page.getByText("고급 검색 조건", { exact: true }).click();
  const reviewLevelCheckbox = page
    .getByRole("group", { name: "검토 수준" })
    .getByRole("checkbox", { name: "본문 검토됨" });
  await reviewLevelCheckbox.click();
  await expect(reviewLevelCheckbox).toBeChecked();
  await page.getByLabel("세부 근거").selectOption("true");
  expect(ledger.records.filter((record) => record.pathname === "/api/evidence/search")).toHaveLength(
    evidenceReadsBeforeTyping,
  );

  await page.getByRole("button", { name: "검색", exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe(queryText);
  expect(new URL(page.url()).searchParams.get("corpus_kinds")).toBe("paper");
  expect(new URL(page.url()).searchParams.get("review_levels")).toBe("full_text_reviewed");
  expect(new URL(page.url()).searchParams.get("has_evidence")).toBe("true");
  expect(new URL(page.url()).searchParams.has("cursor")).toBe(false);
  expect(new URL(page.url()).searchParams.has("offset")).toBe(false);
  expect(new URL(page.url()).searchParams.has("page")).toBe(false);
  expect(new URL(page.url()).searchParams.has("provider_ids")).toBe(false);

  await expect(page.getByText("총 2건 · 현재 2건", { exact: true })).toBeVisible();
  await expect(
    page.getByText("한 정규 결과가 여러 코퍼스 facet에 포함되어 facet 합계는 전체 결과 수를 초과할 수 있습니다."),
  ).toBeVisible();
  await expect(page.getByText("paper 1", { exact: true })).toBeVisible();
  await expect(page.getByText("review 1", { exact: true })).toBeVisible();
  await expect(page.getByText("evidence 1", { exact: true })).toBeVisible();
  await expect(page.getByText("report_section 1", { exact: true })).toBeVisible();

  const paperCard = page.getByRole("article").filter({ hasText: paperTitle });
  await expect(paperCard.getByRole("heading", { name: paperTitle })).toBeVisible();
  await expect(paperCard.getByText("정규 ID 2601.00001", { exact: true })).toBeVisible();
  await expect(paperCard.getByText("2601.00001v1, 2601.00001v2", { exact: true })).toBeVisible();
  await expect(paperCard.getByText("paper · review · evidence", { exact: true })).toBeVisible();
  await expect(paperCard.getByText("canonical paper and persisted review match", { exact: true })).toBeVisible();
  await expect(paperCard.getByText(/inline span 2 · run evidence 1/)).toBeVisible();
  await expect(paperCard.getByText("본문 검토됨", { exact: true })).toBeVisible();
  await expect(paperCard.getByText("Provider proof 참조 있음", { exact: true })).toBeVisible();
  await expect(paperCard.getByText("참조 미검증", { exact: true })).toBeVisible();
  await expect(paperCard.getByText(maliciousSnippet, { exact: true })).toBeVisible();
  await expect(paperCard.locator("img, script")).toHaveCount(0);
  await paperCard.getByText("발생 위치 3건", { exact: true }).click();
  await expect(paperCard.getByText("전체 3건 중 API가 제공한 2건만 표시합니다.", { exact: true })).toBeVisible();

  const reportCard = page.getByRole("article").filter({ hasText: "Evidence Findings" });
  await expect(reportCard.getByText("독립 보고서 섹션", { exact: true })).toBeVisible();
  await expect(reportCard.getByText("보고서-논문 연결이 인덱싱되지 않음", { exact: true })).toBeVisible();
  await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Provider.*실행|검토.*생성|보고서.*재생성/ })).toHaveCount(0);

  const detailButton = paperCard.getByRole("button", { name: `${paperTitle} 출처 상세` });
  expect(ledger.records.filter((record) => record.pathname.startsWith("/api/evidence/items/"))).toHaveLength(0);
  await detailButton.click();

  const detailPanel = page.getByRole("complementary", { name: `${paperTitle} 출처 상세` });
  const detailHeading = detailPanel.getByRole("heading", { name: `${paperTitle} 출처 상세`, level: 2 });
  await expect(detailHeading).toBeFocused();
  await expect(detailPanel.getByRole("heading", { name: "출처 노드" })).toBeVisible();
  const nodeItems = detailPanel.getByRole("list", { name: "출처 노드 목록" }).getByRole("listitem");
  await expect(nodeItems).toHaveText([
    /Spatial Computing topic.*topic · topic-alpha.*참조 해시 일치/,
    /Alpha run.*run · run-a.*참조 해시 일치/,
    /Spatial Evidence Systems.*paper · 2601\.00001.*본문 검토됨/,
    /Provider review.*review · provider-review-1.*Provider proof 참조 있음/,
    /Portable provider proof.*provider_proof · proof-1.*참조 미검증/,
  ]);
  const edgeList = detailPanel.getByRole("heading", { name: "출처 연결" }).locator("xpath=following-sibling::ol[1]");
  await expect(edgeList.getByRole("listitem")).toHaveText([
    /paper-node → paper_has_review → review-node.*영속 기록/,
    /run-node → run_contains_paper → paper-node.*인덱스 투영/,
    /review-node → review_references_artifact → proof-node.*휴대형 참조/,
    /run-node → run_topic_matches_profile → topic-node.*결정론적 연결/,
  ]);
  await expect(detailPanel.getByText("Provider 보고서 형식이 올바르지 않음", { exact: true })).toBeVisible();
  await expect(detailPanel.getByText("일부 연결 정보 누락", { exact: true })).toBeVisible();
  const portableRefs = detailPanel.getByText("project:data/reviews/provider-review-1.json", { exact: true });
  await expect(portableRefs).toHaveCount(2);
  await expect(portableRefs.first()).toBeVisible();
  await expect(detailPanel.getByText("C:\\Users\\smoke\\proof.json", { exact: true })).toHaveCount(0);
  await expect(detailPanel.getByText("C:\\Users\\smoke\\artifact.json", { exact: true })).toHaveCount(0);
  await expect(detailPanel.getByText("raw provider output", { exact: false })).toHaveCount(0);
  await expect(detailPanel.getByText('"resultId"', { exact: true })).toHaveCount(0);

  const detailLinks = detailPanel.getByRole("navigation", { name: "상세 내부 링크" });
  await expect(detailLinks.getByRole("link", { name: "토픽" })).toHaveAttribute(
    "href",
    "/app/topics/topic-alpha/papers",
  );
  await expect(detailLinks.getByRole("link", { name: "Run" })).toHaveAttribute("href", "/app/runs/run-a");
  await expect(detailLinks.getByRole("link", { name: "보고서" })).toHaveAttribute("href", "/app/reports/run-a");

  await page.keyboard.press("Escape");
  await expect(detailPanel).toHaveCount(0);
  await expect(detailButton).toBeFocused();
  await detailButton.click();
  await expect(page.getByRole("complementary", { name: `${paperTitle} 출처 상세` })).toBeVisible();
  await expect.poll(
    () => ledger.records.filter((record) => record.pathname.startsWith("/api/evidence/items/")).length,
  ).toBe(1);
  await page.keyboard.press("Escape");

  const evidenceSearchRecords = ledger.records.filter(
    (record) => record.pathname === "/api/evidence/search" && record.expected,
  );
  const browseRecords = evidenceSearchRecords.filter((record) => Object.keys(record.query).length === 0);
  const submittedRecords = evidenceSearchRecords.filter(
    (record) => record.query.q?.[0] === queryText,
  );
  expect(browseRecords.length).toBeGreaterThanOrEqual(1);
  expect(browseRecords.length).toBeLessThanOrEqual(2);
  expect(submittedRecords.length).toBeGreaterThanOrEqual(1);
  expect(submittedRecords.length).toBeLessThanOrEqual(2);
  expect(new Set(evidenceSearchRecords.map((record) => JSON.stringify(record.query)))).toEqual(
    new Set([
      JSON.stringify({}),
      JSON.stringify({
        corpus_kinds: ["paper"],
        has_evidence: ["true"],
        q: [queryText],
        review_levels: ["full_text_reviewed"],
      }),
    ]),
  );

  const detailRecords = ledger.records.filter((record) => record.pathname.startsWith("/api/evidence/items/"));
  expect(detailRecords).toHaveLength(1);
  expect(detailRecords[0]).toMatchObject({
    method: "GET",
    pathname: `/api/evidence/items/${encodeURIComponent(paperItem.resultId)}`,
    query: {},
    responseClass: "mock-200",
    expected: true,
  });
  expect(ledger.mutationRequests).toEqual([]);
  expect(ledger.unexpectedApiRequests).toEqual([]);
  expect(ledger.offOriginRequests).toEqual([]);
  expect(ledger.sensitiveEndpointRequests).toEqual([]);
  expect(browserErrors).toEqual([]);
});
