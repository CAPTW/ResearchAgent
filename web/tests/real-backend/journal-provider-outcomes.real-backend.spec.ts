import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("provider non-success cannot become a journal candidate and survives restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const runs = fs.readdirSync(path.join(backend.dataRoot, "runs")).sort();
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
  const seed = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `import json,sys,httpx
from app.domain.journal_execution import Prepare
from app.repositories import local_synthesis,journal_executions as r
from app.services.journal_execution import execute
from app.services.providers.openai_provider import OpenAIProvider
from tests.test_openai_response_contract import envelope
from tests.test_journal_execution import NOW
topic,job_id=sys.argv[1:]; job=local_synthesis.get(topic,job_id); ids={}
for state in ['incomplete','failed','cancelled','queued','invalid','completed']:
 model='offline-'+state
 saved,_=r.prepare(topic,job_id,Prepare(request_id='response-'+state,expected_input_digest=job['input_digest'],provider_name='openai',model=model),'offline-fixture',now=NOW)
 identity=saved['contract']['execution_id']; ids[state]=identity; packet=saved['contract']['packet']
 text=json.dumps(dict(schema_version='JOURNAL_PROPOSAL_V1',packet_id=packet['packet_id'],title='Completed synthetic journal',sections=[dict(kind='synthesis',entries=[dict(kind='interpretation',text='This fixture is not a scientific conclusion.',citation_ids=[packet['references'][0]['citation_id']])])]))
 response=envelope(text if state!='invalid' else '{"title":"first","title":"second"}',status='completed' if state=='invalid' else state)
 provider=OpenAIProvider(api_key='synthetic-only',model=model,api_base='https://offline.invalid/v1',transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response)))
 claim=r.claim(topic,job_id,identity,expected_attempt=0,now=NOW)
 execute(claim,provider=provider,model=model,clock=lambda:NOW)
print(json.dumps(ids))
`, topicA, job], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 20000 });
  expect(seed.status, seed.stderr).toBe(0);
  const ids = JSON.parse(seed.stdout) as Record<string, string>;
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await expect(results.getByText(/저장된 실행 기록 6개/)).toBeVisible();
  const record = (state: string) => results.getByRole("listitem").filter({ has: page.getByText(ids[state], { exact: true }) });
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  const saved: Record<string, unknown> = {};
  for (const [state, label] of [["incomplete", "도구 응답이 미완료로 종료됨"], ["failed", "도구가 생성 실패를 보고함"], ["cancelled", "도구가 생성 취소를 보고함"], ["queued", "도구 응답 오류"], ["invalid", "출력 형식·입력·인용 검증 실패"]]) {
    const value = await (await page.request.get(`${root}/journal-executions/${ids[state]}`)).json();
    saved[state] = value;
    expect(value.current.state).toBe(state === "queued" ? "INTERRUPTED" : "FAILED");
    expect(value.current.outcome_unknown).toBe(state === "queued");
    expect(value.current.result).toBeNull();
    await expect(record(state).getByRole("button", { name: "검토 후보로 보존·열기" })).toHaveCount(0);
    const rejected = await page.request.post(`${root}/journal-executions/${ids[state]}/candidate`, { data: { expected_result_digest: "0".repeat(64) } });
    expect(rejected.status()).toBe(409);
    await record(state).getByRole("button", { name: "실행 이력·제어 열기" }).click();
    await expect(history.getByText(label, { exact: true })).toBeVisible();
    if (["incomplete", "failed", "cancelled"].includes(state)) await expect(history.getByText(/비용이 발생하지 않았다는 뜻은 아니며/)).toBeVisible();
  }
  await record("completed").getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const candidate = page.getByRole("article", { name: "생성 결과 저널 후보" });
  await expect(candidate.getByText("실제 외부 모델 실행의 독립적인 증명이 아닙니다. 테스트 어댑터의 응답일 수도 있습니다.", { exact: true })).toBeVisible();
  const candidates = await (await page.request.get(`${root}/journal-candidates`)).json();
  expect(candidates.total).toBe(1);
  await page.goto("about:blank"); await backend.restart();
  for (const [state, value] of Object.entries(saved)) {
    expect(await (await page.request.get(`${root}/journal-executions/${ids[state]}`)).json()).toEqual(value);
  }
  expect(await (await page.request.get(`${root}/journal-candidates`)).json()).toEqual(candidates);
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job)}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await record("incomplete").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText("도구 응답이 미완료로 종료됨", { exact: true })).toBeVisible();
  await expect(history.getByText(/최근 저장 상태: 실패 · 시도 1/)).toBeVisible();
  await expect(page.getByText("주제 데이터를 불러오는 중입니다.", { exact: true })).not.toBeVisible();
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  await history.getByRole("heading").evaluate(el => el.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: path.join(artifacts, "provider-response-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await history.getByRole("heading").evaluate(el => el.scrollIntoView({ block: "start" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "provider-response-narrow.png") });
  await assertHygiene(ledger);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { completionRequired: true, noFailedCandidate: true, restartPreserved: true, automaticRuns: 0, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
