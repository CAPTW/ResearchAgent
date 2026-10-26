import { remainingBudget, test, topicA, topicB, withinDeadline } from "./m2-paper-bank.real-backend.helpers";

for (const [connector, modelLabel] of [["grok_subscription", "Grok Build · 구독 계정 모델"], ["anthropic_api", "Claude · Anthropic API 키 모델"]]) {
test(`model connection ${connector} survives topic change and backend restart without external execution`, async ({ page, backend }, testInfo) => {
  const expect = withinDeadline(remainingBudget(testInfo));
  const openedAt = Date.now();
  const external: string[] = [];
  await page.context().route("**/*", async route => {
    if (new URL(route.request().url()).origin !== "http://127.0.0.1:8002") {
      external.push(route.request().url()); await route.abort(); return;
    }
    await route.continue();
  });
  await page.goto(`/app/topics/${topicA}/settings`);
  const panel = page.getByRole("region", { name: "모델 연결 설정", exact: true });
  const selection = panel.getByRole("combobox", { name: "기본 모델 연결" });
  await expect(selection).toHaveValue("codex_subscription");
  console.info(`model connection panel ready: ${Date.now() - openedAt}ms`);
  await expect(panel.getByText("실행 권한: 미등록", { exact: true })).toBeVisible();
  await expect(panel.getByRole("group", { name: "Claude Code · 구독", exact: true }).getByText("연결 어댑터 지원", { exact: true })).toBeVisible();
  await expect(panel.getByRole("group", { name: "Claude Code · 구독", exact: true }).getByText(/논문 검토·Journal CLI 어댑터 준비됨/)).toBeVisible();
  const antigravity = panel.getByRole("group", { name: "Antigravity · Google 계정", exact: true });
  await expect(antigravity.getByText("연결 어댑터 지원", { exact: true })).toBeVisible();
  await expect(antigravity.getByText(/논문 검토·Journal CLI 어댑터 준비됨/)).toBeVisible();
  await expect(antigravity.getByText(/전역 설정 및 Windows 격리 검증은 아직 미완료/)).toBeVisible();
  const grok = panel.getByRole("group", { name: "Grok Build · 구독 계정", exact: true });
  await expect(grok.getByText("연결 어댑터 지원", { exact: true })).toBeVisible();
  await expect(grok.getByText(/논문 검토·Journal ACP 어댑터 준비됨/)).toBeVisible();
  await expect(grok.getByText(/CLI 내부 도구의 완전 차단은 보장되지 않습니다/)).toBeVisible();
  await expect(panel.getByRole("group", { name: "Grok · xAI API 키", exact: true }).getByText(/논문 검토·Journal Responses 어댑터 준비됨/)).toBeVisible();
  await expect(panel.getByRole("group", { name: "Claude · Anthropic API 키", exact: true }).getByText(/논문 검토·Journal Messages 어댑터 준비됨/)).toBeVisible();
  await selection.selectOption(connector);
  const input = panel.getByRole("textbox", { name: modelLabel, exact: true });
  await input.fill("synthetic-browser-model");
  // Keyboard submits the actual form; no key, auth token or model call is sent.
  await input.press("Enter");
  await expect(panel.getByText(/모델 연결 설정을 저장했습니다/)).toBeVisible();
  const saved = await (await page.request.get("/settings/model-connections")).json();
  expect(saved.default_connector_id).toBe(connector);
  expect(saved.execution_allowed).toBe(false);
  await page.goto(`/app/topics/${topicB}/settings`);
  await expect(selection).toHaveValue(connector);
  await expect(input).toHaveValue("synthetic-browser-model");
  await backend.restart();
  await page.reload();
  await expect(selection).toHaveValue(connector);
  await expect(input).toHaveValue("synthetic-browser-model");
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  await expect(panel.getByRole("button", { name: "모델 연결 설정 저장", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await selection.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("model-connections-narrow.png") });
  expect(external).toEqual([]);
});
}

test("stale model connection save preserves input and requires refreshing current revision", async ({ page }, testInfo) => {
  const expect = withinDeadline(remainingBudget(testInfo));
  const openedAt = Date.now();
  await page.goto(`/app/topics/${topicA}/settings`);
  const panel = page.getByRole("region", { name: "모델 연결 설정", exact: true });
  const selection = panel.getByRole("combobox", { name: "기본 모델 연결" });
  await expect(selection).toHaveValue("codex_subscription");
  console.info(`model connection panel ready: ${Date.now() - openedAt}ms`);
  const current = await (await page.request.get("/settings/model-connections")).json();
  const input = panel.getByRole("textbox", { name: "OpenAI · API 키 모델", exact: true });
  await input.fill("preserve-my-model");
  const response = await page.request.put("/settings/model-connections", { data: {
    expected_revision: current.revision, default_connector_id: "xai_api", models: { xai_api: "other-window-model" },
  } });
  expect(response.status()).toBe(200);
  const save = panel.getByRole("button", { name: "모델 연결 설정 저장", exact: true });
  await save.click();
  await expect(panel.getByRole("alert")).toContainText("다른 곳에서 설정이 변경되었습니다");
  await expect(input).toHaveValue("preserve-my-model");
  await expect(save).toBeDisabled();
  await panel.getByRole("button", { name: "모델 연결 상태 새로고침" }).click();
  await expect(panel.getByText(/최신 저장 상태를 확인했습니다/)).toBeVisible();
  await expect(input).toHaveValue("preserve-my-model");
  await selection.selectOption("openai_api");
  await save.click();
  await expect(panel.getByText(/모델 연결 설정을 저장했습니다/)).toBeVisible();
  expect((await (await page.request.get("/settings/model-connections")).json()).default_connector_id).toBe("openai_api");
});
