import { expect, test, type Request, type Response } from "@playwright/test";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import type { EvidenceItemDetailResponse } from "../../src/lib/api";

const appOrigin = "http://127.0.0.1:8002";
const paperTitle = "Synthetic Spatial Computing Provenance";
const canonicalPaperId = "2601.00001";
const runId = "evidence-browser-real-backend-run";
const topicId = "spatial-computing";
const topicName = "Spatial Computing";
const runApiPath = `/topics/${topicId}/runs/${runId}`;
const runBrowserHref = `/app${runApiPath}`;
const fabricatedReportHref = `/app/reports/${runId}`;
const reportTitle = "Spatial Computing Evidence Findings";
const portableArtifactRef = "run:raw/2601.00001v1.txt";

type QueryRecord = Record<string, string[]>;

type RequestRecord = {
  method: string;
  origin: string;
  pathname: string;
  query: QueryRecord;
};

type ResponseRecord = RequestRecord & {
  status: number;
};

type FailureRecord = {
  origin: string;
  pathname: string;
  failureClass: string;
};

type NetworkLedger = {
  requests: RequestRecord[];
  responses: ResponseRecord[];
  failures: FailureRecord[];
  evidencePayloads: unknown[];
  offOriginAttempts: Array<{ scheme: string; host: string; pathname: string }>;
  sameOriginIntercepted: number;
  mutationRequests: string[];
  forbiddenRequests: string[];
};

function normalizeQuery(url: URL): QueryRecord {
  return Object.fromEntries(
    [...new Set(url.searchParams.keys())]
      .sort()
      .map((key) => [key, url.searchParams.getAll(key)]),
  );
}

function requestRecord(request: Request): RequestRecord {
  const url = new URL(request.url());
  return {
    method: request.method(),
    origin: url.origin,
    pathname: url.pathname,
    query: normalizeQuery(url),
  };
}

function responseRecord(response: Response): ResponseRecord {
  return { ...requestRecord(response.request()), status: response.status() };
}

function isExpectedApplicationPath(pathname: string) {
  return (
    pathname.startsWith("/app/") ||
    pathname === "/app" ||
    pathname === "/auth/me" ||
    pathname === "/api/topics" ||
    pathname === "/api/evidence/search" ||
    pathname.startsWith("/api/evidence/items/") ||
    pathname === "/health"
  );
}

function isForbiddenFamily(pathname: string) {
  return /\/(?:oauth|mcp|providers?|tunnel)(?:\/|$)|\/\.well-known(?:\/|$)/i.test(pathname);
}

function isApiRead(pathname: string) {
  return pathname.startsWith("/api/") || pathname.startsWith("/auth/");
}

function countRequests(ledger: NetworkLedger, pathname: string, predicate?: (record: RequestRecord) => boolean) {
  return ledger.requests.filter(
    (record) => record.origin === appOrigin && record.pathname === pathname && (!predicate || predicate(record)),
  ).length;
}

function expectBoundedRead(count: number, label: string) {
  expect(count, `${label} must be one or two identical StrictMode-safe GETs`).toBeGreaterThanOrEqual(1);
  expect(count, `${label} must be one or two identical StrictMode-safe GETs`).toBeLessThanOrEqual(2);
}

function assertNoSensitivePayload(payloads: unknown[]) {
  const serialized = JSON.stringify(payloads);
  for (const forbidden of [
    "C:\\Users\\",
    "D:\\dev\\repos\\",
    "/home/",
    ".env",
    ".mcp.json",
    "BEGIN PRIVATE KEY",
    "Bearer ",
    "raw provider output",
  ]) {
    expect(serialized).not.toContain(forbidden);
  }
}

test("uses the real local backend to search evidence and inspect provenance", async ({ page, context }) => {
  const source = fs.readFileSync(fileURLToPath(import.meta.url), "utf8");
  const forbiddenSourceTokens = [
    [".", "fulfill", "("].join(""),
    ["install", "Evidence", "Mocks"].join(""),
    ["fake", "Auth"].join(""),
    ["fake", "Topics"].join(""),
    ["fake", "Search"].join(""),
    ["fake", "Detail"].join(""),
  ];
  for (const token of forbiddenSourceTokens) {
    expect(source, `real-backend spec must not contain ${token}`).not.toContain(token);
  }

  const ledger: NetworkLedger = {
    requests: [],
    responses: [],
    failures: [],
    evidencePayloads: [],
    offOriginAttempts: [],
    sameOriginIntercepted: 0,
    mutationRequests: [],
    forbiddenRequests: [],
  };
  const browserErrors: string[] = [];
  const responseTasks: Array<Promise<void>> = [];
  const offOriginPattern = new RegExp(`^(?!${appOrigin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:/|$))https?://`);

  await context.route(offOriginPattern, async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === appOrigin) {
      ledger.sameOriginIntercepted += 1;
    }
    ledger.offOriginAttempts.push({ scheme: url.protocol, host: url.host, pathname: url.pathname });
    await route.abort("blockedbyclient");
  });

  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("request", (request) => {
    const record = requestRecord(request);
    ledger.requests.push(record);
    if (["POST", "PUT", "PATCH", "DELETE"].includes(record.method)) {
      ledger.mutationRequests.push(`${record.method} ${record.pathname}`);
    }
    if (record.origin === appOrigin && isForbiddenFamily(record.pathname)) {
      ledger.forbiddenRequests.push(`${record.method} ${record.pathname}`);
    }
    if (record.origin === appOrigin && isApiRead(record.pathname) && !isExpectedApplicationPath(record.pathname)) {
      ledger.forbiddenRequests.push(`${record.method} ${record.pathname}`);
    }
  });
  page.on("requestfailed", (request) => {
    const url = new URL(request.url());
    ledger.failures.push({
      origin: url.origin,
      pathname: url.pathname,
      failureClass: request.failure()?.errorText.split(":", 1)[0] ?? "unknown",
    });
  });
  page.on("response", (response) => {
    const task = (async () => {
      const record = responseRecord(response);
      ledger.responses.push(record);
      if (
        record.origin === appOrigin &&
        (record.pathname === "/api/evidence/search" || record.pathname.startsWith("/api/evidence/items/"))
      ) {
        ledger.evidencePayloads.push(await response.json());
      }
    })();
    responseTasks.push(task);
  });

  const authResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/auth/me");
  const topicsResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/topics");
  const initialSearchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/evidence/search" && [...url.searchParams.keys()].length === 0;
  });

  await page.goto("/app/provenance");
  const initialResponses = await Promise.all([authResponse, topicsResponse, initialSearchResponse]);
  expect(initialResponses.map((response) => response.status())).toEqual([200, 200, 200]);
  await expect(page).toHaveURL(`${appOrigin}/app/provenance`);
  await expect(page.getByRole("heading", { name: "근거·출처 탐색", level: 1 })).toBeVisible();
  await expect(page.getByText(/로컬·내부용 읽기 전용 탐색입니다/)).toBeVisible();

  const reportCard = page.getByRole("article").filter({ hasText: reportTitle });
  await expect(reportCard.getByRole("heading", { name: reportTitle })).toBeVisible();
  await expect(reportCard.getByText("독립 보고서 섹션", { exact: true })).toBeVisible();
  await expect(reportCard.getByText("보고서-논문 연결이 인덱싱되지 않음", { exact: true })).toBeVisible();

  await page.getByLabel("검색어").fill("spatial");
  await page.getByLabel("토픽").selectOption(topicId);
  const corpusGroup = page.getByRole("group", { name: "코퍼스 종류" });
  await corpusGroup.getByRole("checkbox", { name: "논문" }).check();
  await page.getByText("고급 검색 조건", { exact: true }).click();
  await page.getByRole("group", { name: "검토 수준" }).getByRole("checkbox", { name: "본문 검토됨" }).check();
  await page.getByLabel("세부 근거").selectOption("true");
  expect(ledger.requests.filter((record) => record.pathname.startsWith("/api/evidence/items/"))).toHaveLength(0);

  const filteredResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/evidence/search" && url.searchParams.get("q") === "spatial";
  });
  await page.getByRole("button", { name: "검색", exact: true }).click();
  expect((await filteredResponse).status()).toBe(200);

  const currentUrl = new URL(page.url());
  expect(currentUrl.pathname).toBe("/app/provenance");
  expect(Object.fromEntries(currentUrl.searchParams.entries())).toEqual({
    q: "spatial",
    topic_ids: topicId,
    corpus_kinds: "paper",
    review_levels: "full_text_reviewed",
    has_evidence: "true",
  });

  const paperCard = page.getByRole("article").filter({ hasText: paperTitle });
  await expect(paperCard.getByRole("heading", { name: paperTitle })).toBeVisible();
  await expect(paperCard.getByText(`정규 ID ${canonicalPaperId}`, { exact: true })).toBeVisible();
  await expect(paperCard.getByText("본문 검토됨", { exact: true })).toBeVisible();
  await expect(paperCard.getByText(/inline span 1 · run evidence 1/)).toBeVisible();
  await expect(paperCard.getByText("발생 1건", { exact: true })).toBeVisible();
  await paperCard.getByText("발생 위치 1건", { exact: true }).click();
  await expect(paperCard.getByText(new RegExp(`${runId}.*spatial computing`))).toBeVisible();

  const detailButton = paperCard.getByRole("button", { name: `${paperTitle} 출처 상세` });
  const detailResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname.startsWith("/api/evidence/items/"),
  );
  await detailButton.click();
  const detailHttpResponse = await detailResponse;
  expect(detailHttpResponse.status()).toBe(200);
  const detailPayload = (await detailHttpResponse.json()) as EvidenceItemDetailResponse;
  const topicNode = detailPayload.provenanceNodes.find(
    (node) => node.nodeKind === "topic" && node.stableId === topicId,
  );
  expect(topicNode).toEqual(
    expect.objectContaining({
      nodeKind: "topic",
      stableId: topicId,
      topicId,
      label: topicName,
    }),
  );
  const runNode = detailPayload.provenanceNodes.find(
    (node) => node.nodeKind === "run" && node.stableId === runId,
  );
  expect(runNode).toEqual(
    expect.objectContaining({
      nodeKind: "run",
      stableId: runId,
      runId,
    }),
  );
  expect(detailPayload.occurrences).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        runId,
        topicId,
        topicName,
      }),
    ]),
  );
  expect(detailPayload.links.run).toBe(runApiPath);
  expect(detailPayload.links.report).toBeUndefined();

  const detailPanel = page.getByRole("complementary", { name: `${paperTitle} 출처 상세` });
  const detailHeading = detailPanel.getByRole("heading", { name: `${paperTitle} 출처 상세`, level: 2 });
  await expect(detailHeading).toBeFocused();
  await expect(detailPanel.getByText(topicName, { exact: true }).first()).toBeVisible();
  await expect(detailPanel.getByText(runId, { exact: true }).first()).toBeVisible();
  await expect(detailPanel.getByText(canonicalPaperId, { exact: true }).first()).toBeVisible();
  for (const truthLabel of ["영속 기록", "인덱스 투영", "휴대형 참조", "결정론적 연결"]) {
    await expect(detailPanel.getByText(truthLabel, { exact: true }).first()).toBeVisible();
  }
  await expect(detailPanel.getByText(portableArtifactRef, { exact: true }).first()).toBeVisible();
  await expect(detailPanel.getByText("참조 해시 일치", { exact: true }).first()).toBeVisible();

  const detailLinks = detailPanel.getByRole("navigation", { name: "상세 내부 링크" });
  const topicLink = detailLinks.getByRole("link", { name: "토픽" });
  await expect(topicLink).toBeVisible();
  await expect(topicLink).toHaveAttribute("href", `/app/topics/${topicId}/papers`);
  const renderedRunHref = await detailLinks.getByRole("link", { name: "Run" }).getAttribute("href");
  expect(renderedRunHref).toBe(runBrowserHref);
  await expect(detailLinks.getByRole("link", { name: "보고서" })).toHaveCount(0);
  await expect(detailPanel.locator(`a[href="${fabricatedReportHref}"]`)).toHaveCount(0);
  expect(await detailPanel.textContent()).not.toMatch(/C:\\Users\\|D:\\dev\\repos\\|\/home\//);
  await expect(detailPanel.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Provider.*실행|검토.*생성|보고서.*재생성/ })).toHaveCount(0);

  await page.keyboard.press("Escape");
  await expect(detailPanel).toHaveCount(0);
  await expect(detailButton).toBeFocused();
  await detailButton.click();
  await expect(page.getByRole("complementary", { name: `${paperTitle} 출처 상세` })).toBeVisible();
  await expect.poll(() => ledger.requests.filter((record) => record.pathname.startsWith("/api/evidence/items/")).length).toBe(1);
  await page.keyboard.press("Escape");

  await page.waitForLoadState("networkidle");
  await Promise.all(responseTasks);
  expectBoundedRead(countRequests(ledger, "/auth/me"), "/auth/me");
  expectBoundedRead(countRequests(ledger, "/api/topics"), "/api/topics");
  expectBoundedRead(
    countRequests(ledger, "/api/evidence/search", (record) => Object.keys(record.query).length === 0),
    "initial evidence browse",
  );
  expectBoundedRead(
    countRequests(ledger, "/api/evidence/search", (record) => record.query.q?.[0] === "spatial"),
    "filtered evidence search",
  );
  expect(ledger.requests.filter((record) => record.pathname.startsWith("/api/evidence/items/"))).toHaveLength(1);
  expect(ledger.responses.filter((record) => isApiRead(record.pathname)).every((record) => record.status === 200)).toBe(true);
  expect(ledger.failures.filter((record) => record.origin === appOrigin)).toEqual([]);
  expect(ledger.sameOriginIntercepted).toBe(0);
  expect(ledger.offOriginAttempts).toEqual([]);
  expect(ledger.mutationRequests).toEqual([]);
  expect(ledger.forbiddenRequests).toEqual([]);
  expect(browserErrors).toEqual([]);
  assertNoSensitivePayload(ledger.evidencePayloads);

  console.log(
    `XRML_REAL_BACKEND_LEDGER=${JSON.stringify({
      authReads: countRequests(ledger, "/auth/me"),
      topicReads: countRequests(ledger, "/api/topics"),
      browseReads: countRequests(ledger, "/api/evidence/search", (record) => Object.keys(record.query).length === 0),
      filteredReads: countRequests(ledger, "/api/evidence/search", (record) => record.query.q?.[0] === "spatial"),
      detailReads: ledger.requests.filter((record) => record.pathname.startsWith("/api/evidence/items/")).length,
      sameOriginIntercepted: ledger.sameOriginIntercepted,
      offOriginAttempts: ledger.offOriginAttempts.length,
      mutationRequests: ledger.mutationRequests.length,
      forbiddenRequests: ledger.forbiddenRequests.length,
      sameOriginFailures: ledger.failures.filter((record) => record.origin === appOrigin).length,
    })}`,
  );
});
