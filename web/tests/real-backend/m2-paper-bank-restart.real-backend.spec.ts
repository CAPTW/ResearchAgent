import {
  armCollectionsResponse,
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

test("M2 restart persistence preserves independent resource truth", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  let collectionId: string | null = null;

  try {
    const initialBank = await getPaperBank(page);
    const versionTwo = versionTwoFrom(initialBank);
    const repeatedV1 = initialBank.items.find((item) => item.sourceIdentity.nativeId === "2608.00001v1");
    expect(repeatedV1?.compatibility.occurrences).toHaveLength(2);

    const createResponse = await page.request.post("/api/topics/" + topicA + "/paper-collections", {
      data: { name: "Restart Persistence Queue" },
    });
    expect(createResponse.status()).toBe(201);
    const collection = await createResponse.json() as { id: string; name: string };
    collectionId = collection.id;
    const addResponse = await page.request.put(
      "/api/topics/" + topicA + "/paper-collections/" + collectionId
        + "/members/" + versionTwo.paperVersionId,
    );
    expect(addResponse.ok()).toBe(true);

    const deepLink = "/app/topics/" + topicA + "/papers?collection="
      + encodeURIComponent(collectionId) + "&paper=" + encodeURIComponent(versionTwo.paperVersionId);
    await navigateToPapers(page, deepLink, topicA, remaining, {
      detailPaperVersionId: versionTwo.paperVersionId,
    });
    await withinDeadline(remaining)(page.getByRole("table", { name: "논문 목록" })).toBeVisible();
    await withinDeadline(remaining)(
      page.getByRole("combobox", { name: "표시할 컬렉션", exact: true }),
    ).toHaveValue(collectionId);
    await withinDeadline(remaining)(page.getByRole("region", { name: "논문 상세" })).toBeVisible();
    await withinDeadline(remaining)(page.getByText("m2-evidence-v2-001")).toBeVisible();

    const collectionFilter = page.getByRole("combobox", { name: "표시할 컬렉션", exact: true });
    await withinDeadline(remaining)(collectionFilter).toHaveValue(collectionId);
    await page.getByRole("combobox", { name: "저장 대상 컬렉션" }).selectOption(collectionId);
    const versionTwoRow = page.getByRole("row").filter({ hasText: "2608.00001v2" });
    await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);
    await withinDeadline(remaining)(versionTwoRow.getByRole("button", { name: /저장 해제$/ })).toBeVisible();

    const removeResponse = page.waitForResponse(
      (response) => response.request().method() === "DELETE"
        && response.url().includes("/paper-collections/" + collectionId + "/members/"),
      { timeout: remaining() },
    );
    const removeBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const removeCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await versionTwoRow.getByRole("button", { name: /저장 해제$/ }).click();
    expect((await removeResponse).ok()).toBe(true);
    expect((await removeBankRefresh).ok()).toBe(true);
    const removedCollectionsPayload = await (await removeCollectionsRefresh).json() as {
      items: Array<{ id: string; memberCount: number }>;
    };
    expect(removedCollectionsPayload.items.find((item) => item.id === collectionId)?.memberCount).toBe(0);
    await withinDeadline(remaining)(versionTwoRow).toHaveCount(0);
    await withinDeadline(remaining)(versionTwoRow.getByRole("button", { name: /저장$/ })).toHaveCount(0);

    const unfilteredBankResponse = armPaperBankResponse(page, topicA, remaining);
    await collectionFilter.selectOption("");
    expect((await unfilteredBankResponse).ok()).toBe(true);
    await withinDeadline(remaining)(collectionFilter).toHaveValue("");
    await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);
    await withinDeadline(remaining)(
      page.getByRole("combobox", { name: "저장 대상 컬렉션" }),
    ).toHaveValue(collectionId);
    const saveButton = versionTwoRow.getByRole("button", { name: /저장$/ });
    await withinDeadline(remaining)(saveButton).toBeVisible();
    await withinDeadline(remaining)(saveButton).toBeEnabled();

    const restoreResponse = page.waitForResponse(
      (response) => response.request().method() === "PUT"
        && response.url().includes("/paper-collections/" + collectionId + "/members/"),
      { timeout: remaining() },
    );
    const restoreBankRefresh = armPaperBankResponse(page, topicA, remaining);
    const restoreCollectionsRefresh = armCollectionsResponse(page, topicA, remaining);
    await saveButton.click();
    expect((await restoreResponse).ok()).toBe(true);
    expect((await restoreBankRefresh).ok()).toBe(true);
    const restoredCollectionsPayload = await (await restoreCollectionsRefresh).json() as {
      items: Array<{ id: string; memberCount: number }>;
    };
    expect(restoredCollectionsPayload.items.find((item) => item.id === collectionId)?.memberCount).toBe(1);

    const refilteredBankResponse = armPaperBankResponse(page, topicA, remaining);
    await collectionFilter.selectOption(collectionId);
    expect((await refilteredBankResponse).ok()).toBe(true);
    await withinDeadline(remaining)(collectionFilter).toHaveValue(collectionId);
    await withinDeadline(remaining)(versionTwoRow).toHaveCount(1);
    await withinDeadline(remaining)(versionTwoRow.getByRole("button", { name: /저장 해제$/ })).toBeVisible();

    const requestCountBeforeRestart = ledger.requestTimings.length;
    const restartStartedAt = Date.now();
    await backend.restart();
    const restartReadyMs = Date.now() - restartStartedAt;

    const restartNavigation = await navigateToPapers(page, deepLink, topicA, remaining, {
      detailPaperVersionId: versionTwo.paperVersionId,
    });
    const restartRequests = ledger.requestTimings.slice(requestCountBeforeRestart);
    const dashboardTiming = restartRequests.find(
      (item) => item.pathname === "/api/topics/" + topicA + "/dashboard",
    );
    const paperBankTiming = restartRequests.find(
      (item) => item.pathname === "/api/topics/" + topicA + "/paper-bank",
    );
    const collectionsTiming = restartRequests.find(
      (item) => item.pathname === "/api/topics/" + topicA + "/paper-collections",
    );
    expect(dashboardTiming).toBeDefined();
    expect(paperBankTiming).toBeDefined();
    expect(collectionsTiming).toBeDefined();
    const dashboardFinishedAtPapersReadyMs = dashboardTiming!.finishedAtMs;
    const dashboardStateAtPapersReady = dashboardFinishedAtPapersReadyMs === null
      ? "in_flight"
      : dashboardTiming!.status !== null && dashboardTiming!.status < 400
        ? "finished"
        : "handled_failure";

    const table = page.getByRole("table", { name: "논문 목록" });
    await withinDeadline(remaining)(table).toBeVisible();
    const tableVisibleMs = Date.now() - restartStartedAt;
    await withinDeadline(remaining)(
      page.getByRole("combobox", { name: "표시할 컬렉션", exact: true }),
    ).toHaveValue(collectionId);
    const collectionRestoredMs = Date.now() - restartStartedAt;
    await withinDeadline(remaining)(page.getByRole("region", { name: "논문 상세" })).toBeVisible();
    const detailRestoredMs = Date.now() - restartStartedAt;
    await withinDeadline(remaining)(page.getByText("m2-evidence-v2-001")).toBeVisible();
    const evidenceRestoredMs = Date.now() - restartStartedAt;

    const restoredBank = await getPaperBank(page);
    expect(restoredBank.total).toBe(initialBank.total);
    const restoredVersionTwo = restoredBank.items.find(
      (item) => item.paperVersionId === versionTwo.paperVersionId,
    );
    expect(restoredVersionTwo?.canonicalPaperId).toBe(versionTwo.canonicalPaperId);
    expect(restoredVersionTwo?.collectionIds).toContain(collectionId);
    expect(restoredBank.items.find(
      (item) => item.sourceIdentity.nativeId === "2608.00001v1",
    )?.compatibility.occurrences).toHaveLength(2);

    const fallbackRows = restoredBank.items.filter((item) => item.title === "Fallback Collision Paper");
    expect(fallbackRows).toHaveLength(2);
    const fallbackA = fallbackRows.find(
      (item) => item.compatibility.paperId === "occ-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )!;
    const fallbackB = fallbackRows.find(
      (item) => item.compatibility.paperId === "occ-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )!;
    expect(fallbackA.canonicalPaperId).not.toBe(fallbackB.canonicalPaperId);
    expect(fallbackA.paperVersionId).not.toBe(fallbackB.paperVersionId);

    const noDiscriminatorRows = restoredBank.items.filter(
      (item) => item.title === "No Discriminator Identical Paper",
    );
    expect(noDiscriminatorRows).toHaveLength(2);
    expect(noDiscriminatorRows[0].compatibility.paperId).not.toBe(
      noDiscriminatorRows[1].compatibility.paperId,
    );
    expect(noDiscriminatorRows[0].canonicalPaperId).not.toBe(
      noDiscriminatorRows[1].canonicalPaperId,
    );

    const collectionsResponse = await page.request.get(
      "/api/topics/" + topicA + "/paper-collections",
    );
    expect(collectionsResponse.ok()).toBe(true);
    const collections = await collectionsResponse.json() as {
      items: Array<{ id: string; name: string; memberCount: number }>;
    };
    expect(collections.items.filter((item) => item.id === "saved-papers")).toHaveLength(1);
    expect(collections.items.filter((item) => item.id === collectionId)).toEqual([
      expect.objectContaining({ name: "Restart Persistence Queue", memberCount: 1 }),
    ]);

    const fallbackDetailA = await (
      await page.request.get("/api/topics/" + topicA + "/paper-bank/" + fallbackA.paperVersionId)
    ).json() as { evidence: Array<{ id: string }> };
    const fallbackDetailB = await (
      await page.request.get("/api/topics/" + topicA + "/paper-bank/" + fallbackB.paperVersionId)
    ).json() as { evidence: unknown[] };
    expect(fallbackDetailA.evidence.map((item) => item.id)).toEqual(["m2-fallback-evidence-a"]);
    expect(fallbackDetailB.evidence).toEqual([]);
    const reviewedNoDiscriminator = noDiscriminatorRows.find((item) => item.review.state === "full_text_reviewed")!;
    const unreviewedNoDiscriminator = noDiscriminatorRows.find((item) => item.review.state === "not_reviewed")!;
    const noDiscriminatorDetailA = await (
      await page.request.get(
        "/api/topics/" + topicA + "/paper-bank/" + reviewedNoDiscriminator.paperVersionId,
      )
    ).json() as { evidence: Array<{ id: string }> };
    const noDiscriminatorDetailB = await (
      await page.request.get(
        "/api/topics/" + topicA + "/paper-bank/" + unreviewedNoDiscriminator.paperVersionId,
      )
    ).json() as { evidence: unknown[] };
    expect(noDiscriminatorDetailA.evidence.map((item) => item.id)).toEqual([
      "m2-no-discriminator-evidence-a",
    ]);
    expect(noDiscriminatorDetailB.evidence).toEqual([]);

    const finalCollections = await (
      await page.request.get("/api/topics/" + topicA + "/paper-collections")
    ).json() as { items: Array<{ id: string; memberCount: number }> };
    expect(finalCollections.items.find((item) => item.id === collectionId)?.memberCount).toBe(1);
    const topicBPayload = await (
      await page.request.get("/api/topics/" + topicB + "/paper-bank?pageSize=100")
    ).json() as { items: Array<{ paperVersionId: string }> };
    expect(topicBPayload.items.map((item) => item.paperVersionId)).not.toContain(versionTwo.paperVersionId);

    await expect.poll(
      () => dashboardTiming!.finishedAtMs,
      { message: "independent dashboard request must settle", timeout: remaining() },
    ).not.toBeNull();
    expect(dashboardTiming!.status).toBe(200);
    expect(paperBankTiming!.status).toBe(200);
    expect(collectionsTiming!.status).toBe(200);
    expect(backend.launchEvents).toHaveLength(2);
    await assertHygiene(ledger);

    backend.writeJourneyResult({
      journey: "restart-persistence",
      migrationIds: migrationIds([13, 13], [67, 75], [79, 79]),
      assertions: {
        sameDataRoot: true,
        immediateTruthfulState: true,
        exactDeepLinkRestored: true,
        collectionMembershipRestored: true,
        detailAndEvidenceRestored: true,
        identityStableAcrossRestart: true,
        independentDashboardSettlement: true,
        filteredRemovalTruth: true,
        unfilteredUiReadd: true,
        namedSaveTargetRetainedAcrossFilterClear: true,
        collectionMutationRoundTrip: true,
      },
      timing: {
        restartReadyMs,
        routeVisibleMs: restartNavigation.routeVisibleMs,
        initialTruthVisibleMs: restartNavigation.initialTruthVisibleMs,
        paperBankResponseMs: restartNavigation.paperBankResponseMs,
        tableVisibleMs,
        collectionRestoredMs,
        detailRestoredMs,
        evidenceRestoredMs,
        dashboardStateAtPapersReady,
        dashboardFinishedAtPapersReadyMs,
        dashboardFinishedAtJourneyEndMs: dashboardTiming!.finishedAtMs,
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
