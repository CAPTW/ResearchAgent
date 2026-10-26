import {
  armCollectionsResponse,
  armDetailResponse,
  armPaperBankResponse,
  assertHygiene,
  expect,
  getPaperBank,
  installLedger,
  migrationIds,
  remainingBudget,
  test,
  topicA,
  versionTwoFrom,
  withinDeadline,
} from "./m2-paper-bank.real-backend.helpers";

const themes = ["system", "light", "dark", "solarized-light", "solarized-dark"] as const;
const densities = ["comfortable", "compact"] as const;

test("M2 appearance and accessibility remain stable on one mounted workspace", async ({ page, context, backend }, testInfo) => {
  const startedAt = Date.now();
  const remaining = remainingBudget(testInfo, startedAt);
  const ledger = await installLedger(context, page);
  const bank = await getPaperBank(page);
  const versionTwo = versionTwoFrom(bank);

  await page.goto("/app/topics/" + topicA + "/settings", {
    waitUntil: "domcontentloaded",
    timeout: remaining(),
  });
  const settingsReady = page.locator('[data-settings-ready="true"]');
  await withinDeadline(remaining)(settingsReady).toHaveCount(1);
  const settings = page.locator(".v2-settings-screen");
  await withinDeadline(remaining)(settings).toBeVisible();
  const themeSelect = page.getByRole("combobox", { name: "테마", exact: true });
  const densitySelect = page.getByRole("combobox", { name: "화면 밀도" });
  const motionSelect = page.getByRole("combobox", { name: "동작 설정" });
  await withinDeadline(remaining)(themeSelect).toBeVisible();
  await withinDeadline(remaining)(densitySelect).toBeVisible();
  await withinDeadline(remaining)(motionSelect).toBeVisible();

  let fullDocumentLoadCount = 0;
  page.on("load", () => {
    fullDocumentLoadCount += 1;
  });

  await motionSelect.selectOption("reduced");
  await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-motion", "reduced");
  const checkedCombinations: string[] = [];
  for (const theme of themes) {
    for (const density of densities) {
      await themeSelect.selectOption(theme);
      await densitySelect.selectOption(density);
      const expectedTheme = theme === "system" ? "dark" : theme;
      await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-theme", expectedTheme);
      await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-density", density);
      await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-motion", "reduced");
      const overflow = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        settings: (() => {
          const element = document.querySelector<HTMLElement>(".v2-settings-screen");
          return element ? element.scrollWidth - element.clientWidth : 999;
        })(),
      }));
      expect(overflow.document, theme + "/" + density + " document overflow").toBeLessThanOrEqual(1);
      expect(overflow.settings, theme + "/" + density + " settings overflow").toBeLessThanOrEqual(1);
      checkedCombinations.push(theme + "/" + density);
    }
  }
  expect(checkedCombinations).toHaveLength(10);
  expect(new Set(checkedCombinations).size).toBe(10);
  expect(fullDocumentLoadCount).toBe(0);

  await page.reload({ waitUntil: "domcontentloaded", timeout: remaining() });
  await withinDeadline(remaining)(settings).toBeVisible();
  expect(fullDocumentLoadCount).toBe(1);
  await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-theme", "solarized-dark");
  await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-density", "compact");
  await withinDeadline(remaining)(page.locator("html")).toHaveAttribute("data-motion", "reduced");
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "테마", exact: true })).toHaveValue("solarized-dark");
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "화면 밀도" })).toHaveValue("compact");
  await withinDeadline(remaining)(page.getByRole("combobox", { name: "동작 설정" })).toHaveValue("reduced");

  const paperBankResponse = armPaperBankResponse(page, topicA, remaining);
  const collectionsResponse = armCollectionsResponse(page, topicA, remaining);
  await page.getByRole("link", { name: "논문 목록" }).click();
  expect((await paperBankResponse).ok()).toBe(true);
  expect((await collectionsResponse).ok()).toBe(true);
  const table = page.getByRole("table", { name: "논문 목록" });
  await withinDeadline(remaining)(table).toBeVisible();
  await withinDeadline(remaining)(page).toHaveURL(new RegExp("/topics/" + topicA + "/papers"));
  expect(fullDocumentLoadCount).toBe(1);
  const papersOverflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    workspace: (() => {
      const element = document.querySelector<HTMLElement>(".v2-papers-layout");
      return element ? element.scrollWidth - element.clientWidth : 999;
    })(),
  }));
  expect(papersOverflow.document).toBeLessThanOrEqual(1);
  expect(papersOverflow.workspace).toBeLessThanOrEqual(1);

  const paperSearch = page.getByRole("textbox", { name: "논문 제목, 저자, 키워드 검색" });
  await paperSearch.focus();
  await withinDeadline(remaining)(paperSearch).toBeFocused();
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => document.activeElement !== document.body)).toBe(true);
  expect(await page.evaluate(() => document.activeElement?.matches(":focus-visible") ?? false)).toBe(true);

  const titleButton = page.getByRole("button", { name: "Spatial Evidence Bank Foundations 상세 열기" }).first();
  await titleButton.focus();
  await withinDeadline(remaining)(titleButton).toBeFocused();
  const detailResponse = armDetailResponse(page, topicA, versionTwo.paperVersionId, remaining);
  await titleButton.press("Enter");
  expect((await detailResponse).ok()).toBe(true);
  const detail = page.getByRole("region", { name: "논문 상세" });
  await withinDeadline(remaining)(detail).toBeVisible();
  await withinDeadline(remaining)(detail.getByText("m2-evidence-v2-001")).toBeVisible();
  await detail.press("Escape");
  await withinDeadline(remaining)(detail).toHaveCount(0);
  await withinDeadline(remaining)(titleButton).toBeFocused();

  await assertHygiene(ledger);
  backend.writeJourneyResult({
    journey: "appearance-accessibility",
    migrationIds: migrationIds([8, 12]),
    assertions: {
      oneMountedSettingsScreen: true,
      themeDensityCombinations: checkedCombinations.length,
      deliberatePersistenceReloads: fullDocumentLoadCount,
      appearancePersisted: true,
      settingsOverflowFree: true,
      papersOverflowFree: true,
      keyboardFocusEscape: true,
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
