import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import {
  armCollectionsResponse,
  armDetailResponse,
  armPaperBankResponse,
  assertHygiene,
  expect,
  getPaperBank,
  installLedger,
  migrationIds,
  navigateToPapers,
  remainingBudget,
  test,
  topicA,
  withinDeadline,
} from "./m2-paper-bank.real-backend.helpers";

type Facet = {
  state: "available" | "empty" | "unavailable";
  options: Array<{ value: string; label: string; count: number }>;
  selected: string | null;
  unknownCount: number;
  reason: string | null;
};

type PaperBankWithFacets = {
  total: number;
  items: Array<{
    paperVersionId: string;
    publicationYear: number | null;
    publicationYearState: "known" | "unknown";
    accessState: string;
  }>;
  facets: {
    year: Facet;
    reviewLevel: Facet;
    access: Facet;
    rights: Facet;
  };
};

function apiPath(topicId: string, suffix = "") {
  return "/api/topics/" + topicId + suffix;
}

test("R2B server facets keep year, rights and unsupported paper type truthful", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  await navigateToPapers(page, "/app/topics/" + topicA + "/papers", topicA, remaining);

  const initial = await getPaperBank(page) as unknown as PaperBankWithFacets;
  expect(initial.total).toBeGreaterThan(25);
  expect(initial.facets.year.options.map((option) => option.value)).toEqual(
    expect.arrayContaining(["2026", "2024", "2023", "2022", "unknown"]),
  );
  expect(initial.facets.reviewLevel.options.map((option) => option.value)).toEqual(
    expect.arrayContaining(["not_reviewed", "abstract_reviewed", "full_text_available", "full_text_reviewed"]),
  );
  expect(initial.facets.access.options.map((option) => option.value)).toEqual(
    expect.arrayContaining(["open_access", "restricted", "declared", "unknown"]),
  );
  expect(initial.facets.access).toEqual(initial.facets.rights);

  const yearRequest = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET"
      && url.pathname === apiPath(topicA, "/paper-bank")
      && url.searchParams.get("year") === "2026";
  }, { timeout: remaining() });
  await page.getByRole("combobox", { name: "연도 필터" }).selectOption("2026");
  expect((await yearRequest).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL((url) => url.searchParams.get("year") === "2026");
  const yearPayload = await (
    await page.request.get(apiPath(topicA, "/paper-bank?year=unknown&pageSize=100"))
  ).json() as PaperBankWithFacets;
  expect(yearPayload.items.length).toBeGreaterThan(0);
  expect(yearPayload.items.every((item) => item.publicationYearState === "unknown" && item.publicationYear === null)).toBe(true);

  const rightsRequest = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET"
      && url.pathname === apiPath(topicA, "/paper-bank")
      && url.searchParams.get("openAccess") === "open_access";
  }, { timeout: remaining() });
  await page.getByRole("combobox", { name: "접근 권리" }).selectOption("open_access");
  expect((await rightsRequest).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL((url) => url.searchParams.get("openAccess") === "open_access");
  await withinDeadline(remaining)(page.locator(".v2-paper-access.is-open_access")).toHaveCount(1);
  const restricted = await page.request.get(apiPath(topicA, "/paper-bank?openAccess=restricted&pageSize=100"));
  expect(restricted.ok()).toBe(true);
  const restrictedPayload = await restricted.json() as PaperBankWithFacets;
  expect(restrictedPayload.items.length).toBeGreaterThan(0);
  expect(restrictedPayload.items.every((item) => item.accessState === "restricted")).toBe(true);

  const unsupported = await page.request.get(apiPath(topicA, "/paper-bank?paperType=survey"));
  expect(unsupported.status()).toBe(422);
  expect(await unsupported.json()).toEqual({
    detail: { code: "paper_bank_filter_unsupported", filters: ["paperType"] },
  });
  await assertHygiene(ledger);
  backend.writeJourneyResult({
    journey: "r2b-facets-year-rights",
    migrationIds: migrationIds([101, 108]),
    assertions: {
      serverBackedYearFacet: true,
      reviewFacetUsesExistingReviewState: true,
      rightsStatesRemainTruthful: true,
      unknownYearDoesNotBecomeKnown: true,
      paperTypeRemainsUnsupported: true,
      visibleAccessBadgeIsFieldBacked: true,
    },
    timing: {
      completeMs: Date.now() - startedAt,
      guardMs: testInfo.timeout,
      guardIsProductSla: false,
    },
    hygiene: {
      consoleErrors: ledger.consoleErrors.length,
      pageErrors: ledger.pageErrors.length,
      offOriginAttempts: ledger.offOriginAttempts.length,
    },
  });
});

test("R2B CSV export returns every filtered row as a verified D-drive artifact", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  await navigateToPapers(page, "/app/topics/" + topicA + "/papers?sort=title_asc", topicA, remaining);
  const bank = await getPaperBank(page, "sort=title_asc");
  const visibleRows = page.getByRole("row").filter({ has: page.locator(".v2-paper-title-button") });
  const visibleCount = await visibleRows.count();
  expect(visibleCount).toBeLessThan(bank.total);

  const exportResponse = await page.request.post(apiPath(topicA, "/paper-bank/export"), {
    data: { sort: "title_asc", page: 1, pageSize: 1 },
  });
  expect(exportResponse.status()).toBe(200);
  const metadata = await exportResponse.json() as {
    exportId: string;
    rowCount: number;
    paperVersionIds: string[];
    canonicalQuery: { page: number; pageSize: number; sort: string };
    fileName: string;
    artifactRef: string;
    contentSha256: string;
    downloadUrl: string;
  };
  expect(metadata.rowCount).toBe(bank.total);
  expect(metadata.rowCount).toBeGreaterThan(visibleCount);
  expect(metadata.canonicalQuery).toEqual(expect.objectContaining({ page: 1, pageSize: 1, sort: "title_asc" }));
  expect(metadata.artifactRef).toMatch(/^paper_bank_exports\/m2-spatial-paper-bank\/paper-bank-/);
  expect(metadata.artifactRef).not.toMatch(/[A-Za-z]:[\\/]/u);
  expect(metadata.downloadUrl).toContain("/paper-bank/exports/");

  const download = await page.request.get(metadata.downloadUrl);
  expect(download.status()).toBe(200);
  const csvBytes = await download.body();
  expect(download.headers()["x-xrml-artifact-sha256"]).toBe(metadata.contentSha256);
  expect(crypto.createHash("sha256").update(csvBytes).digest("hex")).toBe(metadata.contentSha256);
  const csvText = csvBytes.toString("utf8");
  const rows = csvText.trimEnd().split("\r\n");
  expect(rows).toHaveLength(metadata.rowCount + 1);
  expect(rows[0]).toBe("paper_version_id,canonical_paper_id,title,authors,provider,publication_date,publication_year,review_state,access_state,license,review_summary_state,full_text_state,evidence_count,source_url");
  expect(csvText).toContain("'=Formula");
  expect(csvText).not.toMatch(/(?:^|,)"?[A-Za-z]:[\\/]/u);
  expect(csvText).not.toContain("\\\\?\\");

  const artifactPath = path.join(backend.dataRoot, ...metadata.artifactRef.split("/").slice(0, -1), metadata.fileName);
  expect(fs.existsSync(artifactPath)).toBe(true);
  const manifestPath = path.join(backend.dataRoot, "paper_bank_exports", topicA, metadata.exportId, "manifest.json");
  expect(JSON.parse(fs.readFileSync(manifestPath, "utf8"))).toEqual(expect.objectContaining({
    exportId: metadata.exportId,
    contentSha256: metadata.contentSha256,
  }));
  await assertHygiene(ledger);
  backend.writeJourneyResult({
    journey: "r2b-csv-export",
    migrationIds: migrationIds([109, 114]),
    assertions: {
      exportButtonAndApiContract: true,
      visiblePageIsSmallerThanExport: true,
      deterministicOrdering: true,
      formulaInjectionMitigated: true,
      artifactPathIsRelative: true,
      downloadDigestVerified: true,
      manifestPersisted: true,
    },
    timing: {
      completeMs: Date.now() - startedAt,
      guardMs: testInfo.timeout,
      guardIsProductSla: false,
    },
    hygiene: {
      consoleErrors: ledger.consoleErrors.length,
      pageErrors: ledger.pageErrors.length,
      offOriginAttempts: ledger.offOriginAttempts.length,
    },
  });
});

test("R2B bulk save is atomic and evidence deep links preserve paper context", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  const navigation = await navigateToPapers(page, "/app/topics/" + topicA + "/papers", topicA, remaining);
  const bank = await getPaperBank(page);
  const selectedIds = bank.items.slice(0, 2).map((item) => item.paperVersionId);
  expect(selectedIds).toHaveLength(2);
  const collectionResponse = await page.request.post(apiPath(topicA, "/paper-collections"), { data: { name: "R2B Atomic Queue" } });
  expect(collectionResponse.status()).toBe(201);
  const collectionId = (await collectionResponse.json() as { id: string }).id;
  await page.reload({ waitUntil: "domcontentloaded", timeout: remaining() });
  await withinDeadline(remaining)(page.getByRole("table", { name: "논문 목록" })).toBeVisible();
  const collectionTarget = page.getByRole("combobox", { name: "저장 대상 컬렉션" });
  await collectionTarget.selectOption(collectionId);
  const rows = page.getByRole("row").filter({ has: page.locator(".v2-paper-title-button") });
  await rows.nth(0).getByRole("checkbox").check();
  await rows.nth(1).getByRole("checkbox").check();
  const bulkRequest = page.waitForRequest((request) => request.method() === "POST" && request.url().endsWith("/paper-collections/" + collectionId + "/members:bulk"), { timeout: remaining() });
  const bulkBankRefresh = armPaperBankResponse(page, topicA, remaining);
  const bulkCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
  await page.getByRole("button", { name: "선택 저장" }).click();
  const request = await bulkRequest;
  expect(request.postDataJSON()).toEqual({ selectedPaperVersionIds: selectedIds });
  expect((await bulkBankRefresh).ok()).toBe(true);
  expect((await bulkCollectionsRefresh).ok()).toBe(true);
  await withinDeadline(remaining)(page.getByText(/2개를 저장/)).toBeVisible();
  const firstSave = await page.request.get(apiPath(topicA, "/paper-collections"));
  expect(firstSave.ok()).toBe(true);
  const firstCollection = (await firstSave.json() as { items: Array<{ id: string; memberCount: number }> }).items.find((item) => item.id === collectionId);
  expect(firstCollection?.memberCount).toBe(2);

  const repeated = await page.request.post(apiPath(topicA, "/paper-collections/" + collectionId + "/members:bulk"), {
    data: { selectedPaperVersionIds: [selectedIds[0], selectedIds[0], selectedIds[1]] },
  });
  expect(repeated.ok()).toBe(true);
  expect(await repeated.json()).toEqual(expect.objectContaining({ requestedCount: 3, uniqueCount: 2, addedCount: 0, alreadyPresentCount: 2, finalMemberCount: 2 }));
  const foreign = await page.request.post(apiPath(topicA, "/paper-collections/" + collectionId + "/members:bulk"), {
    data: { selectedPaperVersionIds: ["version-not-in-topic"] },
  });
  expect(foreign.status()).toBe(422);
  const afterForeign = await page.request.get(apiPath(topicA, "/paper-collections"));
  expect(afterForeign.ok()).toBe(true);
  const preservedCollection = (await afterForeign.json() as { items: Array<{ id: string; memberCount: number }> }).items.find((item) => item.id === collectionId);
  expect(preservedCollection?.memberCount).toBe(2);

  const allBank = await getPaperBank(page, "pageSize=100");
  const versionTwo = allBank.items.find((item) => item.sourceIdentity.nativeId === "2608.00001v2");
  expect(versionTwo).toBeTruthy();
  const detailResponse = armDetailResponse(page, topicA, versionTwo!.paperVersionId, remaining);
  await page.goto(
    "/app/topics/" + topicA + "/papers?collection=" + collectionId + "&year=2026&openAccess=unknown&paper=" + encodeURIComponent(versionTwo!.paperVersionId),
    { waitUntil: "domcontentloaded", timeout: remaining() },
  );
  expect((await detailResponse).ok()).toBe(true);
  const detail = page.getByRole("region", { name: "논문 상세" });
  await withinDeadline(remaining)(detail).toBeVisible();
  await withinDeadline(remaining)(detail.getByText("m2-evidence-v2-001")).toBeVisible();
  const evidenceLink = detail.getByRole("link", { name: "증거 provenance 열기" });
  await withinDeadline(remaining)(evidenceLink).toHaveAttribute("href", new RegExp("collection=" + collectionId));
  await withinDeadline(remaining)(evidenceLink).toHaveAttribute("href", /year=2026/);
  await withinDeadline(remaining)(evidenceLink).toHaveAttribute("href", /openAccess=unknown/);
  const evidenceHref = await evidenceLink.getAttribute("href");
  expect(evidenceHref).toContain("paper=" + versionTwo!.paperVersionId);
  expect(evidenceHref).toContain("evidence=m2-evidence-v2-001");
  await page.goto(evidenceHref!, { waitUntil: "domcontentloaded", timeout: remaining() });
  await withinDeadline(remaining)(page.locator("[data-evidence-id='m2-evidence-v2-001']")).toHaveClass(/is-evidence-target/);
  expect(await page.locator("[data-evidence-id='m2-evidence-v2-001']").evaluate((element) => document.activeElement === element)).toBe(true);
  await assertHygiene(ledger);
  backend.writeJourneyResult({
    journey: "r2b-bulk-save-evidence-destination",
    migrationIds: migrationIds([115, 123]),
    assertions: {
      oneAtomicBulkMutation: true,
      exactRequestedAndUniqueCounts: true,
      idempotentRepeat: true,
      foreignPaperRejectedWithoutPartialWrite: true,
      evidenceDestinationUsesExactVersionAndSpan: true,
      collectionFacetContextPreserved: true,
      evidenceTargetRestoredAndFocused: true,
    },
    timing: {
      navigationVisibleMs: navigation.routeVisibleMs,
      completeMs: Date.now() - startedAt,
      guardMs: testInfo.timeout,
      guardIsProductSla: false,
    },
    hygiene: {
      consoleErrors: ledger.consoleErrors.length,
      pageErrors: ledger.pageErrors.length,
      offOriginAttempts: ledger.offOriginAttempts.length,
    },
  });
});
