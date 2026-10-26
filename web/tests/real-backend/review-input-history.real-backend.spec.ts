import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("preserved synthetic review inputs survive source changes, retry and backend restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const seed = spawnSync(backend.pythonExe, ["-c", `import hashlib,json,sys
from pathlib import Path
from app.services import ai_provider,paper_bank,paper_review
from app.repositories import paper_reviews,ai_review_drafts
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from tests.test_ai_review_provider import _review
topic,root=sys.argv[1:]; item=paper_bank.list_topic_papers(topic,page_size=100)['items'][0]
occ=item['compatibility']['occurrences'][0]
source=_review(1,topic_id=topic,run_id=occ['runId'],paper_id=occ['paperId'],title=item['title'],relevance_score=100)
artifact=build_run_paths(occ['runId']).raw_dir/(safe_filename(occ['paperId'])+'.txt')
artifact.parent.mkdir(parents=True,exist_ok=True)
original=('Preserved synthetic evidence e\\u0301 원문🙂. '*700).encode('utf-8')
artifact.write_bytes(original)
source.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
assert source['extraction_provenance']['artifact']['sha256']==hashlib.sha256(original).hexdigest()
assert source['extraction_provenance']['selection']['truncation']=='TRUNCATED'
paper_reviews.upsert_review(source)
for i in range(2):
 source['abstract_text']='Synthetic context revision '+str(i)
 paper_reviews.upsert_review(source)
 outcome=ai_provider.run_ai_review_provider(topic,provider='stub',mode='stub',limit=1,import_provider_drafts=True,output_root=Path(root)/'fixture-provider',provider_run_id='fixture-input-'+str(i))
 assert outcome['stored_draft_count']==1,outcome
history=ai_review_drafts.list_input_history(topic_id=topic,occurrences=[(occ['runId'],occ['paperId'])])
assert len(history)==2
assert all(row['packet']['source_extraction']==source['extraction_provenance'] for row in history)
artifact.write_text('Changed current text must never replace preserved input',encoding='utf-8')
source.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
assert source['extraction_provenance']['artifact']['sha256']!=hashlib.sha256(original).hexdigest()
paper_reviews.upsert_review(source)
print(json.dumps(dict(item=item,history=history),ensure_ascii=False))
`, topicA, backend.dataRoot], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const { item, history } = JSON.parse(seed.stdout);
  const root = `/api/topics/${topicA}/paper-bank/${item.paperVersionId}/review-inputs`;
  const first = history[0];
  const extraction = first.packet.source_extraction;
  await page.goto(`/app/topics/${topicA}/papers?paper=${item.paperVersionId}`);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await expect(drawer.getByRole("heading", { name: item.title, exact: true })).toBeVisible();
  await expect(drawer.getByText("본문 처리 기록", { exact: true })).toBeVisible();
  const disclosure = drawer.getByText("검토 입력·결과 이력", { exact: true });
  await disclosure.focus(); await disclosure.press("Enter");
  await expect(drawer.getByRole("button", { name: /^입력·결과 열기/ })).toHaveCount(2);
  const select = drawer.getByRole("button", { name: `입력·결과 열기 · ${first.receipt_id}`, exact: true });
  await select.focus(); await select.press("Enter");
  const receipt = drawer.getByRole("region", { name: "선택한 검토 입력·결과" });
  await expect(receipt.getByText("합성 실행 · 실제 AI 본문 검토가 아닙니다.", { exact: true })).toBeVisible();
  await expect(receipt.getByRole("heading", { name: "보존된 검토 입력과 결과" })).toBeFocused();
  await expect(receipt.getByText(/전체 문서 길이: 미확인/)).toBeVisible();
  await receipt.getByText(/^근거 1 ·/).click();
  const evidence = receipt.locator("details").filter({ has: page.locator("summary", { hasText: /^근거 1 ·/ }) });
  const frozenText = first.packet.evidence_spans[0].text;
  await expect(evidence.getByLabel("보존된 근거 전체", { exact: true })).toHaveText(frozenText);
  const search = evidence.getByRole("textbox", { name: "근거 안에서 문구 찾기" });
  const writesBeforeSearch = ledger.mutationRequests.length;
  await search.fill("Preserved synthetic evidence");
  await expect(evidence.getByRole("status")).toContainText("일치 1 /");
  await search.press("Enter");
  await expect(search).toBeFocused();
  await search.fill("No exact phrase in this frozen input");
  await expect(evidence.getByRole("status")).toContainText("일치하는 문구가 없습니다");
  await evidence.getByRole("button", { name: "문구 찾기 지우기" }).click();
  await expect(evidence.getByLabel("보존된 근거 전체", { exact: true })).toHaveText(frozenText);
  expect(ledger.mutationRequests.length).toBe(writesBeforeSearch);
  await expect(receipt.getByText(`정규화 후 선택: ${extraction.selection.chars}자 · 선택 한도: ${extraction.selection.max_chars}자 · 선택 전 텍스트: ${extraction.selection.available_chars}자`, { exact: true })).toBeVisible();
  await expect(receipt.getByText("선택 범위 잘림: 잘림 확인", { exact: true })).toBeVisible();
  await receipt.getByText("원문·추출 기록 상세", { exact: true }).click();
  await expect(receipt.getByText(`당시 읽은 파일: 텍스트 · ${extraction.artifact.bytes}바이트`, { exact: true })).toBeVisible();
  await expect(receipt.getByText(`파일 SHA-256: ${extraction.artifact.sha256}`, { exact: true })).toBeVisible();
  await receipt.getByText("원문·추출 기록 상세", { exact: true }).click();
  const excerpt = receipt.getByLabel("선택 발췌 1", { exact: true });
  await expect(excerpt).toHaveText(first.packet.selected_text_excerpts[0].text);
  const excerptToggle = receipt.getByText(/^발췌 1 · 출처:/);
  await excerptToggle.focus(); await excerptToggle.press("Enter");
  await expect(excerpt).toBeVisible();
  await excerptToggle.press("Enter");
  await expect(excerpt).not.toBeVisible();
  await receipt.getByText("보존된 입력 전체(JSON)", { exact: true }).click();
  const input = receipt.getByLabel("보존된 검토 입력 JSON");
  expect(JSON.parse((await input.textContent())!)).toEqual(first.packet);
  await receipt.getByText("보존된 입력 전체(JSON)", { exact: true }).click();
  await receipt.getByText(/전체 문서 길이: 미확인/).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "review-input-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await receipt.getByText(/전체 문서 길이: 미확인/).scrollIntoViewIfNeeded();
  await expect(receipt.getByText(/전체 문서 길이: 미확인/)).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "review-input-narrow.png") });
  await assertHygiene(ledger);
  // Real disconnected backend, not a successful empty response or stubbed API.
  await backend.stop();
  await drawer.getByRole("button", { name: "검토 입력 이력 새로고침" }).click();
  await expect(drawer.getByRole("alert")).toContainText("검토 입력 이력 조회 오류");
  await expect(excerpt).toHaveText(first.packet.selected_text_excerpts[0].text);
  await backend.start();
  await drawer.getByRole("button", { name: "검토 입력 이력 새로고침" }).click();
  await expect(drawer.getByRole("alert")).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: /^입력·결과 열기/ })).toHaveCount(2);
  expect(await (await page.request.get(`${root}/${first.receipt_id}`)).json()).toMatchObject({ packet: first.packet, draft: first.draft });
  expect((await page.request.get(root.replace(topicA, topicB))).status()).toBe(404);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(page.getByText(first.receipt_id, { exact: true })).toHaveCount(0);
  await page.goto(`/app/topics/${topicA}/papers?paper=${item.paperVersionId}`);
  await disclosure.click(); await select.click();
  await expect(excerpt).toHaveText(first.packet.selected_text_excerpts[0].text);
  await expect(receipt.getByText("선택 범위 잘림: 잘림 확인", { exact: true })).toBeVisible();
  await receipt.getByText("원문·추출 기록 상세", { exact: true }).click();
  await expect(receipt.getByText(`파일 SHA-256: ${extraction.artifact.sha256}`, { exact: true })).toBeVisible();
  // Only the deliberate disconnected request may emit a resource console error.
  const expectedDisconnects = ledger.consoleErrors.splice(0);
  expect(expectedDisconnects.length).toBeGreaterThan(0);
  expect(expectedDisconnects.every(value => value.includes("ERR_CONNECTION_REFUSED"))).toBe(true);
  ledger.handledExpectedErrors.push(...expectedDisconnects);
  await assertHygiene(ledger);
  expect(ledger.mutationRequests).toEqual([]);
  fs.writeFileSync(path.join(artifacts, "review-input-observations.json"), JSON.stringify({ receiptIds: history.map((row: { receipt_id: string }) => row.receipt_id), expectedDisconnects, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { syntheticOnly: true, sourceChangePreserved: true, extractionProvenancePreserved: true, errorRetry: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
