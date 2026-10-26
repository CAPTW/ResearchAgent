import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("preserved OpenAI wire conditions remain readable after restart without reexecution", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const runs = fs.readdirSync(path.join(backend.dataRoot, "runs")).sort();
  await page.goto(`/app/topics/${topicA}/papers`);
  await expect(page).toHaveURL(new RegExp(`/app/topics/${topicA}/papers`));
  await expect(page).not.toHaveTitle("");
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
  const seed = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `import json,sys,httpx,time
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from app.domain.journal_execution import Prepare
from app.repositories import local_synthesis,journal_executions as r,journal_authority as a
from app.services.providers.openai_journal import OpenAIJournalProvider,journal_dispatcher
from app.services.journal_dispatch import StoredJournalAuthority
from tests.test_openai_journal_binding import approval
from tests.test_journal_dispatch import body
from tests.test_openai_response_contract import envelope
topic,job_id=sys.argv[1:]; job=local_synthesis.get(topic,job_id); now=datetime.now(timezone.utc)
execution,_=r.prepare(topic,job_id,Prepare(request_id='wire-fixture',expected_input_digest=job['input_digest'],provider_name='openai',model='offline-model'),'offline-fixture',now=now)
packet=execution['contract']['packet']; scope=(topic,job_id,execution['contract']['execution_id'])
proposal=dict(schema_version='JOURNAL_PROPOSAL_V1',packet_id=packet['packet_id'],title='Synthetic bound response',sections=[dict(kind='synthesis',entries=[dict(kind='interpretation',text='Synthetic offline response.',citation_ids=[packet['references'][0]['citation_id']])])])
provider=OpenAIJournalProvider(execution['contract'],endpoint='https://offline.invalid/v1/responses',timeout_seconds=45,api_key='synthetic-key',transport=httpx.MockTransport(lambda _:httpx.Response(200,json=envelope(json.dumps(proposal)))))
grant=approval(execution,provider); grant=grant.model_copy(update=dict(granted_at=now,expires_at=now+timedelta(minutes=2),proof=replace(grant.proof,accepted_at=now,expires_at=now+timedelta(minutes=5))))
a.register(*scope,grant,provider.binding,issuer='synthetic-issuer',decision_ref='offline-test-only',expected_sequence=0,now=now)
controller=journal_dispatcher(provider,authority=StoredJournalAuthority(grant.authorization_id,clock=lambda:now),clock=lambda:now)
try:
 controller.dispatch(*scope,body(),'owner'); deadline=time.monotonic()+10
 while r.detail(*scope)['current']['state'] in ['READY','LEASED','RUNNING'] and time.monotonic()<deadline: time.sleep(.01)
 assert r.detail(*scope)['current']['state']=='SUCCEEDED'
finally: assert controller.stop()
print(json.dumps(r.detail(*scope)))
`, topicA, job], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 25000 });
  expect(seed.status, seed.stderr).toBe(0);
  const saved = JSON.parse(seed.stdout);
  const url = `${root}/journal-executions/${saved.contract.execution_id}`;
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  await history.getByText("고정된 전송 조건", { exact: true }).click();
  await expect(history.getByText("요청 주소: https://offline.invalid/v1/responses")).toBeVisible();
  await expect(history.getByText(/본문 SHA-256:/)).toContainText(saved.events[0].adapter_manifest.body_sha256);
  await history.getByText("요청 내용·토큰 사용 기록", { exact: true }).click();
  const usage = history.getByRole("region", { name: "시도 1 토큰 기록" });
  await expect(usage.getByText("보고된 입력 토큰").locator("+ dd")).toHaveText("5");
  await expect(usage.getByText("보고된 출력 토큰").locator("+ dd")).toHaveText("4");
  await expect(usage.getByText("보고된 합계 토큰").locator("+ dd")).toHaveText("9");
  await expect(usage.getByText(/보고 도구·모델: openai · reported-offline-model/)).toBeVisible();
  await history.getByText("보존된 어댑터 입력 보기", { exact: true }).click();
  expect(await history.getByLabel("보존된 문헌 입력").textContent()).toBe(saved.contract.provider_request.prompt);
  expect(await history.getByLabel("보존된 지시문").textContent()).toBe(saved.contract.provider_request.instructions);
  expect(JSON.parse((await history.getByLabel("보존된 출력 JSON 규격").textContent())!)).toEqual(saved.contract.provider_request.schema);
  await history.getByLabel("보존된 문헌 입력").focus();
  await expect(history.getByLabel("보존된 문헌 입력")).toBeFocused();
  await expect(history.getByText(/전체 실행 시간이나 비용 상한은 아닙니다/)).toBeVisible();
  await expect(history.getByRole("link", { name: /offline.invalid/ })).toHaveCount(0);
  await expect(page.getByText("주제 데이터를 불러오는 중입니다.", { exact: true })).not.toBeVisible();
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  await history.getByLabel("보존된 문헌 입력").evaluate(el => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, "execution-request-wide.png") });
  await history.getByText("보존된 어댑터 입력 보기", { exact: true }).click();
  await usage.getByRole("heading").evaluate(el => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, "execution-usage-wide.png") });
  await history.getByText("고정된 전송 조건", { exact: true }).evaluate(el => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, "journal-binding-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await history.getByText("고정된 전송 조건", { exact: true }).evaluate(el => el.scrollIntoView({ block: "center" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "journal-binding-narrow.png") });
  await usage.getByRole("heading").evaluate(el => el.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: path.join(artifacts, "execution-usage-narrow.png") });
  await history.getByText("보존된 어댑터 입력 보기", { exact: true }).click();
  await history.getByLabel("보존된 문헌 입력").evaluate(el => el.scrollIntoView({ block: "center" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "execution-request-narrow.png") });
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(url)).json()).toEqual(saved);
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job)}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await history.getByText("고정된 전송 조건", { exact: true }).click();
  await expect(history.getByText(/본문 SHA-256:/)).toContainText(saved.events[0].adapter_manifest.body_sha256);
  await history.getByText("요청 내용·토큰 사용 기록", { exact: true }).click();
  await expect(usage.getByText("보고된 합계 토큰").locator("+ dd")).toHaveText("9");
  await history.getByText("보존된 어댑터 입력 보기", { exact: true }).click();
  expect(await history.getByLabel("보존된 문헌 입력").textContent()).toBe(saved.contract.provider_request.prompt);
  await assertHygiene(ledger);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { wireConditions: true, restartPreserved: true, automaticRuns: 0, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
