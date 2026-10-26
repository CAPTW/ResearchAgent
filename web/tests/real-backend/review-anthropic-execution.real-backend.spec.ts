import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("prepared paper range uses explicit Anthropic synthetic approval, durable evidence and restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const fixtureEnv = { ...process.env, XRML_DATA_DIR: backend.dataRoot };
  const seed = spawnSync(backend.pythonExe, ["-c", `import json,sys
from app.services import paper_bank,paper_review
from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from tests.test_ai_review_provider import _review
topic=sys.argv[1]; item=paper_bank.list_topic_papers(topic,page_size=100)['items'][0]
occ=item['compatibility']['occurrences'][0]
source='a'*26000+'e\\u0301 한글🙂 later methods and results '*300
artifact=build_run_paths(occ['runId']).raw_dir/(safe_filename(occ['paperId'])+'.txt')
artifact.write_bytes(source.encode('utf-8'))
review=_review(1,topic_id=topic,run_id=occ['runId'],paper_id=occ['paperId'],title=item['title'])
review.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
paper_reviews.upsert_review(review)
print(json.dumps(dict(item=item,run=occ['runId'],paper=occ['paperId'],expected=source[26000:26010]),ensure_ascii=False))
`, topicA], { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const fixture = JSON.parse(seed.stdout);
  const root = `/api/topics/${topicA}/paper-bank/${fixture.item.paperVersionId}/review-preparations`;
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  const sources = drawer.getByLabel("저장 본문 출처");
  await expect(sources).toBeVisible();
  const label = (await sources.locator("option").allTextContents()).find(value => value.includes(fixture.run) && value.includes(fixture.paper));
  expect(label).toBeTruthy(); await sources.selectOption({ label: label! });
  await drawer.getByLabel("시작 위치", { exact: true }).fill("26000");
  await drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true }).fill("26010");
  const captured = page.waitForResponse(response => new URL(response.url()).pathname === root && response.request().method() === "POST");
  await drawer.getByRole("button", { name: "선택 구간 고정", exact: true }).click();
  const response = await captured;
  expect(response.status()).toBe(200);
  const preparation = await response.json();
  const base = `${root}/${preparation.preparation_id}/transport-executions`;
  const disclosure = drawer.getByText("검토 실행 조건·이력", { exact: true });
  await disclosure.focus(); await disclosure.press("Enter");
  const panel = drawer.getByRole("region", { name: "검토 실행 기록 확인", exact: true });
  const anthropic = panel.getByRole("region", { name: "Anthropic 구조화 합성 검토", exact: true });
  const selected = panel.getByRole("region", { name: "선택한 HTTP 실행 기록", exact: true });
  expect(ledger.mutationRequests).toHaveLength(1);
  const inspect = anthropic.getByRole("button", { name: "Anthropic 구조화 요청 확인", exact: true });
  await expect(inspect).toBeEnabled();
  await inspect.focus(); await inspect.press("Enter");
  const makeRequest = anthropic.getByRole("button", { name: "이 구간으로 합성 요청 준비", exact: true });
  await expect(makeRequest).toBeEnabled();
  await expect(anthropic.getByText(/선택 범위: 26000–26010 · 10자/)).toBeVisible();
  await anthropic.getByText("Anthropic 검토에 사용할 고정 본문", { exact: true }).click();
  await expect(anthropic.getByLabel("Anthropic 합성 검토 고정 본문")).toHaveText(fixture.expected);
  expect(ledger.mutationRequests).toHaveLength(1); // Preview does not prepare, approve or execute.
  const preparing = page.waitForResponse(value => new URL(value.url()).pathname === base + "/anthropic-requests" && value.request().method() === "POST");
  await makeRequest.click();
  const ready = await (await preparing).json();
  expect(ready.current.state).toBe("READY"); expect(ready.authority).toBeNull();
  const url = `${base}/${ready.contract.execution_id}`;
  await expect(selected.getByText(/마지막 저장 상태: 요청 준비됨/)).toBeVisible();
  await anthropic.getByRole("button", { name: "합성 시험 승인", exact: true }).click();
  const run = anthropic.getByRole("button", { name: "합성 응답 시험 실행", exact: true });
  await expect(run).toBeEnabled();
  const approved = await (await page.request.get(url)).json();
  expect(approved.current.state).toBe("APPROVED");
  expect(approved.events).toHaveLength(2);
  let lost = false;
  const sent: unknown[] = [];
  await page.route(`**${url}/anthropic-offline-dispatch`, async route => {
    sent.push(route.request().postDataJSON());
    if (!lost) { lost = true; expect((await route.fetch()).status()).toBe(200); await route.abort("failed"); }
    else await route.continue();
  });
  await run.focus(); await run.press("Enter");
  const retry = anthropic.getByRole("button", { name: "같은 합성 실행 요청 다시 보내기", exact: true });
  await expect(retry).toBeVisible();
  await expect(anthropic.getByRole("alert")).toContainText("응답을 확인하지 못했습니다");
  await expect(retry).toBeEnabled();
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26000");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue("26010");
  await retry.focus(); await retry.press("Enter");
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  expect(sent).toHaveLength(2); expect(sent[0]).toEqual(sent[1]);
  const saved = await (await page.request.get(url)).json();
  expect(saved.events.map((event: { state: string }) => event.state)).toEqual(["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]);
  expect(saved.contract.transport).toMatchObject({ schema_version: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2", execution_surface: "MOCK_TRANSPORT", shared_schema_context: "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM" });
  const receiptRoot = root.split("/review-preparations")[0] + "/review-inputs";
  const receipt = await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json();
  expect(receipt.input_origin).toBe("FIXTURE_SYNTHETIC");
  expect(receipt.packet).toEqual(saved.contract.prepared.packet);
  expect(receipt.packet.evidence_spans[0].text).toBe(fixture.expected);
  expect((await (await page.request.get(receiptRoot)).json()).items).toHaveLength(1);
  await selected.getByRole("button", { name: "저장된 HTTP 입력·결과 열기", exact: true }).click();
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })).toBeVisible();
  await drawer.getByText("보존된 출처의 범위 요약", { exact: true }).click();
  const coverage = drawer.getByRole("region", { name: "보존된 출처 범위", exact: true });
  await expect(coverage.getByRole("region", { name: "합성 입력·결과", exact: true })).toContainText("입력·결과 기록 1건 · 중복 제외 10자");
  await expect(coverage.getByRole("region", { name: "Provider 응답 기록", exact: true })).toContainText("입력·결과 기록 0건 · 중복 제외 0자");
  await expect(coverage).toContainText("전체 논문 검토 미확인");
  await anthropic.getByRole("button", { name: "합성 시험 승인 철회", exact: true }).click();
  await expect(anthropic.getByText("합성 시험 승인 철회를 확인했습니다.", { exact: false })).toBeVisible();
  const revoked = await (await page.request.get(url)).json();
  expect(revoked.current).toEqual(saved.current); expect(revoked.authority.validity).toBe("REVOKED");
  const preparingSecond = page.waitForResponse(value => new URL(value.url()).pathname === base + "/anthropic-requests" && value.request().method() === "POST");
  await makeRequest.click();
  const second = await (await preparingSecond).json();
  expect(second.contract.execution_id).not.toBe(ready.contract.execution_id);
  await selected.getByRole("button", { name: "이 HTTP 요청 취소", exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 취소됨/)).toBeVisible();
  const cancelled = await (await page.request.get(`${base}/${second.contract.execution_id}`)).json();
  expect(cancelled.events.map((event: { state: string }) => event.state)).toEqual(["READY", "CANCELLED"]);
  expect(cancelled.authority).toBeNull(); expect(cancelled.current.receipt_id).toBeNull();
  await panel.getByRole("button", { name: `HTTP 실행 기록 열기 · ${ready.contract.execution_id}`, exact: true }).click();
  await expect(anthropic.getByText("합성 요청 준비를 저장했습니다. 승인과 실행은 아직 수행하지 않았습니다.", { exact: true })).toHaveCount(0);
  await selected.getByText(/마지막 저장 상태: 결과 저장됨/).evaluate(element => element.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, "anthropic-prepared-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await anthropic.getByText(/선택 범위: 26000–26010/).evaluate(element => element.scrollIntoView({ block: "center" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "anthropic-prepared-narrow.png") });
  const remove = spawnSync(backend.pythonExe, ["-c", `import sys
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
root=build_run_paths(sys.argv[1]).raw_dir.resolve()
target=(root/(safe_filename(sys.argv[2])+'.txt')).resolve()
assert target.is_relative_to(root)
target.unlink()
`, fixture.run, fixture.paper], { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(remove.status, remove.stderr).toBe(0);
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${preparation.preparation_id}`, exact: true }).click();
  await disclosure.click();
  await panel.getByRole("button", { name: `HTTP 실행 기록 열기 · ${ready.contract.execution_id}`, exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  await selected.getByRole("button", { name: "저장된 HTTP 입력·결과 열기", exact: true }).click();
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })).toBeVisible();
  expect(await (await page.request.get(url)).json()).toEqual(revoked);
  expect(await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json()).toEqual(receipt);
  expect(await (await page.request.get(`${base}/${second.contract.execution_id}`)).json()).toEqual(cancelled);
  expect((await (await page.request.get(receiptRoot)).json()).items).toHaveLength(1);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(anthropic).toHaveCount(0);
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  expect(ledger.consoleErrors).toEqual([expect.stringContaining("ERR_FAILED")]);
  ledger.handledExpectedErrors.push(...ledger.consoleErrors.splice(0)); await assertHygiene(ledger);
  expect(ledger.mutationRequests).toHaveLength(8);
  expect(ledger.mutationRequests.every(value => value.includes("/review-preparations"))).toBe(true);
  fs.writeFileSync(path.join(artifacts, "anthropic-prepared-observations.json"), JSON.stringify({ preparation, ready, approved, saved, revoked, cancelled, receipt, sent, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { explicitPreviewAndApproval: true, syntheticOnly: true, lostResponseDeduplicated: true, syntheticCoverageOnly: true, terminalRevokePreservesResult: true, missingSourceReadback: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
