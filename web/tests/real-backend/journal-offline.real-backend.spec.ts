import fs from "node:fs";
import path from "node:path";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("offline approval dispatch human review export and restart use the real app and durable store", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const runs = fs.readdirSync(path.join(backend.dataRoot, "runs")).sort();
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  await page.goto(`/app/topics/${topicA}/papers`);
  await expect(page).toHaveURL(new RegExp(`/app/topics/${topicA}/papers`));
  await expect(page).not.toHaveTitle("");
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  await page.getByRole("table").locator("tbody input[type=checkbox]").first().check();
  await page.getByRole("button", { name: "선택 보고서 입력 고정" }).click();
  const panel = page.getByRole("region", { name: "선택 문헌 근거 초안" });
  await expect(panel.getByText("수동 진행 가능 · 0 / 3 단계")).toBeVisible();
  const job = new URL(page.url()).searchParams.get("synthesis")!;
  const root = `/api/topics/${topicA}/local-synthesis/${job}`;
  for (let step = 1; step <= 3; step++) {
    await panel.getByRole("button", { name: "다음 한 단계 실행" }).click();
    await expect(panel.getByText(`${step === 3 ? "근거 초안 준비됨" : "수동 진행 가능"} · ${step} / 3 단계`)).toBeVisible();
  }
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await results.getByText("새 생성 요청 준비", { exact: true }).click();
  const form = results.getByRole("form", { name: "생성 요청 준비" });
  await form.getByLabel("오프라인 합성 요청 준비").check();
  await expect(form.getByLabel("요청 모델 이름")).toHaveValue("synthetic-journal-v1");
  await form.getByLabel("최대 시도 횟수").fill("2");
  const preparedResponse = page.waitForResponse(r => r.url().endsWith(`${root}/journal-executions`) && r.request().method() === "POST");
  await form.getByRole("button", { name: "로컬 생성 요청 준비" }).click();
  const prepared = (await (await preparedResponse).json()).execution;
  const url = `${root}/journal-executions/${prepared.contract.execution_id}`;
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  const offline = history.getByRole("region", { name: "오프라인 합성 실행", exact: true });
  await expect(history.getByText(/최근 저장 상태: 입력 준비 기록/)).toBeVisible();
  await expect(offline.getByRole("button", { name: "승인한 오프라인 합성 실행", exact: true })).toBeDisabled();
  // Explicit approval through keyboard activation does not start an attempt.
  const approve = offline.getByRole("button", { name: "오프라인 합성 실행 승인 기록", exact: true });
  await approve.focus(); await page.keyboard.press("Enter");
  await expect(offline.getByText(/승인만 기록했습니다/)).toBeVisible();
  expect(await (await page.request.get(url)).json()).toEqual(prepared);
  const authority = (await (await page.request.get(`${url}/offline`)).json()).authorities[0];
  // A server restart reopens the approval without dispatching it automatically.
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${job}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(offline.getByText(authority.record.grant.authorization_id, { exact: true })).toBeVisible();
  expect(await (await page.request.get(url)).json()).toEqual(prepared);
  await offline.getByRole("heading").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "offline-approval-wide.png") });
  // Lose only the response after the real server accepted the dispatch. The
  // exact replay must retrieve the one attempt, never generate another.
  let acceptedBody: string | null = null;
  await page.route(`**${url}/offline-dispatch`, async route => {
    acceptedBody = route.request().postData();
    await route.fetch(); await route.abort("failed");
  }, { times: 1 });
  await offline.getByRole("button", { name: "승인한 오프라인 합성 실행", exact: true }).click();
  await expect(offline.getByRole("alert")).toContainText("응답을 확인하지 못했습니다");
  const retry = offline.getByRole("button", { name: "같은 오프라인 실행 요청 다시 보내기" });
  const retryRequest = page.waitForRequest(r => r.url().endsWith(`${url}/offline-dispatch`));
  await retry.click();
  expect((await retryRequest).postData()).toBe(acceptedBody);
  await expect(history.getByText(/최근 저장 상태: 응답 저장 완료 · 시도 1/)).toBeVisible();
  const finished = await (await page.request.get(url)).json();
  expect(finished.events.map((event: { state: string }) => event.state)).toEqual(["LEASED", "RUNNING", "SUCCEEDED"]);
  expect((await (await page.request.get(root + "/journal-candidates")).json()).total).toBe(0);
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보" });
  await expect(candidate.getByRole("heading", { name: /오프라인 합성 연결 시험/ })).toBeVisible();
  // Preserving an app-generated candidate opens its review panel directly.
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집" });
  await expect(editor.getByRole("button", { name: "저널로 채택", exact: true })).toBeDisabled();
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("합성 연결 시험입니다. 과학적 뒷받침과 본문 검토는 평가하지 않았습니다.");
  await editor.getByLabel("저널 전체 검토 메모").fill("외부 모델 실행 없이 로컬 저장 흐름만 확인했습니다.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const review = candidate.getByRole("article", { name: "보존된 저널 검토 기록" });
  const [download] = await Promise.all([page.waitForEvent("download"), review.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click()]);
  const exportFile = path.join(artifacts, "offline-reviewed-journal.json");
  await download.saveAs(exportFile);
  const bundle = JSON.parse(fs.readFileSync(exportFile, "utf8"));
  expect(bundle.candidate.validation.semantic_support).toBe("NOT_ASSESSED");
  expect(bundle.candidate.proposal.title).toContain("실제 AI 연구 결과 아님");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await review.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "offline-reviewed-narrow.png") });
  await page.goto(`/app/topics/${topicB}/reports`);
  await expect(page.getByRole("region", { name: "주제 저널 보관함", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: bundle.candidate.proposal.title, exact: true })).toHaveCount(0);
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/reports`);
  const library = page.getByRole("region", { name: "주제 저널 보관함", exact: true });
  await library.getByRole("button", { name: "보존 채택본·검토 열기" }).click();
  await expect(library.getByText(bundle.candidate.proposal.title, { exact: true }).first()).toBeVisible();
  expect(await (await page.request.get(url)).json()).toEqual(finished);
  const readback = await (await page.request.get(`${root}/journal-candidates/${bundle.candidate.candidate_id}/reviews/${bundle.review.review_id}/export?format=json`)).json();
  expect(JSON.parse(readback.content)).toEqual(bundle);
  // The deliberately dropped response produces one Chromium network console
  // message; all other console/page/off-origin errors remain failures.
  const expectedNetwork = ledger.consoleErrors.filter(value => value.includes("net::ERR_FAILED"));
  expect(expectedNetwork).toHaveLength(1);
  ledger.handledExpectedErrors.push(...expectedNetwork);
  ledger.consoleErrors = ledger.consoleErrors.filter(value => !value.includes("net::ERR_FAILED"));
  await assertHygiene(ledger);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { approvedWithoutAttempt: true, lostResponseSingleAttempt: true, humanAccepted: true, exportRestartEqual: true, syntheticOnly: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
