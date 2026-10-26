import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test.use({ anthropicNativeFixture: true });

test("native paper flow binds stored key and approval with intercepted HTTP, recovery and restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const fixtureEnv = { ...process.env, XRML_DATA_DIR: backend.dataRoot, TEMP: backend.tempRoot, TMP: backend.tempRoot, TMPDIR: backend.tempRoot };
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
  const anthropic = panel.getByRole("region", { name: "서버 연결 Anthropic Native 검토", exact: true });
  const selected = panel.getByRole("region", { name: "선택한 HTTP 실행 기록", exact: true });
  expect(ledger.mutationRequests).toHaveLength(1);
  const inspect = anthropic.getByRole("button", { name: "Anthropic Native 전송 조건 확인", exact: true });
  await expect(inspect).toBeEnabled(); await inspect.focus(); await inspect.press("Enter");
  const prepare = anthropic.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비", exact: true });
  await expect(prepare).toBeEnabled();
  expect(ledger.mutationRequests).toHaveLength(1);
  const preparing = page.waitForResponse(r => new URL(r.url()).pathname === base + "/anthropic-native-requests" && r.request().method() === "POST");
  await prepare.click();
  const ready = await (await preparing).json(), identity = ready.contract.execution_id;
  const url = `${base}/${identity}`;
  expect(ready.current.state).toBe("READY"); expect(ready.authority).toBeNull();
  expect(ready.contract.transport.credential_binding).toEqual({ connector_id: "anthropic_api", revision: 1, storage: "LOCAL_DPAPI" });
  const approvePreview = anthropic.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인", exact: true });
  const approval = anthropic.getByRole("button", { name: "확인한 Anthropic Native 승인 등록", exact: true });
  const noOffer = page.waitForResponse(r => new URL(r.url()).pathname === url + "/anthropic-native-approval");
  await approvePreview.click();
  expect((await (await noOffer).json()).approval_allowed).toBe(false);
  expect(fs.existsSync(path.join(backend.dataRoot, "native-sends.jsonl"))).toBe(false);
  function seedOffer(execution: string) {
    const seeded = spawnSync(backend.pythonExe, ["-c", "import json,sys; from tests.helpers.anthropic_native_app_fixture import seed_offer; print(json.dumps(seed_offer(*sys.argv[1:])))",
      topicA, fixture.item.paperVersionId, preparation.preparation_id, execution], { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 15000 });
    expect(seeded.status, seeded.stderr).toBe(0);
    expect(JSON.parse(seeded.stdout)).toMatchObject({ fixture_only: true, live_calls: 0, registered: false });
  }
  seedOffer(identity);
  await approvePreview.click(); await expect(approval).toBeEnabled(); await approval.focus(); await approval.press("Enter");
  await expect(selected.getByText(/마지막 저장 상태: 승인 기록됨/)).toBeVisible();
  const approved = await (await page.request.get(url)).json();
  expect(approved.events.map((e: { state: string }) => e.state)).toEqual(["READY", "APPROVED"]);
  const status = anthropic.getByRole("button", { name: "선택한 Native 요청의 실행 조건 확인", exact: true });
  const execute = anthropic.getByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행", exact: true });
  await status.click(); await expect(execute).toBeEnabled();
  let lost = false;
  const sent: Array<Record<string, unknown>> = [];
  await page.route(`**${url}/anthropic-native-dispatch`, async route => {
    sent.push(route.request().postDataJSON());
    if (!lost) { lost = true; expect((await route.fetch()).status()).toBe(200); await route.abort("failed"); }
    else await route.continue();
  });
  await execute.click();
  const retry = anthropic.getByRole("button", { name: "같은 Anthropic Native 실행 요청 다시 확인", exact: true });
  await expect(anthropic.getByRole("alert")).toBeVisible(); await expect(retry).toBeEnabled();
  await retry.focus(); await retry.press("Enter");
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  expect(sent).toHaveLength(2); expect(sent[0]).toEqual(sent[1]);
  const saved = await (await page.request.get(url)).json();
  expect(saved.events.map((e: { state: string }) => e.state)).toEqual(["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]);
  expect(saved.contract.transport).toMatchObject({ schema_version: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2", execution_surface: "NATIVE_HTTP" });
  const receiptRoot = root.split("/review-preparations")[0] + "/review-inputs";
  const receipt = await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json();
  // Native product provenance is exercised by a test-only intercepted sender;
  // this disposable record is never live-model qualification or user data.
  expect(receipt.input_origin).toBe("PROVIDER_RESPONSE");
  expect(receipt.packet).toEqual(saved.contract.prepared.packet);
  expect(receipt.packet.evidence_spans[0].text).toBe(fixture.expected);
  expect((await (await page.request.get(receiptRoot)).json()).items).toHaveLength(1);
  await selected.getByRole("button", { name: "저장된 HTTP 입력·결과 열기", exact: true }).click();
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })).toBeVisible();
  await anthropic.getByRole("button", { name: "Anthropic Native 승인 철회", exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(url)).json()).authority.validity).toBe("REVOKED");
  const revoked = await (await page.request.get(url)).json(); expect(revoked.current).toEqual(saved.current);
  await inspect.click(); await expect(prepare).toBeEnabled();
  const preparingSecond = page.waitForResponse(r => new URL(r.url()).pathname === base + "/anthropic-native-requests" && r.request().method() === "POST");
  await prepare.click(); const second = await (await preparingSecond).json();
  seedOffer(second.contract.execution_id);
  await approvePreview.click(); await expect(approval).toBeEnabled(); await approval.click();
  await expect(selected.getByText(/마지막 저장 상태: 승인 기록됨/)).toBeVisible();
  const credential = "/settings/model-connections/anthropic_api/credential";
  const removed = await page.request.delete(credential, { headers: { origin: "http://127.0.0.1:8002", "x-xrml-credential-action": "1" }, data: { expected_revision: 1 } });
  expect(removed.status()).toBe(200); expect(await removed.json()).toMatchObject({ stored: false, revision: 2 });
  const secondUrl = `${base}/${second.contract.execution_id}`;
  const changed = page.waitForResponse(r => new URL(r.url()).pathname === secondUrl + "/anthropic-native-status");
  await status.click(); expect((await (await changed).json()).enabled).toBe(false);
  await expect(execute).toBeDisabled();
  await selected.getByRole("button", { name: "이 HTTP 요청 취소", exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 취소됨/)).toBeVisible();
  const cancelled = await (await page.request.get(secondUrl)).json();
  expect(cancelled.current.receipt_id).toBeNull();
  await panel.getByRole("button", { name: `HTTP 실행 기록 열기 · ${identity}`, exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  await selected.getByRole("heading").evaluate(el => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, "anthropic-native-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await anthropic.getByRole("heading").evaluate(el => el.scrollIntoView({ block: "start" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "anthropic-native-narrow.png") });
  const sends = () => fs.readFileSync(path.join(backend.dataRoot, "native-sends.jsonl"), "utf8").trim().split("\n").map(line => JSON.parse(line));
  expect(sends()).toEqual([{ kind: "MOCK_HTTP_ONLY", live_calls: 0 }]);
  fs.unlinkSync(path.join(backend.dataRoot, "native-approval-offers.json"));
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${preparation.preparation_id}`, exact: true }).click();
  await disclosure.click();
  await panel.getByRole("button", { name: `HTTP 실행 기록 열기 · ${identity}`, exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  await selected.getByRole("button", { name: "저장된 HTTP 입력·결과 열기", exact: true }).click();
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })).toBeVisible();
  expect(await (await page.request.get(url)).json()).toEqual(revoked);
  expect(await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json()).toEqual(receipt);
  expect(await (await page.request.get(secondUrl)).json()).toEqual(cancelled);
  const replay = await page.request.post(url + "/anthropic-native-dispatch", { headers: { origin: "http://127.0.0.1:8002", "x-xrml-paper-anthropic-action": "1" }, data: sent[0] });
  expect(replay.status()).toBe(200); expect((await replay.json()).current).toEqual(saved.current);
  expect(sends()).toEqual([{ kind: "MOCK_HTTP_ONLY", live_calls: 0 }]);
  await page.goto(`/app/topics/${topicB}/papers`); await expect(anthropic).toHaveCount(0);
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  expect(ledger.consoleErrors).toEqual([expect.stringContaining("ERR_FAILED")]);
  ledger.handledExpectedErrors.push(...ledger.consoleErrors.splice(0)); await assertHygiene(ledger);
  fs.writeFileSync(path.join(artifacts, "anthropic-native-observations.json"), JSON.stringify({ qualification: "TEST_ONLY_NATIVE_HTTP_INTERCEPTION", live_calls: 0, ready, approved, saved, revoked, cancelled, receipt, sent, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { fixtureOnly: true, liveCalls: 0, singleAttempt: true, boundStoredKey: true, explicitSourceApproval: true, deletedKeyBlocks: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
