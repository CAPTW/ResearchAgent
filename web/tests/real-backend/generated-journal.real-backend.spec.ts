import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("stored offline adapter result becomes a distinct reviewable journal with preserved provenance", async ({ page, backend, context }, info) => {
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
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await expect(results.getByText(/저장된 실행 기록이 없습니다/)).toBeVisible();
  const seed = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `import json,sys
from app.domain.journal_execution import Prepare
from app.repositories import local_synthesis,journal_executions as r
from app.services.journal_execution import execute
from app.services.providers.base import ProviderResult
topic,job_id=sys.argv[1:]
job=local_synthesis.get(topic,job_id)
body=Prepare(request_id='browser-offline-result',expected_input_digest=job['input_digest'],provider_name='synthetic',model='requested-test-model')
saved,_=r.prepare(topic,job_id,body,'offline-fixture')
packet=saved['contract']['packet']
proposal=dict(schema_version='JOURNAL_PROPOSAL_V1',packet_id=packet['packet_id'],title='테스트로 저장한 생성 저널',sections=[dict(kind='synthesis',entries=[dict(kind='interpretation',text='이 합성 시험 문장은 사람의 근거 검토가 필요합니다. e\\u0301 · \\u1100\\u1161',citation_ids=[packet['references'][0]['citation_id']])])])
class Offline:
 provider_name='synthetic'
 def is_configured(self):return True
 def generate_json(self,request):return ProviderResult(provider_name='synthetic',model='reported-test-model',response_id='synthetic-response',payload=proposal,raw_text=json.dumps(proposal,ensure_ascii=False))
execute(r.claim(topic,job_id,saved['contract']['execution_id'],expected_attempt=0),provider=Offline(),model='requested-test-model')
cancelled,_=r.prepare(topic,job_id,body.model_copy(update=dict(request_id='browser-cancelled')),'offline-fixture')
r.cancel(topic,job_id,cancelled['contract']['execution_id'],expected_attempt=0)
` , topicA, job], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 20000 });
  expect(seed.status, seed.stderr).toBe(0);
  await results.getByRole("button", { name: "저장된 생성 결과 새로고침" }).click();
  await expect(results.getByText(/저장된 실행 기록 2개/)).toBeVisible();
  await expect(results.getByText(/성공한 응답이 저장되지 않아/)).toBeVisible();
  expect(await results.getByRole("button", { name: "검토 후보로 보존·열기" }).count()).toBe(1);
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const reader = results.getByRole("article", { name: "생성 결과 저널 후보" });
  await expect(reader.getByText(/어댑터가 보고한 Provider·모델: synthetic · reported-test-model/)).toBeVisible();
  await expect(reader.getByText(/테스트 어댑터의 응답일 수도 있습니다/)).toBeVisible();
  expect(await reader.getByText(/가져온 사용자:/).count()).toBe(0);
  const candidateId = (await (await page.request.get(root + "/journal-candidates")).json()).items[0].candidate_id;
  const candidateUrl = root + "/journal-candidates/" + candidateId;
  const original = await (await page.request.get(candidateUrl)).json();
  expect(original.origin).toBe("RECORDED_ADAPTER_RESULT");
  expect(original.proposal.sections[0].entries[0].text).toBe("이 합성 시험 문장은 사람의 근거 검토가 필요합니다. e\u0301 · \u1100\u1161");
  expect(original.raw_json).toContain("e\u0301");
  expect(original.validation.independent_live_proof).toBe("NOT_VERIFIED");
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  expect((await (await page.request.get(root + "/journal-candidates")).json()).total).toBe(1);
  await reader.getByText("보존된 실행 출처", { exact: true }).click();
  await expect(reader.getByText(/요청 Provider·모델: synthetic · requested-test-model/)).toBeVisible();
  const editor = reader.getByRole("region", { name: "저널 근거 판단 편집" });
  await expect(editor.getByLabel("문장 1 근거 판단")).toHaveValue("not_assessed");
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("보존 초록에 한정한 테스트 검토입니다.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(reader.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const reviewId = (await (await page.request.get(candidateUrl + "/reviews")).json()).latest.review_id;
  const review = await (await page.request.get(candidateUrl + "/reviews/" + reviewId)).json();
  expect(review.report.content).toContain(original.execution.contract.execution_id);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const [download] = await Promise.all([page.waitForEvent("download"), reader.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click()]);
  const bundleFile = path.join(artifacts, "generated-journal-accepted.json");
  await download.saveAs(bundleFile);
  expect(JSON.parse(fs.readFileSync(bundleFile, "utf8")).candidate).toEqual(original);
  const imported = (await (await page.request.post(root + "/journal-candidates", { data: { raw_json: original.raw_json } })).json()).candidate;
  expect(imported.candidate_id).not.toBe(candidateId);
  expect((await (await page.request.get(root + "/journal-candidates/" + imported.candidate_id + "/reviews")).json()).latest).toBeNull();
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(candidateUrl)).json()).toEqual(original);
  expect(await (await page.request.get(candidateUrl + "/reviews/" + reviewId)).json()).toEqual(review);
  await page.goto(`/app/topics/${topicA}/reports`);
  const library = page.getByRole("region", { name: "주제 저널 보관함", exact: true });
  await expect(library.getByText("보존된 저널 2개 · 현재 페이지 2개")).toBeVisible();
  await expect(library.getByText(/파일로 가져온 후보 · 보존 날짜/)).toBeVisible();
  await library.getByRole("listitem").filter({ hasText: "저장된 생성 결과 · 보존 날짜" }).getByRole("button", { name: "보존 채택본·검토 열기" }).click();
  const reopened = library.getByRole("article", { name: "생성 결과 저널 후보" });
  await expect(reopened.getByText(/어댑터가 보고한 Provider·모델: synthetic · reported-test-model/)).toBeVisible();
  await expect(page.getByText("주제 데이터를 불러오는 중입니다.", { exact: true })).not.toBeVisible();
  await reopened.getByText("보존된 실행 출처", { exact: true }).click();
  await reopened.getByRole("heading", { name: original.proposal.title, exact: true }).evaluate(el => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, "generated-journal-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await reopened.getByRole("heading", { name: original.proposal.title, exact: true }).evaluate(el => el.scrollIntoView({ block: "center" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "generated-journal-narrow.png") });
  await assertHygiene(ledger);
  expect(ledger.mutationRequests.every(value => value.includes("/local-synthesis"))).toBe(true);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { syntheticAdapterOnly: true, explicitPreservation: true, distinctImportedIdentity: true, humanReview: true, receiptExport: true, restartPreserved: true, automaticRuns: 0, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
