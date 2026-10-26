import fs from "node:fs";
import path from "node:path";

import {
  armDetailResponse,
  assertHygiene,
  expect,
  getPaperBank,
  installLedger,
  migrationIds,
  navigateToPapers,
  remainingBudget,
  rewriteFirstOccurrenceId,
  test,
  topicA,
  topicB,
  windowsAbsolutePath,
  withinDeadline,
} from "./m2-paper-bank.real-backend.helpers";

test("M2 occurrence identity and integrity remain fail-closed", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  let fallbackCollectionId: string | null = null;
  let noDiscriminatorCollectionId: string | null = null;
  const triagePath = path.join(
    backend.dataRoot,
    "runs",
    "m2-paper-bank-topic-a-run-1",
    "triage",
    "triage.jsonl",
  );
  const originalTriage = fs.readFileSync(triagePath, "utf8");

  try {
    const bank = await getPaperBank(page);
    const fallbackRows = bank.items.filter((item) => item.title === "Fallback Collision Paper");
    expect(fallbackRows).toHaveLength(2);
    const fallbackA = fallbackRows.find(
      (item) => item.compatibility.paperId === "occ-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    );
    const fallbackB = fallbackRows.find(
      (item) => item.compatibility.paperId === "occ-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    );
    expect(fallbackA).toBeTruthy();
    expect(fallbackB).toBeTruthy();
    expect(fallbackA!.canonicalPaperId).not.toBe(fallbackB!.canonicalPaperId);
    expect(fallbackA!.paperVersionId).not.toBe(fallbackB!.paperVersionId);
    expect(fallbackA!.review).toEqual(expect.objectContaining({ state: "full_text_reviewed", evidenceCount: 1 }));
    expect(fallbackB!.review).toEqual(expect.objectContaining({ state: "not_reviewed", evidenceCount: 0 }));

    const fallbackDetailAResponse = await page.request.get(
      "/api/topics/" + topicA + "/paper-bank/" + fallbackA!.paperVersionId,
    );
    const fallbackDetailBResponse = await page.request.get(
      "/api/topics/" + topicA + "/paper-bank/" + fallbackB!.paperVersionId,
    );
    expect(fallbackDetailAResponse.ok()).toBe(true);
    expect(fallbackDetailBResponse.ok()).toBe(true);
    const fallbackDetailA = await fallbackDetailAResponse.json() as {
      authors: string[];
      evidence: Array<{ id: string; locator: unknown }>;
      fullText: { state: string };
    };
    const fallbackDetailB = await fallbackDetailBResponse.json() as {
      authors: string[];
      evidence: unknown[];
      fullText: { state: string };
    };
    expect(fallbackDetailA.authors).toEqual(["Fallback Author A"]);
    expect(fallbackDetailA.evidence).toEqual([
      expect.objectContaining({ id: "m2-fallback-evidence-a", locator: { page: 7, section: "Occurrence A" } }),
    ]);
    expect(fallbackDetailA.fullText.state).toBe("reviewed");
    expect(fallbackDetailB.authors).toEqual(["Fallback Author B"]);
    expect(fallbackDetailB.evidence).toEqual([]);
    expect(fallbackDetailB.fullText.state).toBe("unavailable");

    const noDiscriminatorRows = bank.items
      .filter((item) => item.title === "No Discriminator Identical Paper")
      .sort((left, right) => left.compatibility.paperId.localeCompare(right.compatibility.paperId));
    expect(noDiscriminatorRows).toHaveLength(2);
    const [noDiscriminatorA, noDiscriminatorB] = noDiscriminatorRows;
    expect(noDiscriminatorA.compatibility.paperId).toMatch(/^occ-legacy-[0-9a-f]{32}$/);
    expect(noDiscriminatorB.compatibility.paperId).toMatch(/^occ-legacy-[0-9a-f]{32}$/);
    expect(noDiscriminatorA.compatibility.paperId).not.toBe(noDiscriminatorB.compatibility.paperId);
    expect(noDiscriminatorA.canonicalPaperId).not.toBe(noDiscriminatorB.canonicalPaperId);
    expect(noDiscriminatorA.paperVersionId).not.toBe(noDiscriminatorB.paperVersionId);
    const reviewedNoDiscriminator = noDiscriminatorRows.find((item) => item.review.state === "full_text_reviewed")!;
    const unreviewedNoDiscriminator = noDiscriminatorRows.find((item) => item.review.state === "not_reviewed")!;
    expect(reviewedNoDiscriminator.review.evidenceCount).toBe(1);
    expect(unreviewedNoDiscriminator.review.evidenceCount).toBe(0);

    const noDiscriminatorDetailA = await (
      await page.request.get(
        "/api/topics/" + topicA + "/paper-bank/" + reviewedNoDiscriminator.paperVersionId,
      )
    ).json() as { evidence: Array<{ id: string; locator: unknown }>; fullText: { state: string } };
    const noDiscriminatorDetailB = await (
      await page.request.get(
        "/api/topics/" + topicA + "/paper-bank/" + unreviewedNoDiscriminator.paperVersionId,
      )
    ).json() as { evidence: unknown[]; fullText: { state: string } };
    expect(noDiscriminatorDetailA.fullText.state).toBe("reviewed");
    expect(noDiscriminatorDetailA.evidence).toEqual([
      expect.objectContaining({
        id: "m2-no-discriminator-evidence-a",
        locator: { page: 11, section: "Occurrence authority" },
      }),
    ]);
    expect(noDiscriminatorDetailB.fullText.state).toBe("unavailable");
    expect(noDiscriminatorDetailB.evidence).toEqual([]);

    const serializedBank = JSON.stringify(bank);
    expect(serializedBank).not.toMatch(windowsAbsolutePath);
    expect(serializedBank.toLowerCase()).not.toContain("file://");
    expect(serializedBank).not.toContain("\\\\?\\");
    expect(serializedBank).not.toContain("\\\\.\\");

    const fallbackCollectionResponse = await page.request.post(
      "/api/topics/" + topicA + "/paper-collections",
      { data: { name: "Fallback A only" } },
    );
    expect(fallbackCollectionResponse.status()).toBe(201);
    fallbackCollectionId = (await fallbackCollectionResponse.json() as { id: string }).id;
    expect((await page.request.put(
      "/api/topics/" + topicA + "/paper-collections/" + fallbackCollectionId
        + "/members/" + fallbackA!.paperVersionId,
    )).ok()).toBe(true);
    const fallbackFiltered = await (
      await page.request.get(
        "/api/topics/" + topicA + "/paper-bank?collectionId="
          + encodeURIComponent(fallbackCollectionId) + "&pageSize=100",
      )
    ).json() as { items: Array<{ paperVersionId: string }> };
    expect(fallbackFiltered.items.map((item) => item.paperVersionId)).toEqual([fallbackA!.paperVersionId]);

    const noDiscriminatorCollectionResponse = await page.request.post(
      "/api/topics/" + topicA + "/paper-collections",
      { data: { name: "No discriminator A only" } },
    );
    expect(noDiscriminatorCollectionResponse.status()).toBe(201);
    noDiscriminatorCollectionId = (await noDiscriminatorCollectionResponse.json() as { id: string }).id;
    expect((await page.request.put(
      "/api/topics/" + topicA + "/paper-collections/" + noDiscriminatorCollectionId
        + "/members/" + reviewedNoDiscriminator.paperVersionId,
    )).ok()).toBe(true);
    const noDiscriminatorFiltered = await (
      await page.request.get(
        "/api/topics/" + topicA + "/paper-bank?collectionId="
          + encodeURIComponent(noDiscriminatorCollectionId) + "&pageSize=100",
      )
    ).json() as { items: Array<{ paperVersionId: string }> };
    expect(noDiscriminatorFiltered.items.map((item) => item.paperVersionId)).toEqual([
      reviewedNoDiscriminator.paperVersionId,
    ]);

    const reviewedOnly = await (
      await page.request.get(
        "/api/topics/" + topicA
          + "/paper-bank?q=No%20Discriminator%20Identical%20Paper&reviewState=full_text_reviewed&pageSize=100",
      )
    ).json() as { items: Array<{ paperVersionId: string }> };
    expect(reviewedOnly.items.map((item) => item.paperVersionId)).toEqual([
      reviewedNoDiscriminator.paperVersionId,
    ]);

    const pageOne = await (
      await page.request.get(
        "/api/topics/" + topicA
          + "/paper-bank?q=No%20Discriminator%20Identical%20Paper&sort=title_asc&page=1&pageSize=1",
      )
    ).json() as { items: Array<{ paperVersionId: string }>; queryDigest: string };
    const pageTwo = await (
      await page.request.get(
        "/api/topics/" + topicA
          + "/paper-bank?q=No%20Discriminator%20Identical%20Paper&sort=title_asc&page=2&pageSize=1",
      )
    ).json() as { items: Array<{ paperVersionId: string }>; queryDigest: string };
    expect(new Set([pageOne.items[0].paperVersionId, pageTwo.items[0].paperVersionId])).toEqual(
      new Set(noDiscriminatorRows.map((item) => item.paperVersionId)),
    );
    expect(pageOne.queryDigest).not.toBe(pageTwo.queryDigest);

    await navigateToPapers(
      page,
      "/app/topics/" + topicA + "/papers?query=Fallback%20Collision%20Paper",
      topicA,
      remaining,
    );
    const fallbackUiRows = page.getByRole("row").filter({ hasText: "Fallback Collision Paper" });
    await withinDeadline(remaining)(fallbackUiRows).toHaveCount(2);
    const fallbackUiRowA = fallbackUiRows.filter({ hasText: "Fallback Author A" });
    const fallbackUiRowB = fallbackUiRows.filter({ hasText: "Fallback Author B" });
    const fallbackAUiResponse = armDetailResponse(page, topicA, fallbackA!.paperVersionId, remaining);
    await fallbackUiRowA.getByRole("button", { name: "Fallback Collision Paper 상세 열기" }).click();
    expect((await fallbackAUiResponse).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(new RegExp("paper=" + fallbackA!.paperVersionId));
    await withinDeadline(remaining)(page.getByText("m2-fallback-evidence-a")).toBeVisible();
    await page.getByRole("button", { name: "논문 상세 닫기" }).click();
    const fallbackBUiResponse = armDetailResponse(page, topicA, fallbackB!.paperVersionId, remaining);
    await fallbackUiRowB.getByRole("button", { name: "Fallback Collision Paper 상세 열기" }).click();
    expect((await fallbackBUiResponse).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(new RegExp("paper=" + fallbackB!.paperVersionId));
    await withinDeadline(remaining)(page.getByText("연결된 evidence가 없습니다.")).toBeVisible();

    await navigateToPapers(
      page,
      "/app/topics/" + topicA + "/papers?query=No%20Discriminator%20Identical%20Paper",
      topicA,
      remaining,
    );
    const noDiscriminatorUiRows = page.getByRole("row").filter({ hasText: "No Discriminator Identical Paper" });
    await withinDeadline(remaining)(noDiscriminatorUiRows).toHaveCount(2);
    const reviewedUiRow = noDiscriminatorUiRows.filter({
      hasText: reviewedNoDiscriminator.compatibility.paperId,
    });
    const unreviewedUiRow = noDiscriminatorUiRows.filter({
      hasText: unreviewedNoDiscriminator.compatibility.paperId,
    });
    const reviewedUiResponse = armDetailResponse(
      page,
      topicA,
      reviewedNoDiscriminator.paperVersionId,
      remaining,
    );
    await reviewedUiRow.getByRole("button", { name: "No Discriminator Identical Paper 상세 열기" }).click();
    expect((await reviewedUiResponse).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(
      new RegExp("paper=" + reviewedNoDiscriminator.paperVersionId),
    );
    await withinDeadline(remaining)(page.getByText("m2-no-discriminator-evidence-a")).toBeVisible();
    await page.getByRole("button", { name: "논문 상세 닫기" }).click();
    const unreviewedUiResponse = armDetailResponse(
      page,
      topicA,
      unreviewedNoDiscriminator.paperVersionId,
      remaining,
    );
    await unreviewedUiRow.getByRole("button", { name: "No Discriminator Identical Paper 상세 열기" }).click();
    expect((await unreviewedUiResponse).ok()).toBe(true);
    await withinDeadline(remaining)(page).toHaveURL(
      new RegExp("paper=" + unreviewedNoDiscriminator.paperVersionId),
    );
    await withinDeadline(remaining)(page.getByText("연결된 evidence가 없습니다.")).toBeVisible();
    await withinDeadline(remaining)(page.locator("body")).not.toContainText(windowsAbsolutePath);

    const topicBResponse = await page.request.get("/api/topics/" + topicB + "/paper-bank?pageSize=100");
    expect(topicBResponse.ok()).toBe(true);
    const topicBPayload = await topicBResponse.json() as { items: Array<{ paperVersionId: string }> };
    expect(topicBPayload.items.map((item) => item.paperVersionId)).not.toContain(fallbackA!.paperVersionId);

    const malformedResults: Array<{ kind: string; status: number }> = [];
    for (const malformedCase of [
      { kind: "generated_like", value: "occ-" + "a".repeat(31) },
      { kind: "legacy_like", value: "occ-legacy-" + "b".repeat(31) },
      { kind: "arbitrary_scalar", value: 17 },
    ]) {
      try {
        fs.writeFileSync(triagePath, rewriteFirstOccurrenceId(originalTriage, malformedCase.value), "utf8");
        const malformedResponse = await page.request.get("/api/topics/" + topicA + "/paper-bank?pageSize=100");
        expect(malformedResponse.status()).toBe(503);
        expect(await malformedResponse.json()).toEqual({ detail: "occurrence_identity_invalid" });
        malformedResults.push({ kind: malformedCase.kind, status: malformedResponse.status() });
      } finally {
        fs.writeFileSync(triagePath, originalTriage, "utf8");
      }
    }
    expect(malformedResults).toEqual([
      { kind: "generated_like", status: 503 },
      { kind: "legacy_like", status: 503 },
      { kind: "arbitrary_scalar", status: 503 },
    ]);

    const triageRecords = originalTriage
      .split(/\r?\n/u)
      .filter((line) => line.trim().length > 0)
      .map((line) => JSON.parse(line) as Record<string, unknown>);
    const legacyRecord = triageRecords.find(
      (record) => record.title === "No Discriminator Identical Paper"
        && !("occurrence_id" in record),
    );
    expect(legacyRecord).toBeTruthy();
    expect(legacyRecord).not.toHaveProperty("occurrence_id");
    try {
      fs.appendFileSync(triagePath, JSON.stringify(legacyRecord!) + "\n", "utf8");
      const legacyCopyResponse = await page.request.get(
        "/api/topics/" + topicA + "/paper-bank?pageSize=100",
      );
      expect(legacyCopyResponse.status()).toBe(200);
      const legacyCopyBank = await legacyCopyResponse.json() as typeof bank;
      const legacyCopyRows = legacyCopyBank.items.filter(
        (item) => item.title === "No Discriminator Identical Paper",
      );
      expect(legacyCopyRows).toHaveLength(3);
      expect(new Set(legacyCopyRows.map((item) => item.compatibility.paperId)).size).toBe(3);
      expect(new Set(legacyCopyRows.map((item) => item.canonicalPaperId)).size).toBe(3);
      expect(new Set(legacyCopyRows.map((item) => item.paperVersionId)).size).toBe(3);
      expect(legacyCopyRows.filter((item) => item.review.state === "full_text_reviewed")).toEqual([
        expect.objectContaining({
          paperVersionId: reviewedNoDiscriminator.paperVersionId,
          review: expect.objectContaining({ evidenceCount: 1 }),
        }),
      ]);
      expect(legacyCopyRows.filter((item) => item.review.state === "not_reviewed")).toHaveLength(2);
      const legacyCopyCollection = await (
        await page.request.get(
          "/api/topics/" + topicA + "/paper-bank?collectionId="
            + encodeURIComponent(noDiscriminatorCollectionId) + "&pageSize=100",
        )
      ).json() as { items: Array<{ paperVersionId: string }> };
      expect(legacyCopyCollection.items.map((item) => item.paperVersionId)).toEqual([
        reviewedNoDiscriminator.paperVersionId,
      ]);
      const fallbackAfterLegacyCopy = legacyCopyBank.items.filter(
        (item) => item.title === "Fallback Collision Paper",
      );
      expect(fallbackAfterLegacyCopy).toHaveLength(2);
      expect(fallbackAfterLegacyCopy.find(
        (item) => item.compatibility.paperId === fallbackA!.compatibility.paperId,
      )?.review).toEqual(expect.objectContaining({ state: "full_text_reviewed", evidenceCount: 1 }));
      expect(fallbackAfterLegacyCopy.find(
        (item) => item.compatibility.paperId === fallbackB!.compatibility.paperId,
      )?.review).toEqual(expect.objectContaining({ state: "not_reviewed", evidenceCount: 0 }));
    } finally {
      fs.writeFileSync(triagePath, originalTriage, "utf8");
    }

    const duplicate = triageRecords.find(
      (record) => record.title === "Fallback Collision Paper"
        && typeof record.occurrence_id === "string"
        && /^occ-[0-9a-f]{32}$/u.test(record.occurrence_id),
    );
    expect(duplicate).toBeTruthy();
    expect(duplicate!.occurrence_id).toMatch(/^occ-[0-9a-f]{32}$/u);
    const consoleCountBeforeExpectedFailure = ledger.consoleErrors.length;
    try {
      fs.appendFileSync(triagePath, JSON.stringify(duplicate!) + "\n", "utf8");
      const corruptResponse = await page.request.get("/api/topics/" + topicA + "/paper-bank?pageSize=100");
      expect(corruptResponse.status()).toBe(503);
      expect(await corruptResponse.json()).toEqual({ detail: "occurrence_identity_duplicate" });
      const corruptDashboardResponse = await page.request.get("/api/topics/" + topicA + "/dashboard");
      expect(corruptDashboardResponse.status()).toBe(503);
      expect(await corruptDashboardResponse.json()).toEqual({ detail: "occurrence_identity_duplicate" });
      await navigateToPapers(page, "/app/topics/" + topicA + "/papers", topicA, remaining, {
        expectedPaperBankStatus: 503,
      });
      const integrityAlert = page.getByRole("alert");
      await withinDeadline(remaining)(integrityAlert).toContainText("화면을 불러오지 못했습니다.");
      await withinDeadline(remaining)(integrityAlert).toContainText("occurrence_identity_duplicate");
      await withinDeadline(remaining)(page.getByRole("table", { name: "논문 목록" })).toHaveCount(0);
      const expectedErrors = ledger.consoleErrors.splice(consoleCountBeforeExpectedFailure);
      expect(expectedErrors.every((message) => message.includes("503"))).toBe(true);
      ledger.handledExpectedErrors.push(...expectedErrors);
    } finally {
      fs.writeFileSync(triagePath, originalTriage, "utf8");
    }

    expect((await page.request.get("/api/topics/" + topicA + "/paper-bank?pageSize=100")).ok()).toBe(true);
    await assertHygiene(ledger);
    backend.writeJourneyResult({
      journey: "identity-integrity",
      migrationIds: migrationIds([56, 66], [76, 78]),
      assertions: {
        fallbackCollisionDistinct: true,
        noDiscriminatorDistinct: true,
        evidenceIsolation: true,
        collectionIsolation: true,
        stablePagination: true,
        pathHygiene: true,
        malformedIdentityRejected: true,
        legacyCoordinateCopyDistinct: true,
        duplicateIdentityRejected: true,
        dashboardDuplicateIdentityTyped: true,
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
  } finally {
    fs.writeFileSync(triagePath, originalTriage, "utf8");
  }
});
