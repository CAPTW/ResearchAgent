import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, topicA, topicB, installLedger, assertHygiene, remainingBudget, withinDeadline } from "./m2-paper-bank.real-backend.helpers";

test("synthetic Anthropic API Journal keeps frozen conditions through review export and restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const expect = withinDeadline(remainingBudget(info));
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const runs = fs.readdirSync(path.join(backend.dataRoot, "runs")).sort();
  await page.goto(`/app/topics/${topicA}/settings`);
  const settings = page.getByRole("region", { name: "모델 연결 설정", exact: true });
  await expect(settings.getByRole("combobox", { name: "기본 모델 연결" })).toHaveValue("codex_subscription");
  await settings.getByRole("combobox", { name: "기본 모델 연결" }).selectOption("anthropic_api");
  const model = settings.getByRole("textbox", { name: "Claude · Anthropic API 키 모델", exact: true });
  await model.fill("offline-requested-model"); await model.press("Enter");
  await expect(settings.getByText(/모델 연결 설정을 저장했습니다/)).toBeVisible();
  await page.goto(`/app/topics/${topicA}/papers`);
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
  const preparationResults = panel.getByRole("region", { name: "저장된 저널 생성 결과" });
  await preparationResults.getByText("새 생성 요청 준비", { exact: true }).click();
  const form = preparationResults.getByRole("form", { name: "생성 요청 준비" });
  await expect(form.getByRole("radio", { name: "저장된 모델 연결 사용" })).toBeChecked();
  await expect(form.getByText("offline-requested-model", { exact: true })).toBeVisible();
  const preparedResponse = page.waitForResponse(r => r.url().endsWith("/journal-executions/selected-connection") && r.request().method() === "POST");
  await form.getByRole("button", { name: "로컬 생성 요청 준비" }).click();
  const response = await preparedResponse;
  expect(response.status()).toBe(201);
  const prepared = (await response.json()).execution;
  expect(prepared.contract.specification).toMatchObject({ provider_name: "anthropic", model: "offline-requested-model" });
  expect(prepared.current).toMatchObject({ state: "READY", attempt: 0 });
  expect(prepared.events).toEqual([]);
  // Test-owned authority and MockTransport only. No live app issuer or network client.
  const seed = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `import json,sys,time,httpx
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from app.repositories import journal_executions as r,journal_authority as a
from app.services.providers.anthropic_journal import AnthropicJournalProvider,journal_dispatcher
from app.services.journal_dispatch import StoredJournalAuthority
from tests.test_openai_journal_binding import approval
from tests.test_journal_dispatch import body
def envelope(raw):
 return dict(id='synthetic-message',type='message',role='assistant',model='reported-offline-model',stop_reason='end_turn',content=[dict(type='text',text=raw)],usage=dict(input_tokens=3,output_tokens=6,cache_creation_input_tokens=4,cache_read_input_tokens=8))
topic,job_id,execution_id=sys.argv[1:]; now=datetime.now(timezone.utc)
execution=r.detail(topic,job_id,execution_id); assert execution['current']['state']=='READY' and execution['current']['attempt']==0
packet=execution['contract']['packet']; scope=(topic,job_id,execution['contract']['execution_id']); calls=[]
proposal=dict(schema_version='JOURNAL_PROPOSAL_V1',packet_id=packet['packet_id'],title='Synthetic Anthropic Journal',sections=[dict(kind='synthesis',entries=[dict(kind='interpretation',text='Synthetic response e\u0301 한글🙂. Not a scientific assessment.',citation_ids=[packet['references'][0]['citation_id']])])])
def handle(request):
 calls.append(request); assert str(request.url)=='https://api.anthropic.com/v1/messages'
 assert request.headers['anthropic-version']=='2023-06-01' and request.headers['x-api-key']=='synthetic-key'
 assert 'authorization' not in request.headers
 return httpx.Response(200,json=envelope(json.dumps(proposal,ensure_ascii=False)))
provider=AnthropicJournalProvider(execution['contract'],timeout_seconds=45,api_key='synthetic-key',transport=httpx.MockTransport(handle))
grant=approval(execution,provider); grant=grant.model_copy(update=dict(granted_at=now,expires_at=now+timedelta(minutes=2),proof=replace(grant.proof,accepted_at=now,expires_at=now+timedelta(minutes=5))))
a.register(*scope,grant,provider.binding,issuer='synthetic-anthropic-fixture',decision_ref='MockTransport-only',expected_sequence=0,now=now)
controller=journal_dispatcher(provider,authority=StoredJournalAuthority(grant.authorization_id,clock=lambda:now),clock=lambda:now)
try:
 controller.dispatch(*scope,body(),'owner'); deadline=time.monotonic()+10
 while r.detail(*scope)['current']['state'] in ['READY','LEASED','RUNNING'] and time.monotonic()<deadline: time.sleep(.01)
 saved=r.detail(*scope); assert saved['current']['state']=='SUCCEEDED',saved
 assert controller.dispatch(*scope,body(),'owner')==saved and len(calls)==1
finally: assert controller.stop()
print(json.dumps(saved))
`, topicA, job, prepared.contract.execution_id], {
    cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot },
    encoding: "utf8", windowsHide: true, timeout: 25000,
  });
  expect(seed.status, seed.stderr).toBe(0);
  const saved = JSON.parse(seed.stdout);
  const url = `${root}/journal-executions/${saved.contract.execution_id}`;
  const manifest = saved.events[0].adapter_manifest;
  expect(saved.contract).toEqual(prepared.contract);
  await page.reload();
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await expect(results.getByText(/어댑터 보고 모델: anthropic · reported-offline-model/)).toBeVisible();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  await history.getByText("고정된 전송 조건", { exact: true }).focus();
  await history.getByText("고정된 전송 조건", { exact: true }).press("Enter");
  await expect(history.getByText("Anthropic Messages API 전송 계획")).toBeVisible();
  await expect(history.getByText(/본문 SHA-256:/)).toContainText(manifest.body_sha256);
  await expect(history.getByText(/Claude 구독 로그인과 별도인 API 키 경로/)).toBeVisible();
  await expect(history.getByText(/전체 사용량·비용 상한을 보장하지 않습니다/)).toBeVisible();
  await expect(history.getByText("요청 주소: https://api.anthropic.com/v1/messages")).toBeVisible();
  await history.getByText("요청 내용·토큰 사용 기록", { exact: true }).click();
  const usage = history.getByRole("region", { name: "시도 1 토큰 기록" });
  await expect(usage.getByText("보고된 합계 토큰").locator("+ dd")).toHaveText("미보고");
  await expect(usage.getByText("보고된 캐시 생성 입력 토큰").locator("+ dd")).toHaveText("4");
  await expect(usage.getByText("보고된 캐시 읽기 입력 토큰").locator("+ dd")).toHaveText("8");
  await history.getByText("보존된 어댑터 입력 보기", { exact: true }).click();
  const input = history.getByLabel("보존된 문헌 입력");
  expect(await input.textContent()).toBe(saved.contract.provider_request.prompt);
  await input.focus(); await expect(input).toBeFocused();
  await history.getByText("고정된 전송 조건", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "anthropic-binding-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await history.getByText("고정된 전송 조건", { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "anthropic-binding-narrow.png") });
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보" });
  await expect(candidate.getByText("어댑터가 보고한 Provider·모델: anthropic · reported-offline-model", { exact: true })).toBeVisible();
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집" });
  await expect(editor.getByRole("button", { name: "저널로 채택", exact: true })).toBeDisabled();
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("MockTransport 연결 시험. 실제 모델·과학적 뒷받침은 평가하지 않음.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const review = candidate.getByRole("article", { name: "보존된 저널 검토 기록" });
  const [download] = await Promise.all([page.waitForEvent("download"), review.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click()]);
  const exportFile = path.join(artifacts, "anthropic-fixture-reviewed-journal.json");
  await download.saveAs(exportFile);
  const bundle = JSON.parse(fs.readFileSync(exportFile, "utf8"));
  expect(bundle.candidate.validation.semantic_support).toBe("NOT_ASSESSED");
  expect(bundle.candidate.raw_json).toBe(saved.current.result.raw_json);
  expect(bundle.candidate.execution.schema_version).toBe("JOURNAL_EXECUTION_RECEIPT_V2");
  expect(bundle.candidate.execution.adapter_manifest).toEqual(manifest);
  expect(bundle.candidate.markdown).toContain("전체 HTTP 응답은 보존되지 않습니다");
  expect(bundle.candidate.execution.provider_reported.usage).toEqual({ input_tokens: 3, output_tokens: 6, cache_creation_input_tokens: 4, cache_read_input_tokens: 8 });
  await page.goto(`/app/topics/${topicB}/reports`);
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(url)).json()).toEqual(saved);
  const readback = await (await page.request.get(`${root}/journal-candidates/${bundle.candidate.candidate_id}/reviews/${bundle.review.review_id}/export?format=json`)).json();
  expect(JSON.parse(readback.content)).toEqual(bundle);
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job)}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await history.getByText("고정된 전송 조건", { exact: true }).click();
  await expect(history.getByText(/본문 SHA-256:/)).toContainText(manifest.body_sha256);
  await assertHygiene(ledger);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { mockTransportOnly: true, anthropicManifestReadback: true, humanAcceptance: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
