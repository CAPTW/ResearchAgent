import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("explicit preserved review selection reaches frozen journal input, human export and restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const seed = spawnSync(backend.pythonExe, ["-c", `import json,sys
from pathlib import Path
from app.services import ai_provider,paper_bank,paper_review
from app.repositories import paper_reviews,ai_review_drafts
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from tests.test_ai_review_provider import _review
topic,root=sys.argv[1:]; item=paper_bank.list_topic_papers(topic,page_size=100)['items'][0]
occ=item['compatibility']['occurrences'][0]
artifact=build_run_paths(occ['runId']).raw_dir/(safe_filename(occ['paperId'])+'.txt')
artifact.parent.mkdir(parents=True,exist_ok=True)
artifact.write_bytes(('Original synthetic input e\\u0301 원문🙂. '*900).encode('utf-8'))
source=_review(1,topic_id=topic,run_id=occ['runId'],paper_id=occ['paperId'],title=item['title'],relevance_score=100)
source.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
for index in range(2):
 source['abstract_text']='Synthetic input context version '+str(index)
 paper_reviews.upsert_review(source)
 result=ai_provider.run_ai_review_provider(topic,provider='stub',mode='stub',limit=1,import_provider_drafts=True,output_root=Path(root)/'fixture-provider',provider_run_id='fixture-journal-input-'+str(index))
 assert result['stored_draft_count']==1,result
history=ai_review_drafts.list_input_history(topic_id=topic,occurrences=[(occ['runId'],occ['paperId'])])
assert len(history)==2
print(json.dumps(dict(item=item,receipt=history[-1]),ensure_ascii=False))
`, topicA, backend.dataRoot], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const { item, receipt } = JSON.parse(seed.stdout);
  const components = receipt.packet.input_scope.components.filter((part: { chars: number }) => part.chars > 0).map((part: { json_pointer: string }) => part.json_pointer).sort();
  await page.goto(`/app/topics/${topicA}/papers?paper=${item.paperVersionId}`);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await drawer.getByText("검토 입력·결과 이력", { exact: true }).click();
  await drawer.getByRole("button", { name: `입력·결과 열기 · ${receipt.receipt_id}`, exact: true }).click();
  const choose = drawer.getByRole("button", { name: "보고서 입력에 이 기록 선택", exact: true });
  await choose.focus(); await choose.press("Enter");
  await expect(page.getByRole("region", { name: "보고서에 선택한 검토 기록" })).toContainText(receipt.receipt_id);
  await drawer.getByRole("button", { name: "논문 상세 닫기" }).click();
  await page.getByRole("button", { name: "선택 보고서 입력 고정" }).click();
  const panel = page.getByRole("region", { name: "선택 문헌 근거 초안" });
  await expect(panel.getByText("수동 진행 가능 · 0 / 3 단계")).toBeVisible();
  const jobId = new URL(page.url()).searchParams.get("synthesis")!;
  const root = `/api/topics/${topicA}/local-synthesis/${jobId}`;
  const frozen = await (await page.request.get(root)).json();
  expect(frozen.inputs.schema_version).toBe("LOCAL_EVIDENCE_SYNTHESIS_V2");
  expect(frozen.inputs.papers[0].review_input).toEqual({ schema_version: "FROZEN_REVIEW_INPUT_V1", receipt, selected_component_ids: components });
  await panel.getByText("고정 입력 1편과 근거", { exact: true }).click();
  await panel.getByText(item.title, { exact: true }).click();
  await expect(panel.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toBeVisible();
  await expect(panel.getByRole("heading", { name: "과거 검토 결과 · 원문 사실과 구분되는 해석" })).toBeVisible();
  await panel.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "review-input-frozen-wide.png") });
  for (let step=1; step<=3; step++) {
    await panel.getByRole("button", { name: "다음 한 단계 실행" }).click();
    await expect(panel.getByText(`${step===3?"근거 초안 준비됨":"수동 진행 가능"} · ${step} / 3 단계`)).toBeVisible();
  }
  await panel.getByText("저널 입력·결과 파일 교환", { exact: true }).click();
  const exchange = panel.getByRole("region", { name: "저널 결과 교환" });
  await exchange.getByRole("button", { name: "저널 고정 입력 확인" }).click();
  await expect(exchange.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toBeVisible();
  const packet = await (await page.request.get(root + "/journal-exchange")).json();
  expect(packet.review_inputs[0]).toMatchObject({ receipt_id: receipt.receipt_id, selected_component_ids: components, review_result: receipt.draft, input_scope: receipt.packet.input_scope, source_extraction: receipt.packet.source_extraction });
  const refs = packet.references.filter((ref: { kind: string }) => ref.kind === "review_input_excerpt");
  expect(refs).toHaveLength(components.length);
  expect(refs.every((ref: { review_receipt_id: string }) => ref.review_receipt_id === receipt.receipt_id)).toBe(true);
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = panel.getByRole("region", { name: "저장된 저널 생성 결과" });
  await results.getByText("새 생성 요청 준비", { exact: true }).click();
  const form = results.getByRole("form", { name: "생성 요청 준비" });
  await form.getByLabel("오프라인 합성 요청 준비").check();
  const preparedResponse = page.waitForResponse(response=>response.url().endsWith(`${root}/journal-executions`) && response.request().method()==="POST");
  await form.getByRole("button", { name: "로컬 생성 요청 준비" }).click();
  const prepared = (await (await preparedResponse).json()).execution;
  expect(prepared.contract.provider_request.prompt).toContain(receipt.receipt_id);
  const offline = results.getByRole("region", { name: "오프라인 합성 실행", exact: true });
  await offline.getByRole("button", { name: "오프라인 합성 실행 승인 기록", exact: true }).click();
  await expect(offline.getByText(/승인만 기록했습니다/)).toBeVisible();
  await offline.getByRole("button", { name: "승인한 오프라인 합성 실행", exact: true }).click();
  await expect(results.getByText(/최근 저장 상태: 응답 저장 완료 · 시도 1/)).toBeVisible();
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보" });
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집" });
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("합성 결과이며 과거 검토 입력 연결만 확인했습니다.");
  await editor.getByLabel("저널 전체 검토 메모").fill("원문 전체 검토·실제 외부 모델 실행은 미확인입니다.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const review = candidate.getByRole("article", { name: "보존된 저널 검토 기록" });
  const [download] = await Promise.all([page.waitForEvent("download"), review.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click()]);
  const output = path.join(artifacts, "review-input-journal.json"); await download.saveAs(output);
  const bundle = JSON.parse(fs.readFileSync(output, "utf8"));
  expect(bundle.candidate.packet.review_inputs).toEqual(packet.review_inputs);
  await candidate.getByText("선택한 과거 검토 문맥 1개", { exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await candidate.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "review-input-journal-narrow.png") });
  await page.goto(`/app/topics/${topicB}/reports`);
  await expect(page.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toHaveCount(0);
  expect((await page.request.get(root.replace(`/topics/${topicA}/`,`/topics/${topicB}/`))).status()).toBe(404);
  const change = spawnSync(backend.pythonExe, ["-c", `from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from app.services import paper_review
import sys
topic,run,paper=sys.argv[1:]
source=paper_reviews.get_review(topic,run,paper)
(build_run_paths(run).raw_dir/(safe_filename(paper)+'.txt')).write_text('Changed file after journal capture',encoding='utf-8')
source.update(paper_review.read_local_full_text_evidence(run_id=run,paper_id=paper,max_text_chars=24000))
paper_reviews.upsert_review(source)
`, topicA, receipt.run_id, receipt.paper_id], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(change.status, change.stderr).toBe(0);
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${jobId}`);
  await panel.getByText("고정 입력 1편과 근거", { exact: true }).click();
  await panel.getByText(item.title, { exact: true }).click();
  await expect(panel.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toBeVisible();
  const reopened = await (await page.request.get(root)).json();
  expect(reopened.inputs).toEqual(frozen.inputs);
  expect(await (await page.request.get(root + "/journal-exchange")).json()).toEqual(packet);
  const reread = await (await page.request.get(`${root}/journal-candidates/${bundle.candidate.candidate_id}/reviews/${bundle.review.review_id}/export?format=json`)).json();
  expect(JSON.parse(reread.content)).toEqual(bundle);
  await assertHygiene(ledger);
  expect(ledger.mutationRequests.every(value=>value.includes("/local-synthesis"))).toBe(true);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { explicitReceiptSelection: true, selectedComponentsFrozen: true, syntheticOnly: true, humanAccepted: true, exportRestartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
