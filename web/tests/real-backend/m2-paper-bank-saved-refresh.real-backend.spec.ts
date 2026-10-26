import type { Route } from "@playwright/test";

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
  topicB,
  versionTwoFrom,
  withinDeadline,
  type PaperBankPayload,
} from "./m2-paper-bank.real-backend.helpers";

type CollectionList = {
  items: Array<{ id: string; name: string; memberCount: number }>;
};

function paperBankRequests(
  requests: Array<{ method: string; pathname: string; search: string }>,
) {
  return requests.filter(
    (request) => request.method === "GET" && request.pathname === "/api/topics/" + topicA + "/paper-bank",
  );
}

function collectionRequests(
  requests: Array<{ method: string; pathname: string; search: string }>,
) {
  return requests.filter(
    (request) => request.method === "GET"
      && request.pathname === "/api/topics/" + topicA + "/paper-collections",
  );
}

test("Shell Saved papers route, collection intent, detail and persistence", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);

  const allPapers = await getPaperBank(page);
  const versionTwo = versionTwoFrom(allPapers);
  const repeatedV1 = allPapers.items.find((item) => item.sourceIdentity.nativeId === "2608.00001v1")!;
  const collections = await (
    await page.request.get("/api/topics/" + topicA + "/paper-collections")
  ).json() as CollectionList;
  const populatedCollection = collections.items.find((item) => item.name === "Seeded Reading Queue")!;
  const emptyCollection = collections.items.find((item) => item.name === "Seeded Empty Queue")!;

  const initialNavigation = await navigateToPapers(
    page,
    "/app/topics/" + topicA + "/papers",
    topicA,
    remaining,
  );
  const versionTwoRow = page.getByRole("row").filter({ hasText: "2608.00001v2" });
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);

  const saveResponse = page.waitForResponse(
    (response) => response.request().method() === "PUT"
      && response.url().includes("/paper-collections/saved-papers/members/" + versionTwo.paperVersionId),
    { timeout: remaining() },
  );
  const saveBankRefresh = armPaperBankResponse(page, topicA, remaining);
  const saveCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
  await versionTwoRow.getByRole("button", { name: /저장$/ }).click();
  expect((await saveResponse).ok()).toBe(true);
  expect((await saveBankRefresh).ok()).toBe(true);
  expect((await saveCollectionsRefresh).ok()).toBe(true);
  await withinDeadline(remaining)(versionTwoRow.getByRole("button", { name: /저장 해제$/ })).toBeVisible();

  const savedLink = page.getByRole("link", { name: "저장된 논문" });
  await withinDeadline(remaining)(savedLink).toBeVisible();
  const shellSavedBank = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank"
      && url.searchParams.get("collectionId") === "saved-papers";
  }, { timeout: remaining() });
  const shellSavedCollections = armCollectionsResponse(page, topicA, remaining);
  await savedLink.focus();
  await savedLink.press("Enter");
  const shellSavedPayload = await (await shellSavedBank).json() as PaperBankPayload;
  expect((await shellSavedCollections).ok()).toBe(true);
  expect(shellSavedPayload.items.map((item) => item.paperVersionId)).toEqual([versionTwo.paperVersionId]);
  await withinDeadline(remaining)(page).toHaveURL(
    new RegExp("/app/topics/" + topicA + "/papers\\?collection=saved$"),
  );
  await withinDeadline(remaining)(savedLink).toHaveAttribute("aria-current", "page");
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);
  await withinDeadline(remaining)(page.getByRole("row").filter({ hasText: "2608.00001v1" })).toHaveCount(0);

  const directSavedUrl = "/app/topics/" + topicA
    + "/papers?collection=saved&query=Spatial%20Evidence&provider=arxiv&sort=title_asc";
  const directNavigation = await navigateToPapers(page, directSavedUrl, topicA, remaining);
  const directUrl = new URL(directNavigation.paperBankResponse.url());
  expect(directUrl.searchParams.get("collectionId")).toBe("saved-papers");
  expect(directUrl.searchParams.get("q")).toBe("Spatial Evidence");
  expect(directUrl.searchParams.get("provider")).toBe("arxiv");
  expect(directUrl.searchParams.get("sort")).toBe("title_asc");
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);

  const saveTarget = page.getByRole("combobox", { name: "저장 대상 컬렉션" });
  await saveTarget.selectOption(emptyCollection.id);
  const namedBank = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank"
      && url.searchParams.get("collectionId") === populatedCollection.id;
  }, { timeout: remaining() });
  const namedCollections = armCollectionsResponse(page, topicA, remaining);
  await page.getByRole("combobox", { name: "표시할 컬렉션", exact: true }).selectOption(populatedCollection.id);
  expect((await namedBank).ok()).toBe(true);
  expect((await namedCollections).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL(new RegExp("collection=" + populatedCollection.id));
  await withinDeadline(remaining)(saveTarget).toHaveValue(emptyCollection.id);
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);

  const backBank = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank"
      && url.searchParams.get("collectionId") === "saved-papers";
  }, { timeout: remaining() });
  await page.goBack({ waitUntil: "domcontentloaded", timeout: remaining() });
  expect((await backBank).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL(/collection=saved/);
  await withinDeadline(remaining)(saveTarget).toHaveValue(emptyCollection.id);

  const forwardBank = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank"
      && url.searchParams.get("collectionId") === populatedCollection.id;
  }, { timeout: remaining() });
  await page.goForward({ waitUntil: "domcontentloaded", timeout: remaining() });
  expect((await forwardBank).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL(new RegExp("collection=" + populatedCollection.id));
  await withinDeadline(remaining)(saveTarget).toHaveValue(emptyCollection.id);

  const savedAgain = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank"
      && url.searchParams.get("collectionId") === "saved-papers";
  }, { timeout: remaining() });
  await page.getByRole("combobox", { name: "표시할 컬렉션", exact: true }).selectOption("saved-papers");
  expect((await savedAgain).ok()).toBe(true);
  await withinDeadline(remaining)(page).toHaveURL(/collection=saved/);
  await withinDeadline(remaining)(saveTarget).toHaveValue(emptyCollection.id);

  const detailResponse = armDetailResponse(page, topicA, versionTwo.paperVersionId, remaining);
  await versionTwoRow.getByRole("button", { name: "Spatial Evidence Bank Foundations 상세 열기" }).click();
  expect((await detailResponse).ok()).toBe(true);
  const detail = page.getByRole("region", { name: "논문 상세" });
  await withinDeadline(remaining)(detail).toBeVisible();
  await withinDeadline(remaining)(page).toHaveURL(/collection=saved.*paper=/);
  await withinDeadline(remaining)(detail.getByText("전문 검토 완료")).toBeVisible();
  await withinDeadline(remaining)(detail.getByText("reviewed", { exact: true })).toBeVisible();
  await withinDeadline(remaining)(detail.getByText("m2-evidence-v2-001")).toBeVisible();
  await detail.getByRole("button", { name: "논문 상세 닫기" }).click();
  await withinDeadline(remaining)(detail).toHaveCount(0);
  await withinDeadline(remaining)(page).toHaveURL(/collection=saved/);

  await saveTarget.selectOption("saved-papers");
  const removeResponse = page.waitForResponse(
    (response) => response.request().method() === "DELETE"
      && response.url().includes("/paper-collections/saved-papers/members/" + versionTwo.paperVersionId),
    { timeout: remaining() },
  );
  const removeBankRefresh = armPaperBankResponse(page, topicA, remaining);
  const removeCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
  await versionTwoRow.getByRole("button", { name: /저장 해제$/ }).click();
  expect((await removeResponse).ok()).toBe(true);
  expect((await removeBankRefresh).ok()).toBe(true);
  expect((await removeCollectionsRefresh).ok()).toBe(true);
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(0);
  await withinDeadline(remaining)(
    page.getByText("Saved papers 컬렉션은 존재하며 저장된 논문이 0개입니다."),
  ).toBeVisible();

  const clearSavedBank = armPaperBankResponse(page, topicA, remaining);
  await page.getByRole("combobox", { name: "표시할 컬렉션", exact: true }).selectOption("");
  expect((await clearSavedBank).ok()).toBe(true);
  await withinDeadline(remaining)(page).not.toHaveURL(/collection=/);
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);

  const topicBSaved = await page.request.get(
    "/api/topics/" + topicB + "/paper-bank?collectionId=saved-papers&pageSize=100",
  );
  expect(topicBSaved.ok()).toBe(true);
  expect(((await topicBSaved.json()) as PaperBankPayload).items).toEqual([]);

  const expectedConsoleStart = ledger.consoleErrors.length;
  await navigateToPapers(
    page,
    "/app/topics/" + topicA + "/papers?collection=deleted-route-collection",
    topicA,
    remaining,
    { expectedPaperBankStatus: 422 },
  );
  await withinDeadline(remaining)(page.getByText("요청한 컬렉션을 찾을 수 없습니다.")).toBeVisible();
  const expectedErrors = ledger.consoleErrors.splice(expectedConsoleStart);
  expect(expectedErrors.every((message) => message.includes("422"))).toBe(true);
  ledger.handledExpectedErrors.push(...expectedErrors);
  const clearMissingBank = armPaperBankResponse(page, topicA, remaining);
  await page.getByRole("button", { name: "컬렉션 필터 지우기" }).click();
  expect((await clearMissingBank).ok()).toBe(true);
  await withinDeadline(remaining)(page).not.toHaveURL(/collection=/);
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);

  await page.goto(
    "/app/topics/" + topicA + "/papers?collection=%2Fmalformed",
    { waitUntil: "domcontentloaded", timeout: remaining() },
  );
  await withinDeadline(remaining)(page.getByText("컬렉션 경로가 올바르지 않습니다.")).toBeVisible();

  await assertHygiene(ledger);
  expect(ledger.mutationRequests).toContain(
    "PUT /api/topics/" + topicA + "/paper-collections/saved-papers/members/" + versionTwo.paperVersionId,
  );
  expect(ledger.mutationRequests).toContain(
    "DELETE /api/topics/" + topicA + "/paper-collections/saved-papers/members/" + versionTwo.paperVersionId,
  );
  backend.writeJourneyResult({
    journey: "saved-papers-route",
    migrationIds: migrationIds([80, 87]),
    assertions: {
      shellKeyboardRouteAndCurrentState: true,
      savedAliasAndExactPersistedMembers: true,
      directUrlAndSupportedQueryState: true,
      namedRouteAndBrowserHistory: true,
      saveTargetIndependent: true,
      detailReviewFullTextEvidenceContinuity: true,
      removeMembershipAndUnderlyingPaperRetention: true,
      emptyMissingMalformedAndCrossTopicTruth: true,
    },
    timing: {
      routeVisibleMs: initialNavigation.routeVisibleMs,
      initialTruthVisibleMs: initialNavigation.initialTruthVisibleMs,
      paperBankResponseMs: initialNavigation.paperBankResponseMs,
      directRouteVisibleMs: directNavigation.routeVisibleMs,
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

test("Resource-scoped Papers refresh preserves independent state", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);

  const allPapers = await getPaperBank(page);
  const versionTwo = versionTwoFrom(allPapers);
  const repeatedV1 = allPapers.items.find((item) => item.sourceIdentity.nativeId === "2608.00001v1")!;
  const collections = await (
    await page.request.get("/api/topics/" + topicA + "/paper-collections")
  ).json() as CollectionList;
  const populatedCollection = collections.items.find((item) => item.name === "Seeded Reading Queue")!;
  const emptyCollection = collections.items.find((item) => item.name === "Seeded Empty Queue")!;
  expect((await page.request.put(
    "/api/topics/" + topicA + "/paper-collections/saved-papers/members/" + versionTwo.paperVersionId,
  )).ok()).toBe(true);

  const route = "/app/topics/" + topicA + "/papers?collection=saved&query=Spatial%20Evidence"
    + "&provider=arxiv&sort=title_asc&paper=" + encodeURIComponent(versionTwo.paperVersionId);
  await navigateToPapers(page, route, topicA, remaining, {
    detailPaperVersionId: versionTwo.paperVersionId,
  });
  const detail = page.getByRole("region", { name: "논문 상세" });
  await withinDeadline(remaining)(detail).toBeVisible();
  await page.getByRole("combobox", { name: "저장 대상 컬렉션" }).selectOption(emptyCollection.id);
  const versionTwoRow = page.getByRole("row").filter({ hasText: "2608.00001v2" });
  await versionTwoRow.getByRole("checkbox", { name: /선택/ }).check();
  await withinDeadline(remaining)(page.getByText("1 선택됨")).toBeVisible();

  expect((await page.request.put(
    "/api/topics/" + topicA + "/paper-collections/saved-papers/members/" + repeatedV1.paperVersionId,
  )).ok()).toBe(true);

  const documentMarker = "r2a-" + Date.now();
  const navigationCount = await page.evaluate((marker) => {
    (window as Window & { __r2aDocumentMarker?: string }).__r2aDocumentMarker = marker;
    return performance.getEntriesByType("navigation").length;
  }, documentMarker);
  let delayedRefreshRequests = 0;
  let delayNextSavedBank = true;
  const delayHandler = async (routeRequest: Route) => {
    const url = new URL(routeRequest.request().url());
    if (delayNextSavedBank
        && routeRequest.request().method() === "GET"
        && url.pathname === "/api/topics/" + topicA + "/paper-bank"
        && url.searchParams.get("collectionId") === "saved-papers") {
      delayNextSavedBank = false;
      delayedRefreshRequests += 1;
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
    await routeRequest.continue();
  };
  await page.route("**/api/topics/**/paper-bank?**", delayHandler);

  const bankBeforeRefresh = paperBankRequests(ledger.requests).length;
  const collectionsBeforeRefresh = collectionRequests(ledger.requests).length;
  const mutationsBeforeRefresh = ledger.mutationRequests.length;
  const refreshBank = armPaperBankResponse(page, topicA, remaining);
  const refreshCollections = armCollectionsResponse(page, topicA, remaining);
  const refreshDetail = armDetailResponse(page, topicA, versionTwo.paperVersionId, remaining);
  const refresh = page.getByRole("button", { name: /새로고침/ });
  await refresh.click();
  await withinDeadline(remaining)(refresh).toHaveAttribute("aria-busy", "true");
  await withinDeadline(remaining)(refresh).toBeDisabled();
  await refresh.evaluate((element) => (element as HTMLButtonElement).click());
  expect((await refreshBank).ok()).toBe(true);
  expect((await refreshCollections).ok()).toBe(true);
  expect((await refreshDetail).ok()).toBe(true);
  await withinDeadline(remaining)(page.getByText("논문 리소스를 새로고침했습니다.")).toBeVisible();
  expect(delayedRefreshRequests).toBe(1);
  expect(paperBankRequests(ledger.requests)).toHaveLength(bankBeforeRefresh + 1);
  expect(collectionRequests(ledger.requests)).toHaveLength(collectionsBeforeRefresh + 1);
  expect(ledger.mutationRequests).toHaveLength(mutationsBeforeRefresh);
  await withinDeadline(remaining)(page.getByRole("row").filter({ hasText: "2608.00001v1" })).toHaveCount(1);
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "저장 대상 컬렉션" })).toHaveValue(emptyCollection.id);
  await withinDeadline(remaining)(versionTwoRow.getByRole("checkbox", { name: /선택/ })).toBeChecked();
  await withinDeadline(remaining)(detail).toBeVisible();
  await withinDeadline(remaining)(detail.getByText("m2-evidence-v2-001")).toBeVisible();
  await withinDeadline(remaining)(page).toHaveURL(/collection=saved.*query=Spatial(?:%20|\+)Evidence.*provider=arxiv.*sort=title_asc.*paper=/);
  expect(await page.evaluate(() => (
    window as Window & { __r2aDocumentMarker?: string }
  ).__r2aDocumentMarker)).toBe(documentMarker);
  expect(await page.evaluate(() => performance.getEntriesByType("navigation").length)).toBe(navigationCount);
  await page.unroute("**/api/topics/**/paper-bank?**", delayHandler);

  let failCollectionOnce = true;
  const collectionFailureHandler = async (routeRequest: Route) => {
    const url = new URL(routeRequest.request().url());
    if (failCollectionOnce
        && routeRequest.request().method() === "GET"
        && url.pathname === "/api/topics/" + topicA + "/paper-collections") {
      failCollectionOnce = false;
      await routeRequest.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "paper_collection_store_unavailable" }),
      });
      return;
    }
    await routeRequest.continue();
  };
  await page.route("**/api/topics/**/paper-collections", collectionFailureHandler);
  const consoleBeforeExpectedFailure = ledger.consoleErrors.length;
  const failureBankBefore = paperBankRequests(ledger.requests).length;
  const failureCollectionsBefore = collectionRequests(ledger.requests).length;
  const failedBank = armPaperBankResponse(page, topicA, remaining);
  const failedCollections = armCollectionsResponse(page, topicA, remaining);
  await refresh.click();
  expect((await failedBank).ok()).toBe(true);
  expect((await failedCollections).status()).toBe(503);
  await withinDeadline(remaining)(page.getByRole("alert")).toContainText("paper_collection_store_unavailable");
  await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);
  await withinDeadline(remaining)(page.locator(".v2-collection-list strong", { hasText: "Seeded Reading Queue" })).toBeVisible();
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "저장 대상 컬렉션" })).toBeDisabled();
  expect(paperBankRequests(ledger.requests)).toHaveLength(failureBankBefore + 1);
  expect(collectionRequests(ledger.requests)).toHaveLength(failureCollectionsBefore + 1);
  const failureConsole = ledger.consoleErrors.splice(consoleBeforeExpectedFailure);
  expect(failureConsole.every((message) => message.includes("503"))).toBe(true);
  ledger.handledExpectedErrors.push(...failureConsole);

  const retryBankBefore = paperBankRequests(ledger.requests).length;
  const retryCollectionsBefore = collectionRequests(ledger.requests).length;
  const retryCollections = armCollectionsResponse(page, topicA, remaining);
  await page.getByRole("button", { name: "해당 리소스 다시 시도" }).click();
  expect((await retryCollections).ok()).toBe(true);
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "저장 대상 컬렉션" })).toBeEnabled();
  expect(paperBankRequests(ledger.requests)).toHaveLength(retryBankBefore);
  expect(collectionRequests(ledger.requests)).toHaveLength(retryCollectionsBefore + 1);
  await page.unroute("**/api/topics/**/paper-collections", collectionFailureHandler);

  let delayNamedRequest = true;
  const staleHandler = async (routeRequest: Route) => {
    const url = new URL(routeRequest.request().url());
    if (delayNamedRequest
        && routeRequest.request().method() === "GET"
        && url.pathname === "/api/topics/" + topicA + "/paper-bank"
        && url.searchParams.get("collectionId") === populatedCollection.id) {
      delayNamedRequest = false;
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
    await routeRequest.continue();
  };
  await page.route("**/api/topics/**/paper-bank?**", staleHandler);
  const collectionFilter = page.getByRole("combobox", { name: "표시할 컬렉션", exact: true });
  await collectionFilter.selectOption(populatedCollection.id);
  await expect.poll(
    () => paperBankRequests(ledger.requests).some((request) => (
      new URLSearchParams(request.search).get("collectionId") === populatedCollection.id
    )),
    { timeout: remaining() },
  ).toBe(true);
  const newestSavedBank = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/topics/" + topicA + "/paper-bank"
      && url.searchParams.get("collectionId") === "saved-papers";
  }, { timeout: remaining() });
  await collectionFilter.selectOption("saved-papers");
  expect((await newestSavedBank).ok()).toBe(true);
  await new Promise((resolve) => setTimeout(resolve, 450));
  await withinDeadline(remaining)(page).toHaveURL(/collection=saved/);
  await withinDeadline(remaining)(collectionFilter).toHaveValue("saved-papers");
  await withinDeadline(remaining)(page.getByRole("row").filter({ hasText: "2608.00001v1" })).toHaveCount(1);
  await page.unroute("**/api/topics/**/paper-bank?**", staleHandler);

  const workRequests = ledger.requests.filter((request) => (
    request.method !== "GET"
    && (/\/runs?(?:\/|$)/u.test(request.pathname) || /provider/iu.test(request.pathname))
  ));
  expect(workRequests).toEqual([]);
  await assertHygiene(ledger);
  backend.writeJourneyResult({
    journey: "resource-scoped-papers-refresh",
    migrationIds: migrationIds([88, 95]),
    assertions: {
      paperBankAndCollectionRefetch: true,
      noDocumentReloadRunOrProvider: true,
      routeQueryProviderSortCollectionPreserved: true,
      saveTargetSelectionAndDetailPreserved: true,
      persistedBackendChangeRendered: true,
      busyStateAndDuplicateFence: true,
      staleGenerationCannotOverwriteNewestRoute: true,
      collectionFailurePreservesPaperBankAndPriorCollectionData: true,
      collectionRetryIsResourceScoped: true,
      truthfulCompletionAndFailureDisclosure: true,
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
