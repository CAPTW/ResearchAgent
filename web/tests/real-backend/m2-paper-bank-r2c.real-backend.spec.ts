import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import type { Page, TestInfo } from "@playwright/test";
import {
  appOrigin, armDetailResponse, armPaperBankResponse, assertHygiene, expect,
  getPaperBank, installLedger, remainingBudget, test, topicA, versionTwoFrom,
  withinDeadline, type BrowserLedger, type OwnedBackend, type RemainingBudget,
} from "./m2-paper-bank.real-backend.helpers";

const globalRole = "FEDERATED_GLOBAL_SEARCH_AND_PARSER_DERIVED_SEARCH_HELP";
const languageRole = "TOPIC_RESEARCH_LANGUAGE_POLICY_AND_ACQUISITION_TIME_WINDOW";
const timeRole = "TIME_FORMAT_PERSISTENCE_AND_FINAL_M2_MAPPING_TRUTH";
const topicRoute = "/app/topics/" + topicA;
const profilePath = "/topic-profiles/" + topicA;
const configurationPath = "/api/topics/" + topicA + "/configuration";
type SearchItem = {
  kind: string; resultId: string; topicId: string; paperVersionId: string | null;
  evidenceId: string | null; runId: string | null; primaryLabel: string;
  secondaryText: string; route: string; updatedAt: string | null;
};
type SearchPayload = {
  query: { raw: string; normalized: string; tokens: string[] };
  items: SearchItem[]; sourceStates: Record<string, string>;
  unavailableKinds: Record<string, string>; queryDigest: string;
};
function escapeRegex(value: string) { return value.replace(/[|\\{}()[\]^+*?.]/g, "\\$&"); }
function seedSearchProjection(backend: OwnedBackend) {
  const script = [
    "import json",
    "from app.repositories import metadata_index",
    "from app.db.bootstrap import connect_metadata_index",
    "ids=['m2-paper-bank-topic-a-run-1','m2-paper-bank-topic-a-run-2','m2-paper-bank-topic-b-run-1']",
    "result=[metadata_index.index_run(run) for run in ids]",
    "with connect_metadata_index() as connection:",
    " row=connection.execute(\"SELECT * FROM paper_reviews WHERE topic_id=? AND paper_id=?\", ('m2-spatial-paper-bank','2608.00001v2')).fetchone()",
    " span=next(item for item in json.loads(row['evidence_spans_json']) if item['evidence_span_id']=='m2-evidence-v2-001')",
    " connection.execute(\"INSERT INTO evidence (run_id,evidence_id,source_id,paper_id,title,source_type,quality_hint,confidence,artifact_path,citation_text,snippet) VALUES (?,?,?,?,?,?,?,?,?,?,?)\", (row['run_id'],span['evidence_span_id'],row['paper_id'],row['paper_id'],row['title'],'synthetic_fixture','fixture',0.9,row['full_text_artifact_path'],row['title'],span['text']))",
    "print(json.dumps(result))",
  ].join("\n");
  const result = spawnSync(backend.pythonExe, ["-B", "-c", script], {
    cwd: backend.sourceRoot, windowsHide: true, encoding: "utf8",
    env: { ...process.env, XRML_DATA_DIR: backend.dataRoot, TEMP: backend.tempRoot, TMP: backend.tempRoot, TMPDIR: backend.tempRoot, AUTH_MODE: "disabled", TOPIC_SCHEDULER_ENABLED: "0" },
  });
  expect(result.status, result.stderr).toBe(0);
  fs.writeFileSync(path.join(backend.root, "search-fixture-projection.json"), result.stdout);
}
function runIdentities(backend: OwnedBackend) {
  const storage = path.join(backend.dataRoot, "runs");
  return fs.readdirSync(storage).sort().flatMap((id) => {
    const file = path.join(storage, id, "run.json");
    return fs.existsSync(file) ? [{ id, sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex") }] : [];
  });
}
async function profile(page: Page) {
  const response = await page.request.get("/topic-profiles");
  expect(response.ok()).toBe(true);
  const items = (await response.json()).items as Array<Record<string, unknown>>;
  const matches = items.filter((item) => item.profile_id === topicA);
  expect(matches).toHaveLength(1);
  return matches[0]!;
}
async function settingsReady(page: Page, remaining: RemainingBudget) {
  await withinDeadline(remaining)(page.locator(".v2-app-shell")).toBeVisible();
  await withinDeadline(remaining)(page.locator('[data-settings-ready="true"]')).toHaveCount(1);
  await withinDeadline(remaining)(page.locator(".v2-settings-screen")).toBeVisible();
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "테마", exact: true })).toHaveCount(1);
  await withinDeadline(remaining)(page.getByRole("heading", { name: "주제 기본 설정", exact: true })).toBeVisible();
}
async function papersFromSettings(page: Page, remaining: RemainingBudget) {
  const before = await page.evaluate(() => performance.timeOrigin);
  const response = armPaperBankResponse(page, topicA, remaining);
  const link = page.getByRole("navigation", { name: "주제 화면", exact: true }).getByRole("link", { name: "논문 목록", exact: true });
  await withinDeadline(remaining)(link).toHaveCount(1);
  await link.click();
  expect((await response).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL(appOrigin + topicRoute + "/papers");
  await withinDeadline(remaining)(page.getByRole("region", { name: "논문 목록 화면", exact: true })).toBeVisible();
  await withinDeadline(remaining)(page.getByRole("table", { name: "논문 목록", exact: true })).toBeVisible();
  await withinDeadline(remaining)(page.locator("[data-settings-ready]")).toHaveCount(0);
  await withinDeadline(remaining)(page.locator(".v2-settings-screen")).toHaveCount(0);
  await withinDeadline(remaining)(page.getByRole("navigation", { name: "주제 화면", exact: true }).locator('[aria-current="page"]')).toHaveCount(1);
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(before);
}
async function search(page: Page, raw: string, remaining: RemainingBudget): Promise<SearchPayload> {
  const box = page.getByRole("searchbox", { name: "주제, 키워드, 논문 검색", exact: true });
  await withinDeadline(remaining)(box).toHaveCount(1);
  const response = page.waitForResponse((item) => {
    const url = new URL(item.url());
    return item.request().method() === "GET" && url.pathname === "/api/search" && url.searchParams.get("q") === raw.trim();
  }, { timeout: remaining() });
  await box.fill(raw);
  await box.focus();
  const result = await response;
  expect(result.status()).toBe(200);
  const payload = await result.json() as SearchPayload;
  const region = page.getByRole("region", { name: "전역 검색 결과", exact: true });
  await withinDeadline(remaining)(region).toBeVisible();
  await withinDeadline(remaining)(region.getByRole("button")).toHaveCount(payload.items.length);
  if (!payload.items.length) await withinDeadline(remaining)(region.getByRole("status")).toHaveText("일치하는 결과가 없습니다.");
  return payload;
}
async function selectResult(page: Page, item: SearchItem, remaining: RemainingBudget) {
  // Secondary text includes the native identity; equal titles alone never select a result.
  const button = page.getByRole("region", { name: "전역 검색 결과", exact: true })
    .getByRole("button", { name: new RegExp("^" + escapeRegex(item.primaryLabel + " " + item.kind + " " + item.secondaryText)) });
  await withinDeadline(remaining)(button).toHaveCount(1);
  const origin = await page.evaluate(() => performance.timeOrigin);
  const detailResponse = item.paperVersionId && ["paper", "evidence"].includes(item.kind)
    ? armDetailResponse(page, item.topicId, item.paperVersionId, remaining) : null;
  await button.click();
  await withinDeadline(remaining)(page).toHaveURL(appOrigin + "/app" + item.route);
  await withinDeadline(remaining)(page.locator(".v2-app-shell")).toBeVisible();
  expect((await page.locator("body").innerText()).trim().length).toBeGreaterThan(0);
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(origin);
  if (detailResponse) {
    const response = await detailResponse;
    expect(response.ok()).toBe(true);
    const payload = await response.json();
    expect(payload.paperVersionId).toBe(item.paperVersionId);
    const detail = page.getByRole("region", { name: "논문 상세", exact: true });
    await withinDeadline(remaining)(detail).toBeVisible();
    await withinDeadline(remaining)(detail.getByText(item.paperVersionId!, { exact: true })).toBeVisible();
    if (item.evidenceId) {
      const target = detail.locator('[data-evidence-id="' + item.evidenceId + '"]');
      await withinDeadline(remaining)(target).toBeVisible();
      await withinDeadline(remaining)(target).toHaveClass(/is-evidence-target/);
    }
  }
}
async function finish(backend: OwnedBackend, ledger: BrowserLedger, initialRuns: ReturnType<typeof runIdentities>,
  role: string, assertions: Record<string, boolean | number | string>, startedAt: number, testInfo: TestInfo) {
  expect(runIdentities(backend)).toEqual(initialRuns);
  expect(initialRuns.length).toBe(3);
  expect(ledger.mutationRequests.filter((item) =>
    !/^PATCH \/topic-profiles\//.test(item) && !/^PUT \/api\/topics\/[^/]+\/configuration$/.test(item)
    && !/^PUT \/topic-notifications\/settings\//.test(item))).toEqual([]);
  await assertHygiene(ledger);
  fs.writeFileSync(path.join(backend.root, "r2c-request-ledger.json"), JSON.stringify(ledger, null, 2) + "\n");
  backend.writeJourneyResult({
    journey: role, migrationIds: [role],
    assertions: { ...assertions, unchangedFixtureRuns: true, providerCalls: 0, retrievalCalls: 0, automaticRuns: 0 },
    timing: { completeMs: Date.now() - startedAt, guardMs: testInfo.timeout, guardIsProductSla: false },
    hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length },
  });
}

test("M2 R2C · FEDERATED_GLOBAL_SEARCH_AND_PARSER_DERIVED_SEARCH_HELP", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now(); const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page); const initialRuns = runIdentities(backend);
  seedSearchProjection(backend);
  await page.goto(topicRoute + "/dashboard", { waitUntil: "domcontentloaded", timeout: remaining() });
  await withinDeadline(remaining)(page.getByRole("heading", { name: "M2 Spatial Paper Bank", exact: true })).toBeVisible();
  const all = await search(page, "ＳＰＡＴＩＡＬ", remaining);
  expect(all.query.normalized).toBe("spatial"); expect(all.query.tokens).toEqual(["spatial"]);
  const observedKinds = new Set(all.items.map((item) => item.kind));
  expect(observedKinds).toEqual(new Set(["topic", "paper"]));
  expect(new Set(all.items.map((item) => item.resultId)).size).toBe(all.items.length);
  for (const kind of observedKinds) {
    await withinDeadline(remaining)(page.getByRole("region", { name: "전역 검색 결과", exact: true })
      .getByRole("button").filter({ has: page.getByText(kind, { exact: true }) })).not.toHaveCount(0);
  }
  const repeat = await page.request.get("/api/search", { params: { q: "  spatial  ", limit: 12 } });
  expect(repeat.ok()).toBe(true); const equivalent = await repeat.json() as SearchPayload;
  expect(equivalent.queryDigest).toBe(all.queryDigest);
  expect(equivalent.items.map((item) => item.resultId)).toEqual(all.items.map((item) => item.resultId));
  const collision = await search(page, "A Similar Paper Title", remaining);
  const pair = collision.items.filter((item) => item.kind === "paper" && item.primaryLabel === "A Similar Paper Title");
  expect(pair).toHaveLength(2);
  for (const key of ["resultId", "paperVersionId", "route"] as const) expect(new Set(pair.map((item) => item[key])).size).toBe(2);
  for (const item of pair) { await selectResult(page, item, remaining); await search(page, "A Similar Paper Title", remaining); }
  const evidenceResults = await search(page, "m2-evidence-v2-001", remaining);
  for (const item of evidenceResults.items) observedKinds.add(item.kind);
  const evidence = evidenceResults.items.filter((item) => item.kind === "evidence" && item.evidenceId === "m2-evidence-v2-001");
  expect(evidence).toHaveLength(1); await selectResult(page, evidence[0]!, remaining);
  const runId = "m2-paper-bank-topic-a-run-2";
  const runs = (await search(page, runId, remaining)).items.filter((item) => item.kind === "run" && item.runId === runId);
  for (const item of runs) observedKinds.add(item.kind);
  expect(runs).toHaveLength(1); await selectResult(page, runs[0]!, remaining);
  const topics = (await search(page, topicA, remaining)).items.filter((item) => item.kind === "topic" && item.topicId === topicA);
  expect(topics).toHaveLength(1); await selectResult(page, topics[0]!, remaining);
  expect(observedKinds).toEqual(new Set(["topic", "paper", "evidence", "run"]));
  await search(page, "spatial", remaining);
  const helpResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/search/help", { timeout: remaining() });
  const helpLink = page.getByRole("link", { name: "검색 도움말 보기", exact: true });
  await withinDeadline(remaining)(helpLink).toHaveCount(1); await helpLink.click();
  const helpResult = await helpResponse; expect(helpResult.ok()).toBe(true); const help = await helpResult.json();
  await withinDeadline(remaining)(page).toHaveURL(appOrigin + "/app/evidence?view=search-help");
  const helpRegion = page.getByRole("region", { name: "검색 도움말", exact: true });
  await withinDeadline(remaining)(helpRegion).toBeVisible();
  await withinDeadline(remaining)(helpRegion.getByText("Search Help · parser-derived", { exact: true })).toBeVisible();
  expect(help.normalization).toEqual({ unicode: "NFKC", case: "casefold", controls: "space", whitespace: "collapse_and_trim" });
  expect(help.limits).toEqual({ maxCharacters: 200, maxTokens: 8 });
  for (const syntax of ["quotes", "comma", "boolean", "wildcard", "field"]) {
    expect(help.syntax[syntax]).toEqual({ status: "literal", interpretation: "literal" });
    const row = helpRegion.locator("dl > div").filter({ has: page.getByText(syntax, { exact: true }) });
    await withinDeadline(remaining)(row).toHaveCount(1); await withinDeadline(remaining)(row).toContainText("literal · literal");
  }
  for (const raw of ['"Spatial Evidence"', "Spatial,Evidence", "Spatial AND Evidence", "Spatial*", 'title:"Spatial"']) {
    const response = await page.request.get("/api/search", { params: { q: raw } }); expect(response.status()).toBe(200);
    const result = await response.json() as SearchPayload;
    const literal = raw.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
    expect(result.query.normalized).toBe(literal); expect(result.query.tokens).toEqual(literal.split(" "));
  }
  const empty = await search(page, "r2c-no-such-fixture-9f723b", remaining);
  expect(empty.items).toEqual([]); expect(empty.unavailableKinds).toEqual({});
  const unavailable = await page.request.get("/api/search", { params: { q: "spatial", kinds: "citation" } });
  expect(unavailable.status()).toBe(200);
  expect((await unavailable.json()).unavailableKinds).toEqual({ citation: "UNAVAILABLE_WITH_REASON_CITATION_OBSERVATION_AUTHORITY_NOT_IMPLEMENTED" });
  const invalid = await page.request.get("/api/search", { params: { q: "x".repeat(201) } }); expect(invalid.status()).toBe(422);
  expect(ledger.mutationRequests).toEqual([]);
  await finish(backend, ledger, initialRuns, globalRole, {
    fourTypedKinds: true, normalizedStableIdentities: true, sameLabelDistinctPaperVersions: true,
    exactTopicPaperEvidenceRunDestinations: true, realParserDerivedHelp: true,
    allUnsupportedSyntaxLiteral: true, emptyUnavailableErrorDistinct: true, readOnlySearch: true,
  }, startedAt, testInfo);
});

test("M2 R2C · TOPIC_RESEARCH_LANGUAGE_POLICY_AND_ACQUISITION_TIME_WINDOW", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now(); const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page); const initialRuns = runIdentities(backend); const before = await profile(page);
  await page.goto(topicRoute + "/settings", { waitUntil: "domcontentloaded", timeout: remaining() }); await settingsReady(page, remaining);
  const settings = page.locator(".v2-settings-screen");
  const acquisition = settings.getByRole("combobox", { name: "수집 언어", exact: true });
  const report = settings.getByRole("combobox", { name: "보고서 출력 언어", exact: true });
  const start = settings.getByLabel("수집 시작일", { exact: true }); const end = settings.getByLabel("수집 종료일", { exact: true });
  for (const control of [acquisition, report, start, end]) await withinDeadline(remaining)(control).toHaveCount(1);
  const cadence = settings.getByRole("combobox", { name: "수집 주기", exact: true }); const reportCadence = settings.getByRole("combobox", { name: "보고 주기", exact: true });
  await withinDeadline(remaining)(cadence).toHaveCount(1); await withinDeadline(remaining)(reportCadence).toHaveCount(1);
  await withinDeadline(remaining)(cadence).toHaveValue(String(before.default_collection_cadence));
  await withinDeadline(remaining)(reportCadence).toHaveValue(String(before.default_report_cadence));
  await acquisition.selectOption("ko"); await report.selectOption("en-US");
  await start.fill("2026-01-01"); await end.fill("2026-08-31");
  const savedProfile = page.waitForResponse((response) => response.request().method() === "PATCH" && new URL(response.url()).pathname === profilePath, { timeout: remaining() });
  const savedConfig = page.waitForResponse((response) => response.request().method() === "PUT" && new URL(response.url()).pathname === configurationPath, { timeout: remaining() });
  await settings.getByRole("button", { name: "지원 설정 저장", exact: true }).click();
  expect((await savedProfile).ok()).toBe(true);
  const configurationSave = await savedConfig; expect(configurationSave.ok()).toBe(true);
  expect((await configurationSave.json()).validationStatus).toBe("saved");
  await withinDeadline(remaining)(page.getByRole("status").filter({ hasText: "주제 설정을 저장했습니다. 연구 작업은 실행하지 않았습니다." })).toBeVisible();
  await settingsReady(page, remaining);
  await page.reload({ waitUntil: "domcontentloaded", timeout: remaining() }); await settingsReady(page, remaining);
  await withinDeadline(remaining)(acquisition).toHaveValue("ko"); await withinDeadline(remaining)(report).toHaveValue("en-US");
  await withinDeadline(remaining)(start).toHaveValue("2026-01-01"); await withinDeadline(remaining)(end).toHaveValue("2026-08-31");
  const persisted = await profile(page);
  expect(persisted).toMatchObject({
    profile_id: topicA, acquisition_language: "ko", review_report_language: "en-US",
    acquisition_start_date: "2026-01-01", acquisition_end_date: "2026-08-31",
    default_collection_cadence: before.default_collection_cadence, default_report_cadence: before.default_report_cadence,
  });
  await withinDeadline(remaining)(cadence).toHaveValue(String(before.default_collection_cadence));
  const configuration = await page.request.get(configurationPath); expect(configuration.ok()).toBe(true);
  expect((await configuration.json()).topicConfiguration).toMatchObject({
    acquisitionLanguage: "ko", reviewReportLanguage: "en-US", acquisitionStartDate: "2026-01-01", acquisitionEndDate: "2026-08-31",
  });
  const inverted = await page.request.patch(profilePath, { data: { acquisition_start_date: "2026-09-01", acquisition_end_date: "2026-01-01" } });
  expect(inverted.status()).toBe(400); expect((await inverted.json()).detail).toContain("must not be later");
  expect(await profile(page)).toEqual(persisted);
  const malformed = await page.request.patch(profilePath, { data: { acquisition_language: "invalid value!" } });
  expect(malformed.status()).toBe(400); expect(await profile(page)).toEqual(persisted);
  await withinDeadline(remaining)(start).toHaveValue("2026-01-01"); await withinDeadline(remaining)(end).toHaveValue("2026-08-31");
  await papersFromSettings(page, remaining);
  await finish(backend, ledger, initialRuns, languageRole, {
    independentLanguageFieldsPersisted: true, validWindowPersisted: true, cadencePreserved: true,
    reloadPersistence: true, exactProfileConfigurationAgreement: true,
    invertedAndMalformedRejectedWithoutMutation: true, sameTabSettingsToPapers: true, staleSettingsReadinessAbsent: true,
  }, startedAt, testInfo);
});

test("M2 R2C · TIME_FORMAT_PERSISTENCE_AND_FINAL_M2_MAPPING_TRUTH", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now(); const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page); const initialRuns = runIdentities(backend);
  const dashboardResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/topics/" + topicA + "/dashboard", { timeout: remaining() });
  await page.goto(topicRoute + "/settings", { waitUntil: "domcontentloaded", timeout: remaining() }); await settingsReady(page, remaining);
  const dashboard = await (await dashboardResponse).json();
  const instant = dashboard.topic.updatedAt || (await profile(page)).updated_at; expect(typeof instant).toBe("string");
  const control = page.getByRole("combobox", { name: "시간 표시 형식", exact: true }); await withinDeadline(remaining)(control).toHaveCount(1);
  expect(await control.locator("option").evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value))).toEqual(["locale", "12h", "24h"]);
  const headerTime = page.getByRole("button", { name: /^최근 업데이트:/ }); await withinDeadline(remaining)(headerTime).toHaveCount(1);
  for (const mode of ["locale", "12h", "24h"]) {
    await control.selectOption(mode);
    const expected = await page.evaluate(({ value, format }) => new Date(value).toLocaleString("ko-KR", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      hour12: format === "12h" ? true : format === "24h" ? false : undefined,
    }), { value: String(instant), format: mode });
    await withinDeadline(remaining)(headerTime).toContainText(expected);
    if (mode === "12h") expect(expected).toMatch(/오전|오후/);
    if (mode === "24h") expect(expected).not.toMatch(/오전|오후/);
    expect((await page.evaluate(() => JSON.parse(localStorage.getItem("xrml.appearance.v1") ?? "{}"))).timeFormat).toBe(mode);
  }
  const cookie = (await context.cookies()).filter((item) => item.name === "xrml_appearance_v1"); expect(cookie).toHaveLength(1);
  expect(JSON.parse(decodeURIComponent(cookie[0]!.value)).timeFormat).toBe("24h");
  await page.reload({ waitUntil: "domcontentloaded", timeout: remaining() }); await settingsReady(page, remaining);
  await withinDeadline(remaining)(control).toHaveValue("24h"); await papersFromSettings(page, remaining);
  const versionTwo = versionTwoFrom(await getPaperBank(page));
  const row = page.getByRole("row").filter({ has: page.getByText("2608.00001v2", { exact: true }) });
  await withinDeadline(remaining)(row).toHaveCount(1);
  const dateCell = row.getByRole("cell").filter({ hasText: /^2026 · 2026/ }); await withinDeadline(remaining)(dateCell).toHaveCount(1);
  expect(await dateCell.innerText()).not.toMatch(/\d{1,2}:\d{2}|오전|오후|AM|PM/);
  const detailResponse = armDetailResponse(page, topicA, versionTwo.paperVersionId, remaining);
  await row.getByRole("button", { name: versionTwo.title + " 상세 열기", exact: true }).click();
  const detailPayload = await (await detailResponse).json(); expect(detailPayload.review.updatedAt).toBeTruthy();
  const expectedDetail = await page.evaluate((value) => new Date(value).toLocaleString("ko-KR", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }), detailPayload.review.updatedAt);
  const detail = page.getByRole("region", { name: "논문 상세", exact: true });
  const reviewed = detail.locator("dl > div").filter({ has: page.getByText("Review updated", { exact: true }) });
  await withinDeadline(remaining)(reviewed).toContainText(expectedDetail);
  await detail.getByRole("button", { name: "논문 상세 닫기", exact: true }).click();
  const facets = await (await page.request.get("/api/topics/" + topicA + "/paper-bank")).json();
  const review = page.getByRole("combobox", { name: "리뷰 상태", exact: true }); await withinDeadline(remaining)(review).toBeEnabled();
  const reviewResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank" && url.searchParams.get("reviewState") === "full_text_reviewed";
  }, { timeout: remaining() });
  await review.selectOption("full_text_reviewed"); expect((await reviewResponse).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL((url) => url.searchParams.get("review") === "full_text_reviewed");
  for (const [name, id] of [["인용수 필터", "citation"], ["영향력 필터", "impact"]]) {
    const unavailable = facets.unsupportedFilters.filter((item: { id: string }) => item.id === id); expect(unavailable).toHaveLength(1);
    expect(unavailable[0].state).toBe("unavailable");
    const disabled = page.getByRole("combobox", { name, exact: true }); await withinDeadline(remaining)(disabled).toHaveCount(1);
    await withinDeadline(remaining)(disabled).toBeDisabled(); await withinDeadline(remaining)(disabled).toHaveAttribute("title", unavailable[0].reason);
    await disabled.evaluate((element: HTMLElement) => element.focus()); await withinDeadline(remaining)(disabled).not.toBeFocused();
  }
  expect(facets.unsupportedFilters).toContainEqual({ id: "paper_type", state: "unavailable", reason: "source-backed 문서 유형이 저장되지 않았습니다." });
  await withinDeadline(remaining)(page.getByRole("combobox", { name: /문서 유형|논문 유형/ })).toHaveCount(0);
  const unsupported = await page.request.get("/api/topics/" + topicA + "/paper-bank?paperType=survey"); expect(unsupported.status()).toBe(422);
  expect(await unsupported.json()).toEqual({ detail: { code: "paper_bank_filter_unsupported", filters: ["paperType"] } });
  expect(ledger.mutationRequests).toEqual([]);
  await page.getByRole("navigation", { name: "주제 화면", exact: true }).getByRole("link", { name: "설정", exact: true }).click();
  await settingsReady(page, remaining); await withinDeadline(remaining)(control).toHaveValue("24h");
  await finish(backend, ledger, initialRuns, timeRole, {
    localeTwelveTwentyFourActualHeaderFormatting: true, storedPreferenceSurvivesReloadAndRoutes: true,
    detailTimestampUsesSharedFormatter: true, publicationDateOnly: true, realReviewFacetOperable: true,
    citationImpactDisabledWithExactReasons: true, documentTypeAbsentAndApiUnavailable: true,
    noFocusableUnsupportedControl: true, noFakeMutationNotice: true,
  }, startedAt, testInfo);
});
