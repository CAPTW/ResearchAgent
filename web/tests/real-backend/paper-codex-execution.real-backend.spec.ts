import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test.use({ paperCodexFixture: true });

test("paper Codex app prepares, dispatches once, opens exact input and restarts", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const consoleErrors: { text: string; url: string }[] = [];
  page.on("console", message => { if (message.type() === "error") consoleErrors.push({ text: message.text(), url: message.location().url }); });
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
  expect((await captured).status()).toBe(200);
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.expected);
  const preparation = await (await captured).json();
  const base = `${root}/${preparation.preparation_id}/transport-executions`;
  await drawer.getByText("검토 실행 조건·이력", { exact: true }).click();
  const panel = drawer.getByRole("region", { name: "검토 실행 기록 확인", exact: true });
  const codex = panel.getByRole("region", { name: "서버 연결 Codex 논문 검토", exact: true });
  await codex.getByRole("button", { name: "Codex 검토 입력·실행 조건 확인", exact: true }).click();
  await expect(codex.getByText(/fixture-paper-model/)).toBeVisible();
  await expect(codex.getByText(/FIXTURE_SYNTHETIC/)).toBeVisible();
  await codex.getByText("Codex 검토에 전달할 고정 본문", { exact: true }).click();
  await expect(codex.getByLabel("Codex 검토 고정 본문", { exact: true })).toHaveText(fixture.expected);
  expect(fs.existsSync(path.join(backend.dataRoot, "fake-paper-codex", "launches.log"))).toBe(false);
  let prepareBody: unknown;
  await page.route(`**${base}/codex-requests`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    prepareBody = route.request().postDataJSON();
    await route.fetch(); await route.abort("failed");
  });
  await codex.getByRole("button", { name: "이 입력으로 Codex 검토 요청 준비", exact: true }).click();
  await expect(codex.getByRole("alert")).toContainText("응답");
  const retryPrepare = codex.getByRole("button", { name: "같은 Codex 검토 준비 요청 다시 보내기", exact: true });
  await expect(retryPrepare).toBeEnabled();
  await page.unroute(`**${base}/codex-requests`);
  await panel.getByRole("button", { name: "검토 실행 이력 새로고침", exact: true }).click();
  const prepareReplay = page.waitForRequest(r => r.url().endsWith(`${base}/codex-requests`) && r.method() === "POST");
  await retryPrepare.click();
  expect((await prepareReplay).postDataJSON()).toEqual(prepareBody);
  await expect(retryPrepare).toHaveCount(0);
  const listing = await (await page.request.get(base)).json();
  expect(listing.items).toHaveLength(1);
  const ready = listing.items[0];
  expect(ready.current.state).toBe("READY");
  expect(ready.authority).toBeNull();
  expect(ready.contract.prepared.packet.evidence_spans[0].text).toBe(fixture.expected);
  expect(ready.contract.transport.schema_version).toBe("CODEX_PREPARED_REVIEW_CLI_V2");
  expect(fs.existsSync(path.join(backend.dataRoot, "fake-paper-codex", "launches.log"))).toBe(false);
  const url = `${base}/${ready.contract.execution_id}`;
  // An out-of-band fixture registers fabricated authority. The product has no issuer endpoint.
  const grant = spawnSync(backend.pythonExe, ["-c", `import json,sys
from pathlib import Path
from app.auth.resource_access import resource_actor_for_user
from app.services import review_execution
from app.repositories import review_transport_history
from app.domain.review_preparation import encoded,sha
from tests.test_review_codex_execution import approve
from tests.test_prepared_review import response
saved=json.loads(sys.argv[1]); value=saved['contract']
scope=review_execution.scope(value['topic_id'],value['paper_version_id'],value['preparation_id'],resource_actor_for_user(None))
# Read exact stored JSON: a JavaScript round trip changes 30.0 to 30.
roundtrip_hash=sha(encoded(value))
saved=review_transport_history.detail(scope,value['execution_id']); value=saved['contract']
approve(dict(scope=scope,url='fixture'),saved)
Path(sys.argv[2]).write_text(json.dumps(response(value['prepared']).payload,ensure_ascii=False),encoding='utf-8')
print(json.dumps(dict(browser_roundtrip_hash=roundtrip_hash,stored_contract_hash=sha(encoded(value)))))
`, JSON.stringify(ready), path.join(backend.dataRoot, "fake-paper-codex", "output.json")], {
    cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 30000,
  });
  expect(grant.status, grant.stderr).toBe(0);
  await panel.getByRole("button", { name: "검토 실행 이력 새로고침", exact: true }).click();
  await expect(panel.getByRole("region", { name: "선택한 CLI 실행 기록", exact: true })
    .getByText(/마지막 저장 상태: 승인 기록됨/)).toBeVisible();
  const inspect = codex.getByRole("button", { name: "선택한 Codex 요청의 서버 허용 확인", exact: true });
  await expect(inspect).toBeEnabled();
  await inspect.focus(); await expect(inspect).toBeFocused(); await inspect.press("Enter");
  const dispatch = codex.getByRole("button", { name: "서버가 허용한 Codex 검토 실행", exact: true });
  await expect(dispatch).toBeEnabled();
  let dispatchBody: unknown;
  await page.route(`**${url}/codex-dispatch`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    dispatchBody = route.request().postDataJSON();
    await route.fetch(); await route.abort("failed");
  });
  await dispatch.click();
  await expect(codex.getByRole("alert")).toContainText("응답");
  const retryDispatch = codex.getByRole("button", { name: "같은 Codex 검토 실행 요청 다시 보내기", exact: true });
  await expect(retryDispatch).toBeEnabled();
  await page.unroute(`**${url}/codex-dispatch`);
  await expect.poll(async () => (await (await page.request.get(url)).json()).current.state).toBe("RESULT_SAVED");
  await panel.getByRole("button", { name: "검토 실행 이력 새로고침", exact: true }).click();
  const dispatchReplay = page.waitForRequest(r => r.url().endsWith(`${url}/codex-dispatch`) && r.method() === "POST");
  await retryDispatch.click();
  expect((await dispatchReplay).postDataJSON()).toEqual(dispatchBody);
  await expect(retryDispatch).toHaveCount(0);
  const saved = await (await page.request.get(url)).json();
  const receiptRoot = root.replace("review-preparations", "review-inputs");
  const receipt = await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json();
  expect(receipt.input_origin).toBe("FIXTURE_SYNTHETIC");
  expect(receipt.packet).toEqual(saved.contract.prepared.packet);
  expect(receipt.validation.prepared_execution.full_document_review).toBe("NOT_ESTABLISHED");
  const selected = panel.getByRole("region", { name: "선택한 CLI 실행 기록", exact: true });
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await selected.getByText(/마지막 저장 상태: 결과 저장됨/).evaluate(element => element.scrollIntoView({ block: "center" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "paper-codex-app-narrow.png") });
  await selected.getByRole("button", { name: "저장된 CLI 입력·결과 열기", exact: true }).click();
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })
    .getByText(receipt.draft.contribution_summary.trim(), { exact: true })).toBeVisible();
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  const launches = () => fs.readFileSync(path.join(backend.dataRoot, "fake-paper-codex", "launches.log"), "utf8").trim().split(/\r?\n/);
  expect(launches()).toEqual(["launch"]);
  await page.goto("about:blank"); await backend.restart();
  expect((await (await page.request.get(url)).json()).events).toEqual(saved.events);
  expect((await (await page.request.post(`${url}/codex-dispatch`, { data: dispatchBody })).json()).events).toEqual(saved.events);
  expect(await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json()).toEqual(receipt);
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${preparation.preparation_id}`, exact: true }).click();
  await drawer.getByText("검토 실행 조건·이력", { exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  expect(launches()).toEqual(["launch"]);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(codex).toHaveCount(0);
  expect(consoleErrors).toEqual([
    { text: expect.stringContaining("ERR_FAILED"), url: `http://127.0.0.1:8002${base}/codex-requests` },
    { text: expect.stringContaining("ERR_FAILED"), url: `http://127.0.0.1:8002${url}/codex-dispatch` },
  ]);
  expect(ledger.consoleErrors).toEqual(consoleErrors.map(error => error.text));
  ledger.handledExpectedErrors.push(...ledger.consoleErrors.splice(0));
  await assertHygiene(ledger);
  fs.writeFileSync(path.join(artifacts, "paper-codex-app-observations.json"), JSON.stringify({ ready, saved, receipt, prepareBody, dispatchBody, authorityFixture: JSON.parse(grant.stdout), ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { syntheticOnly: true,
    actualAppPreparationAndDispatch: true, oneProcess: true, exactRange: true, lostResponseDeduplicated: true,
    restarted: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: 0, pageErrors: 0, offOriginAttempts: 0 } });
});
