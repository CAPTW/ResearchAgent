import fs from "node:fs";
import path from "node:path";
import { remainingBudget, test, topicA, topicB, withinDeadline } from "./m2-paper-bank.real-backend.helpers";

test("encrypted API key settings preserve explicit state through conflicts, lost response and restart", async ({ page, backend }, testInfo) => {
  const expect = withinDeadline(remainingBudget(testInfo));
  const external: string[] = [], errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.context().route("**/*", async route => {
    if (new URL(route.request().url()).origin !== "http://127.0.0.1:8002") {
      external.push(route.request().url()); await route.abort(); return;
    }
    await route.continue();
  });
  const url = "/settings/model-connections/anthropic_api/credential";
  const headers = { Origin: "http://127.0.0.1:8002", "X-XRML-Credential-Action": "1" };
  const key = "sk-ant-synthetic-browser-credentials-do-not-use";
  const replacement = "sk-ant-synthetic-browser-replacement-do-not-use";
  await page.goto(`/app/topics/${topicA}/settings`);
  const panel = page.getByRole("region", { name: "모델 연결 설정", exact: true });
  const editor = panel.getByRole("region", { name: "Claude · Anthropic API 키 자격 증명", exact: true });
  const input = editor.getByLabel("Claude · Anthropic API 키 값", { exact: true });
  const save = editor.getByRole("button", { name: "API 키 저장", exact: true });
  const refresh = editor.getByRole("button", { name: "API 키 상태 새로고침", exact: true });
  await expect(editor.getByText("적용 경로: 키 없음", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "Opus 5.5 모델 입력", exact: true }).click();
  const model = panel.getByRole("textbox", { name: "Claude · Anthropic API 키 모델", exact: true });
  await expect(model).toHaveValue("claude-opus-5-5");
  await panel.getByRole("combobox", { name: "기본 모델 연결" }).selectOption("anthropic_api");
  await panel.getByRole("button", { name: "모델 연결 설정 저장", exact: true }).click();
  await expect(panel.getByText(/모델 연결 설정을 저장했습니다/)).toBeVisible();
  await expect(input).toHaveAttribute("type", "password");
  await input.fill(key); await input.press("Enter");
  await expect(editor.getByText(/로컬 암호화 저장소에 저장했습니다/)).toBeVisible();
  await expect(input).toHaveValue("");
  const first = await (await page.request.get(url)).json();
  expect(first.stored).toBe(true); expect(first.execution_allowed).toBe(false);
  expect(JSON.stringify(first)).not.toContain(key);
  const encryptedPath = path.join(backend.dataRoot, "private", "provider-credentials.json");
  const encrypted = fs.readFileSync(encryptedPath, "utf8");
  expect(encrypted).not.toContain(key);
  expect(JSON.parse(encrypted).entries.anthropic_api.ciphertext).toBeTruthy();
  expect(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))).not.toContain(key);
  expect(await page.locator("body").innerText()).not.toContain(key);

  // Genuine server-side conflict preserves the user's newer draft.
  await input.fill(replacement);
  expect((await page.request.put(url, { headers, data: { expected_revision: first.revision, api_key: "synthetic-other-window" } })).status()).toBe(200);
  await save.click();
  await expect(editor.getByRole("alert")).toContainText("다른 곳에서");
  await expect(save).toBeDisabled(); await expect(input).toHaveValue(replacement);
  await refresh.click(); await expect(save).toBeEnabled();
  await expect(input).toHaveValue(replacement);

  // Commit to the real API, then lose only its browser response.
  let lost = false;
  await page.route(`**${url}`, async route => {
    if (route.request().method() === "PUT" && !lost) {
      lost = true;
      const committed = await route.fetch(); expect(committed.status()).toBe(200);
      await route.abort("failed"); return;
    }
    await route.continue();
  });
  await save.click(); await expect(editor.getByRole("alert")).toContainText("변경 결과를 확인하지 못했습니다");
  await expect(save).toBeDisabled(); await expect(input).toHaveValue(replacement);
  const committed = await (await page.request.get(url)).json();
  expect(committed.revision).toBe(first.revision + 2);
  await refresh.click(); await expect(save).toBeEnabled(); await save.click();
  await expect(editor.getByText(/로컬 암호화 저장소에 저장했습니다/)).toBeVisible();
  expect((await (await page.request.get(url)).json()).revision).toBe(committed.revision);
  await expect(input).toHaveValue("");

  await backend.restart(); await page.reload();
  await expect(editor.getByText("적용 경로: 로컬 저장 키", { exact: true })).toBeVisible();
  await expect(model).toHaveValue("claude-opus-5-5"); await expect(input).toHaveValue("");
  expect(await (await page.request.get(url)).json()).toEqual(committed);
  await input.fill("synthetic-unsaved-must-disappear");
  await page.goto(`/app/topics/${topicB}/settings`);
  await expect(input).toHaveValue("");
  await expect(editor.getByText("적용 경로: 로컬 저장 키", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await editor.scrollIntoViewIfNeeded();
  // Resizing animates the desktop sidebar off-screen. Inspect the settled drawer,
  // not an intermediate frame that can temporarily cover the credential form.
  await expect.poll(async () => {
    const bounds = await page.locator("#v2-sidebar").boundingBox();
    return bounds ? bounds.x + bounds.width : Number.POSITIVE_INFINITY;
  }).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("credential-settings-narrow.png") });
  const remove = editor.getByRole("button", { name: "저장된 API 키 삭제", exact: true });
  await remove.focus(); await page.keyboard.press("Enter");
  await expect(editor.getByText("적용 경로: 키 없음", { exact: true })).toBeVisible();
  expect((await (await page.request.get(url)).json()).stored).toBe(false);
  await page.reload(); await expect(editor.getByText("적용 경로: 키 없음", { exact: true })).toBeVisible();
  expect(errors).toEqual([]); expect(external).toEqual([]);
});
