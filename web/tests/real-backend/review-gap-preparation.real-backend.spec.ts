import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("remaining source range is previewed before explicit replacement and rejects a changed source", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const seed = spawnSync(backend.pythonExe, ["-c", `import json,sys
from app.services import paper_bank,paper_review
from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from tests.test_ai_review_provider import _review
topic=sys.argv[1]; item=paper_bank.list_topic_papers(topic,page_size=100)['items'][0]
occ=item['compatibility']['occurrences'][0]
source='A'*26000+'e\\u0301 한글🙂 later methods and results '*300
artifact=build_run_paths(occ['runId']).raw_dir/(safe_filename(occ['paperId'])+'.txt')
artifact.write_bytes(source.encode('utf-8'))
review=_review(1,topic_id=topic,run_id=occ['runId'],paper_id=occ['paperId'],title=item['title'])
review.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
paper_reviews.upsert_review(review)
print(json.dumps(dict(item=item,run=occ['runId'],paper=occ['paperId']),ensure_ascii=False))
`, topicA], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const fixture = JSON.parse(seed.stdout);
  const root = `/api/topics/${topicA}/paper-bank/${fixture.item.paperVersionId}/review-preparations`;
  const observed = await (await page.request.get(`${root}/sources`)).json();
  const source = observed.sources.find((value: { run_id: string; paper_id: string }) => value.run_id === fixture.run && value.paper_id === fixture.paper);
  const response = await page.request.post(root, { data: { request_id: "gap-anchor", run_id: fixture.run, paper_id: fixture.paper, expected_source_digest: source.source_digest, start: 26000, end: 26010 } });
  expect(response.status()).toBe(200); const anchor = await response.json();
  const url = `/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`;
  await page.goto(url);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByLabel("시작 위치", { exact: true }).fill("17");
  await drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true }).fill("23");
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${anchor.preparation_id}`, exact: true }).click();
  await drawer.getByText("보존된 출처의 범위 요약", { exact: true }).click();
  const overview = drawer.getByRole("region", { name: "보존된 출처 범위", exact: true });
  await overview.getByLabel("살펴볼 미준비 구간", { exact: true }).selectOption(`26010:${source.source_chars}`);
  const reader = overview.getByRole("region", { name: "미준비 구간 이어서 준비", exact: true });
  async function read() {
    await overview.getByRole("button", { name: "선택한 미준비 구간 열기", exact: true }).click();
    await reader.getByRole("button", { name: "본문 미리보기", exact: true }).press("Enter");
    await expect(reader.getByRole("textbox", { name: "저장 본문 미리보기", exact: true })).toBeVisible();
  }
  await read();
  const text = await reader.getByRole("textbox", { name: "저장 본문 미리보기", exact: true }).inputValue();
  expect(Array.from(text)).toHaveLength(4000); expect(text).toContain("e\u0301 한글🙂");
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("17");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue("23");
  await reader.getByRole("button", { name: "미준비 구간 닫기", exact: true }).click();
  await expect(reader).toHaveCount(0);
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("17");
  await read();
  const textarea = reader.getByRole("textbox", { name: "저장 본문 미리보기", exact: true });
  await textarea.focus(); await textarea.press("ControlOrMeta+a");
  await reader.getByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기", exact: true }).press("Enter");
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26010");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue("30010");
  expect(ledger.mutationRequests.every(value => value.endsWith("/preview"))).toBe(true);
  const savedResponse = page.waitForResponse(value => new URL(value.url()).pathname === root && value.request().method() === "POST");
  await drawer.getByRole("button", { name: "선택 구간 고정", exact: true }).press("Enter");
  const savedResult = await savedResponse;
  expect(savedResult.status()).toBe(200);
  const saved = await savedResult.json();
  expect(saved.selection).toMatchObject({ start: 26010, end: 30010, chars: 4000, text });
  expect(saved.source.source_digest).toBe(source.source_digest);
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(text);
  await drawer.getByText("보존된 출처의 범위 요약", { exact: true }).click();
  await expect(overview.getByText("준비 기록 2건 · 중복 제외 4010자", { exact: true })).toBeVisible();
  const baseline = await (await page.request.get(`${root}/${saved.preparation_id}/coverage`)).json();
  await overview.getByLabel("살펴볼 미준비 구간", { exact: true }).selectOption(`30010:${source.source_chars}`); await read();
  await reader.scrollIntoViewIfNeeded(); await page.screenshot({ path: path.join(artifacts, "gap-preparation-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await textarea.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "gap-preparation-narrow.png") });
  await reader.getByRole("button", { name: "미준비 구간 닫기", exact: true }).click();
  const changed = spawnSync(backend.pythonExe, ["-c", `import sys
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
root=build_run_paths(sys.argv[1]).raw_dir.resolve()
target=(root/(safe_filename(sys.argv[2])+'.txt')).resolve()
assert target.is_relative_to(root)
target.write_bytes(target.read_bytes()+b' changed')
`, fixture.run, fixture.paper], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(changed.status, changed.stderr).toBe(0);
  await overview.getByRole("button", { name: "선택한 미준비 구간 열기", exact: true }).click();
  await reader.getByRole("button", { name: "본문 미리보기", exact: true }).click();
  await expect(reader.getByRole("alert")).toContainText("source_changed");
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26010");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue("30010");
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(`${root}/${saved.preparation_id}/coverage`)).json()).toEqual(baseline);
  expect(await (await page.request.get(`${root}/${saved.preparation_id}`)).json()).toEqual(saved);
  await page.goto(`/app/topics/${topicB}/papers`); await expect(reader).toHaveCount(0);
  const errors = ledger.consoleErrors.splice(0); expect(errors).toHaveLength(1); expect(errors[0]).toContain("409"); ledger.handledExpectedErrors.push(...errors);
  await assertHygiene(ledger); expect(ledger.mutationRequests.filter(value => !value.endsWith("/preview"))).toEqual([`POST ${root}`]);
  fs.writeFileSync(path.join(artifacts, "gap-preparation-observations.json"), JSON.stringify({ saved, baseline, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { explicitSelection: true, inputPreservedBeforeApply: true, sourceChangedRefused: true, restartedHistoryEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
