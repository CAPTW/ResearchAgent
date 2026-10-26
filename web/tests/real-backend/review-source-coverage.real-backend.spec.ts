import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("saved source ranges distinguish synthetic input and survive read retry, source loss and restart", async ({ page, backend, context }, info) => {
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
print(json.dumps(dict(item=item,run=occ['runId'],paper=occ['paperId'],expected=source[26000:26010]),ensure_ascii=False))
`, topicA], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const fixture = JSON.parse(seed.stdout);
  const root = `/api/topics/${topicA}/paper-bank/${fixture.item.paperVersionId}/review-preparations`;
  const sources = await (await page.request.get(`${root}/sources`)).json();
  const source = sources.sources.find((value: { run_id: string; paper_id: string }) => value.run_id === fixture.run && value.paper_id === fixture.paper);
  expect(source).toBeTruthy();
  const preparations = [];
  for (const [start, end] of [[26000, 26010], [26005, 26020], [26025, 26030]]) {
    const response = await page.request.post(root, { data: { request_id: `coverage-${start}`, run_id: fixture.run, paper_id: fixture.paper, expected_source_digest: source.source_digest, start, end } });
    expect(response.status()).toBe(200); preparations.push(await response.json());
  }
  const anchor = preparations[0];
  const capability = await (await page.request.get(`${root}/${anchor.preparation_id}/offline-review`)).json();
  expect(capability.enabled).toBe(true);
  const syntheticResponse = await page.request.post(`${root}/${anchor.preparation_id}/offline-reviews`, { data: { request_id: "coverage-synthetic", expected_preparation_digest: capability.preparation_digest } });
  expect(syntheticResponse.status()).toBe(200);
  const synthetic = await syntheticResponse.json();
  const coverageUrl = `${root}/${anchor.preparation_id}/coverage`;
  const baselineResponse = await page.request.get(coverageUrl); expect(baselineResponse.status()).toBe(200);
  const baseline = await baselineResponse.json();
  expect(baseline.prepared).toMatchObject({ record_count: 3, covered_chars: 25, ranges: [{ start: 26000, end: 26020 }, { start: 26025, end: 26030 }] });
  expect(baseline.inputs.find((value: { input_origin: string }) => value.input_origin === "FIXTURE_SYNTHETIC")).toMatchObject({ record_count: 1, covered_chars: 10, ranges: [{ start: 26000, end: 26010 }] });

  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  const open = async () => {
    await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
    await drawer.getByRole("button", { name: `준비 기록 열기 · ${anchor.preparation_id}`, exact: true }).click();
    const summary = drawer.getByText("보존된 출처의 범위 요약", { exact: true });
    await summary.focus(); await summary.press("Enter");
  };
  await open();
  const overview = drawer.getByRole("region", { name: "보존된 출처 범위", exact: true });
  await expect(overview.getByText("준비 기록 3건 · 중복 제외 25자", { exact: true })).toBeVisible();
  await expect(overview.getByText("준비된 범위: 26000–26020, 26025–26030", { exact: true })).toBeVisible();
  await expect(overview.getByRole("region", { name: "합성 입력·결과", exact: true }).getByText("입력·결과 기록 1건 · 중복 제외 10자")).toBeVisible();
  await expect(overview.getByRole("region", { name: "Provider 응답 기록", exact: true }).getByText("입력·결과 기록 0건 · 중복 제외 0자")).toBeVisible();
  await overview.getByText("준비 기록별 범위", { exact: true }).click();
  await overview.getByRole("button", { name: `준비 기록 열기 · ${preparations[1].preparation_id}`, exact: true }).click();
  await expect(drawer.getByText("저장된 구간: 26005–26020 · 15자 (Unicode 코드 포인트)", { exact: true })).toBeVisible();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${anchor.preparation_id}`, exact: true }).click();
  await drawer.getByText("보존된 출처의 범위 요약", { exact: true }).click();
  await overview.getByText("합성 입력·결과 기록별 범위", { exact: true }).click();
  const receiptRead = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/review-inputs/${synthetic.receipt_id}`));
  await overview.getByRole("button", { name: `입력·결과 열기 · ${synthetic.receipt_id}`, exact: true }).click();
  expect((await (await receiptRead).json()).receipt_id).toBe(synthetic.receipt_id);
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true }).getByText("원래 정규화 본문 위치: 26000–26010 · 선택 10자", { exact: true })).toBeVisible();

  let failed = false;
  await page.route(`**${coverageUrl}`, async route => {
    if (!failed) { failed = true; await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "review_source_coverage_limit_exceeded" }) }); }
    else await route.continue();
  });
  await overview.getByRole("button", { name: "범위 요약 새로고침", exact: true }).click();
  await expect(overview.getByRole("alert")).toContainText("기록 수가 조회 한도를 넘어");
  await expect(overview.getByText("준비 기록 3건 · 중복 제외 25자", { exact: true })).toHaveCount(0);
  await overview.getByRole("button", { name: "범위 요약 새로고침", exact: true }).press("Enter");
  await expect(overview.getByText("준비 기록 3건 · 중복 제외 25자", { exact: true })).toBeVisible();
  await overview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "source-coverage-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await overview.getByText("준비 기록 3건 · 중복 제외 25자", { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const bounds = await overview.boundingBox(); expect(bounds).not.toBeNull(); expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: path.join(artifacts, "source-coverage-narrow.png") });

  const remove = spawnSync(backend.pythonExe, ["-c", `import sys
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
root=build_run_paths(sys.argv[1]).raw_dir.resolve()
target=(root/(safe_filename(sys.argv[2])+'.txt')).resolve()
assert target.is_relative_to(root)
target.unlink()
`, fixture.run, fixture.paper], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(remove.status, remove.stderr).toBe(0);
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(coverageUrl)).json()).toEqual(baseline);
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`); await open();
  await expect(overview.getByText("준비 기록 3건 · 중복 제외 25자", { exact: true })).toBeVisible();
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.expected);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(overview).toHaveCount(0);
  expect((await page.request.get(coverageUrl.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  const expectedErrors = ledger.consoleErrors.splice(0);
  expect(expectedErrors).toHaveLength(1); expect(expectedErrors[0]).toContain("503");
  ledger.handledExpectedErrors.push(...expectedErrors);
  await assertHygiene(ledger); expect(ledger.mutationRequests).toEqual([]);
  fs.writeFileSync(path.join(artifacts, "source-coverage-observations.json"), JSON.stringify({ baseline, synthetic, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { originalRanges: true, syntheticSeparate: true, readRetry: true, sourceLossRestartEqual: true, topicIsolation: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
