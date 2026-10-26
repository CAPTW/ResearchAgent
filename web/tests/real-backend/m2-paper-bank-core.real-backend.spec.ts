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
} from "./m2-paper-bank.real-backend.helpers";

test("M2 Core Paper Bank journey is truthful and independently mutable", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  let collectionId: string | null = null;
  let disposableCollectionId: string | null = null;

  try {
    const initialNavigation = await navigateToPapers(
      page,
      "/app/topics/" + topicA + "/papers",
      topicA,
      remaining,
    );
    const table = page.getByRole("table", { name: "논문 목록" });
    await withinDeadline(remaining)(table).toBeVisible();
    const tableVisibleMs = Date.now() - startedAt;
    await withinDeadline(remaining)(page.getByText("Topic B Robotics-Only Corpus")).toHaveCount(0);

    const topicBResponse = await page.request.get("/api/topics/" + topicB + "/paper-bank?pageSize=100");
    expect(topicBResponse.ok()).toBe(true);
    const topicBPayload = await topicBResponse.json() as { items: Array<{ title: string }> };
    expect(topicBPayload.items.map((item) => item.title)).toEqual(["Topic B Robotics-Only Corpus"]);

    const searchRequest = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/topics/" + topicA + "/paper-bank"
        && url.searchParams.get("q") === "Spatial Evidence";
    }, { timeout: remaining() });
    const searchCollectionsRequest = armCollectionsResponse(page, topicA, remaining);
    const search = page.getByRole("textbox", { name: "논문 제목, 저자, 키워드 검색" });
    await search.fill("Spatial Evidence");
    await search.press("Enter");
    expect((await searchRequest).ok()).toBe(true);
    expect((await searchCollectionsRequest).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(/query=Spatial(?:\+|%20)Evidence/);
    await withinDeadline(remaining)(
      page.getByRole("row").filter({ hasText: "Spatial Evidence Bank Foundations" }),
    ).toHaveCount(2);

    const providerRequest = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/topics/" + topicA + "/paper-bank"
        && url.searchParams.get("provider") === "arxiv";
    }, { timeout: remaining() });
    const providerCollectionsRequest = armCollectionsResponse(page, topicA, remaining);
    await page.getByRole("combobox", { name: "출처" }).selectOption("arxiv");
    expect((await providerRequest).ok()).toBe(true);
    expect((await providerCollectionsRequest).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(/provider=arxiv/);

    const sortRequest = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/topics/" + topicA + "/paper-bank"
        && url.searchParams.get("sort") === "title_asc";
    }, { timeout: remaining() });
    await page.getByRole("combobox", { name: "정렬" }).selectOption("title_asc");
    expect((await sortRequest).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(/sort=title_asc/);
    await withinDeadline(remaining)(page.getByRole("navigation", { name: "논문 페이지 탐색" })).toBeVisible();
    const pageOne = await (
      await page.request.get("/api/topics/" + topicA + "/paper-bank?sort=title_asc&page=1&pageSize=1")
    ).json() as { items: Array<{ paperVersionId: string }>; queryDigest: string };
    const pageTwo = await (
      await page.request.get("/api/topics/" + topicA + "/paper-bank?sort=title_asc&page=2&pageSize=1")
    ).json() as { items: Array<{ paperVersionId: string }>; queryDigest: string };
    expect(pageOne.items).toHaveLength(1);
    expect(pageTwo.items).toHaveLength(1);
    expect(pageOne.items[0].paperVersionId).not.toBe(pageTwo.items[0].paperVersionId);
    expect(pageOne.queryDigest).not.toBe(pageTwo.queryDigest);

    await page.getByRole("textbox", { name: "새 컬렉션 이름" }).fill("Core Journey Queue");
    const createResponse = page.waitForResponse(
      (response) => response.request().method() === "POST"
        && response.url().endsWith("/api/topics/" + topicA + "/paper-collections"),
      { timeout: remaining() },
    );
    const createBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const createCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await page.getByRole("button", { name: "컬렉션 생성" }).click();
    const createdResponse = await createResponse;
    expect(createdResponse.status()).toBe(201);
    const createdCollection = await createdResponse.json() as { id: string; name: string; memberCount: number };
    collectionId = createdCollection.id;
    expect(createdCollection.name).toBe("Core Journey Queue");
    expect(collectionId).toMatch(/^collection-/);
    expect((await createBankRefresh).ok()).toBe(true);
    expect((await createCollectionsRefresh).ok()).toBe(true);
    await withinDeadline(remaining)(
      page.locator(".v2-collection-list strong", { hasText: "Core Journey Queue" }),
    ).toBeVisible();

    let collectionsResponse = await page.request.get("/api/topics/" + topicA + "/paper-collections");
    let collectionsPayload = await collectionsResponse.json() as {
      items: Array<{ id: string; name: string; memberCount: number }>;
    };
    expect(collectionsPayload.items.find((item) => item.name === "Core Journey Queue")?.id).toBe(collectionId);

    await page.getByRole("textbox", { name: "Core Journey Queue 새 이름" }).fill("Core Journey Queue Renamed");
    const renameResponse = page.waitForResponse(
      (response) => response.request().method() === "PATCH"
        && new URL(response.url()).pathname
          === "/api/topics/" + topicA + "/paper-collections/" + collectionId,
      { timeout: remaining() },
    );
    const renameBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const renameCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await page.getByRole("button", { name: "Core Journey Queue 이름 변경" }).click();
    const renamedResponse = await renameResponse;
    expect(renamedResponse.ok()).toBe(true);
    const renamedCollection = await renamedResponse.json() as { id: string; name: string; memberCount: number };
    expect(renamedCollection.id).toBe(collectionId);
    expect(renamedCollection.name).toBe("Core Journey Queue Renamed");
    expect((await renameBankRefresh).ok()).toBe(true);
    expect((await renameCollectionsRefresh).ok()).toBe(true);
    await withinDeadline(remaining)(
      page.locator(".v2-collection-list strong", { hasText: "Core Journey Queue Renamed" }),
    ).toBeVisible();
    await withinDeadline(remaining)(
      page.locator(".v2-collection-list strong").filter({ hasText: /^Core Journey Queue$/ }),
    ).toHaveCount(0);

    collectionsResponse = await page.request.get("/api/topics/" + topicA + "/paper-collections");
    collectionsPayload = await collectionsResponse.json() as {
      items: Array<{ id: string; name: string; memberCount: number }>;
    };
    expect(collectionsPayload.items.find((item) => item.name === "Core Journey Queue Renamed")?.id)
      .toBe(collectionId);
    expect(collectionsPayload.items.find((item) => item.name === "Core Journey Queue")).toBeUndefined();

    const duplicateLabelResponse = await page.request.post(
      "/api/topics/" + topicA + "/paper-collections",
      { data: { name: "Core Journey Queue Renamed" } },
    );
    expect(duplicateLabelResponse.status()).toBe(409);
    expect(await duplicateLabelResponse.json()).toEqual({ detail: "collection_name_exists" });
    const afterDuplicateResponse = await page.request.get("/api/topics/" + topicA + "/paper-collections");
    const afterDuplicatePayload = await afterDuplicateResponse.json() as {
      items: Array<{ id: string; name: string; memberCount: number }>;
    };
    expect(afterDuplicatePayload.items.filter((item) => item.name === "Core Journey Queue Renamed"))
      .toEqual([expect.objectContaining({ id: collectionId })]);

    await page.getByRole("textbox", { name: "새 컬렉션 이름" }).fill("Core Disposable Queue");
    const disposableCreate = page.waitForResponse(
      (response) => response.request().method() === "POST"
        && response.url().endsWith("/api/topics/" + topicA + "/paper-collections"),
      { timeout: remaining() },
    );
    const disposableBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const disposableCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await page.getByRole("button", { name: "컬렉션 생성" }).click();
    expect((await disposableCreate).status()).toBe(201);
    expect((await disposableBankRefresh).ok()).toBe(true);
    expect((await disposableCollectionsRefresh).ok()).toBe(true);
    collectionsResponse = await page.request.get("/api/topics/" + topicA + "/paper-collections");
    collectionsPayload = await collectionsResponse.json() as {
      items: Array<{ id: string; name: string; memberCount: number }>;
    };
    disposableCollectionId = collectionsPayload.items.find((item) => item.name === "Core Disposable Queue")?.id ?? null;
    expect(disposableCollectionId).toBeTruthy();
    const deleteResponse = page.waitForResponse(
      (response) => response.request().method() === "DELETE"
        && response.url().includes("/paper-collections/" + disposableCollectionId),
      { timeout: remaining() },
    );
    const deleteBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const deleteCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await page.getByRole("button", { name: "Core Disposable Queue 삭제" }).click();
    expect((await deleteResponse).ok()).toBe(true);
    expect((await deleteBankRefresh).ok()).toBe(true);
    expect((await deleteCollectionsRefresh).ok()).toBe(true);
    disposableCollectionId = null;
    await withinDeadline(remaining)(
      page.locator(".v2-collection-list strong", { hasText: "Core Disposable Queue" }),
    ).toHaveCount(0);

    const saveTargetCollection = page.getByRole("combobox", { name: "저장 대상 컬렉션" });
    await saveTargetCollection.selectOption(collectionId!);
    await withinDeadline(remaining)(saveTargetCollection).toHaveValue(collectionId!);
    await withinDeadline(remaining)(saveTargetCollection.locator("option:checked"))
      .toHaveText("Core Journey Queue Renamed");
    const bank = await getPaperBank(page);
    const versionTwo = versionTwoFrom(bank);
    const repeatedV1 = bank.items.find((item) => item.sourceIdentity.nativeId === "2608.00001v1");
    expect(repeatedV1?.compatibility.occurrences).toHaveLength(2);
    const versionTwoRow = page.getByRole("row").filter({ hasText: "2608.00001v2" });
    await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);
    const addResponse = page.waitForResponse(
      (response) => response.request().method() === "PUT"
        && response.url().includes("/paper-collections/" + collectionId + "/members/"),
      { timeout: remaining() },
    );
    const addBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const addCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await versionTwoRow.getByRole("button", { name: /저장$/ }).click();
    const addedResponse = await addResponse;
    expect(addedResponse.ok()).toBe(true);
    expect(new URL(addedResponse.url()).pathname)
      .toBe("/api/topics/" + topicA + "/paper-collections/" + collectionId + "/members/" + versionTwo.paperVersionId);
    expect((await addBankRefresh).ok()).toBe(true);
    expect((await addCollectionsRefresh).ok()).toBe(true);
    await withinDeadline(remaining)(versionTwoRow.getByRole("button", { name: /저장 해제$/ })).toBeVisible();

    const filterRequest = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/topics/" + topicA + "/paper-bank"
        && url.searchParams.get("collectionId") === collectionId;
    }, { timeout: remaining() });
    const filterCollections = armCollectionsResponse(page, topicA, remaining);
    const collectionFilter = page.getByRole("combobox", { name: "표시할 컬렉션", exact: true });
    await collectionFilter.selectOption(collectionId!);
    expect((await filterRequest).ok()).toBe(true);
    expect((await filterCollections).ok()).toBe(true);
    await withinDeadline(remaining)(collectionFilter).toHaveValue(collectionId!);
    await withinDeadline(remaining)(collectionFilter.locator("option:checked"))
      .toHaveText("Core Journey Queue Renamed (1)");
    await withinDeadline(remaining)(page).toHaveURL((url) => url.searchParams.get("collection") === collectionId);
    await withinDeadline(remaining)(page.getByRole("row").filter({ hasText: "2608.00001v2" })).toHaveCount(1);
    await withinDeadline(remaining)(page.getByRole("row").filter({ hasText: "2608.00001v1" })).toHaveCount(0);
    const filterSummary = page.locator(".v2-filter-summary");
    await withinDeadline(remaining)(
      filterSummary.getByText("Core Journey Queue Renamed", { exact: true }),
    ).toBeVisible();
    await withinDeadline(remaining)(filterSummary.getByText(collectionId!, { exact: true })).toHaveCount(0);

    const titleButton = page.getByRole("button", { name: "Spatial Evidence Bank Foundations 상세 열기" });
    await titleButton.focus();
    const detailResponse = armDetailResponse(page, topicA, versionTwo.paperVersionId, remaining);
    await titleButton.click();
    expect((await detailResponse).ok()).toBe(true);
    const detail = page.getByRole("region", { name: "논문 상세" });
    await withinDeadline(remaining)(detail).toBeVisible();
    await withinDeadline(remaining)(detail.getByText("full_text_reviewed")).toHaveCount(0);
    await withinDeadline(remaining)(detail.getByText("전문 검토 완료")).toBeVisible();
    await withinDeadline(remaining)(detail.getByText("reviewed", { exact: true })).toBeVisible();
    await withinDeadline(remaining)(detail.getByText("m2-evidence-v2-001")).toBeVisible();
    await withinDeadline(remaining)(detail.getByText(/"page":2/)).toBeVisible();
    await withinDeadline(remaining)(detail.getByRole("link", { name: "증거 provenance 열기" })).toBeVisible();
    await withinDeadline(remaining)(page).toHaveURL(new RegExp("paper=" + versionTwo.paperVersionId));
    const deepLink = page.url();

    await detail.press("Escape");
    await withinDeadline(remaining)(detail).toHaveCount(0);
    await withinDeadline(remaining)(titleButton).toBeFocused();
    const reopened = armDetailResponse(page, topicA, versionTwo.paperVersionId, remaining);
    await titleButton.click();
    expect((await reopened).ok()).toBe(true);
    await withinDeadline(remaining)(page.getByRole("region", { name: "논문 상세" })).toBeVisible();

    await versionTwoRow.getByRole("checkbox", { name: /선택/ }).check();
    await withinDeadline(remaining)(page.getByText("1 선택됨")).toBeVisible();
    const addToReport = page.getByRole("button", { name: /선택 보고서/ });
    await withinDeadline(remaining)(addToReport).toBeDisabled();
    await withinDeadline(remaining)(addToReport).toHaveAttribute("title", /M4/);
    await withinDeadline(remaining)(page.getByRole("combobox", { name: "연도 필터" })).toBeEnabled();
    await withinDeadline(remaining)(
      page.getByRole("combobox", { name: "인용수 필터" }),
    ).toHaveAttribute("title", /관측된 인용 계약/);

    const emptyUrl = new URL(deepLink);
    emptyUrl.searchParams.set("query", "no-matching-paper-fixture-value");
    emptyUrl.searchParams.delete("paper");
    await navigateToPapers(page, emptyUrl.toString(), topicA, remaining);
    await withinDeadline(remaining)(
      page.getByText("Core Journey Queue Renamed 컬렉션은 존재하며 저장된 논문이 0개입니다."),
    ).toBeVisible();

    const invalidDetailUrl = new URL(deepLink);
    invalidDetailUrl.searchParams.set("paper", "version-does-not-exist");
    const consoleCountBeforeExpectedFailure = ledger.consoleErrors.length;
    await navigateToPapers(page, invalidDetailUrl.toString(), topicA, remaining, {
      detailPaperVersionId: "version-does-not-exist",
      expectedDetailStatus: 404,
    });
    await withinDeadline(remaining)(page.getByText("논문 상세를 불러오지 못했습니다.")).toBeVisible();
    await withinDeadline(remaining)(page.getByRole("button", { name: "다시 시도" })).toBeVisible();
    const expectedFailureErrors = ledger.consoleErrors.splice(consoleCountBeforeExpectedFailure);
    expect(expectedFailureErrors).toHaveLength(1);
    expect(expectedFailureErrors[0]).toContain("404");
    ledger.handledExpectedErrors.push(...expectedFailureErrors);

    await assertHygiene(ledger);
    expect(ledger.mutationRequests.some((item) => item.startsWith("POST "))).toBe(true);
    expect(ledger.mutationRequests.some((item) => item.startsWith("PATCH "))).toBe(true);
    expect(ledger.mutationRequests.some((item) => item.startsWith("PUT "))).toBe(true);
    expect(ledger.mutationRequests.some((item) => item.startsWith("DELETE "))).toBe(true);
    backend.writeJourneyResult({
      journey: "core",
      migrationIds: migrationIds([1, 7], [14, 55]),
      assertions: {
        routeAndTable: true,
        searchProviderSort: true,
        stablePaginationContract: true,
        collectionCrudAndSave: true,
        detailReviewFullTextEvidence: true,
        keyboardFocusEscape: true,
        emptyAndErrorTruth: true,
      },
      timing: {
        routeVisibleMs: initialNavigation.routeVisibleMs,
        initialTruthVisibleMs: initialNavigation.initialTruthVisibleMs,
        paperBankResponseMs: initialNavigation.paperBankResponseMs,
        tableVisibleMs,
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
  } finally {
    // The fixture owns its isolated runtime-data tree after the backend stops.
  }
});
