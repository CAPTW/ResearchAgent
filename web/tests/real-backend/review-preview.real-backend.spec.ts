import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import type { Route } from "@playwright/test";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("body preview selects exact Unicode coordinates and refuses a changed source before capture", async ({ page, backend, context }, info) => {
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
tail='e\\u0301 한글🙂 later methods and results.'
source='a'*26000+tail
artifact=build_run_paths(occ['runId']).raw_dir/(safe_filename(occ['paperId'])+'.txt')
artifact.write_bytes(source.encode('utf-8'))
review=_review(1,topic_id=topic,run_id=occ['runId'],paper_id=occ['paperId'],title=item['title'])
review.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
paper_reviews.upsert_review(review)
print(json.dumps(dict(item=item,run=occ['runId'],paper=occ['paperId'],tail=tail,end=len(source)),ensure_ascii=False))
`, topicA], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const fixture = JSON.parse(seed.stdout);
  const root = `/api/topics/${topicA}/paper-bank/${fixture.item.paperVersionId}/review-preparations`;
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await expect(page).toHaveURL(/\/app\/topics\/.+\/papers/);
  await expect(page).toHaveTitle("XRML Research Agent");
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  const sources = drawer.getByLabel("저장 본문 출처", { exact: true });
  await expect(sources).toBeVisible();
  const label = (await sources.locator("option").allTextContents()).find(value => value.includes(fixture.run) && value.includes(fixture.paper));
  expect(label).toBeTruthy();
  await sources.selectOption({ label: label! });
  const view = drawer.getByLabel("저장 본문 미리보기", { exact: true });
  expect((await (await page.request.get(root)).json()).items).toEqual([]);
  let lost = false;
  await page.route(`**${root}/preview`, async route => {
    if (!lost) { lost = true; await route.abort("failed"); }
    else await route.continue();
  });
  const open = drawer.getByRole("button", { name: "본문 미리보기", exact: true });
  await open.focus(); await open.press("Enter");
  const retry = drawer.getByRole("button", { name: "본문 미리보기 다시 시도", exact: true });
  await expect(retry).toBeVisible();
  await expect(view).toHaveCount(0);
  await retry.click();
  await expect(view).toHaveValue("a".repeat(4000));
  let release!: () => void, arrived!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const received = new Promise<void>(resolve => { arrived = resolve; });
  const delay = async (route: Route) => {
    const response = await route.fetch();
    arrived(); await held;
    await route.fulfill({ response });
  };
  await page.route(`**${root}/preview`, delay);
  await drawer.getByRole("button", { name: "다음 본문 구간", exact: true }).click();
  await received;
  await expect(view).toHaveCount(0);
  await drawer.getByRole("button", { name: "미리보기 요청 취소", exact: true }).click();
  release();
  await page.unroute(`**${root}/preview`, delay);
  await expect(drawer.getByRole("button", { name: "미리보기 요청 취소", exact: true })).toHaveCount(0);
  await expect(view).toHaveCount(0);
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("0");
  await drawer.getByLabel("미리보기 시작 위치", { exact: true }).fill("0");
  await drawer.getByRole("button", { name: "이 위치 미리보기", exact: true }).click();
  await expect(view).toHaveValue("a".repeat(4000));
  await drawer.getByRole("button", { name: "다음 본문 구간", exact: true }).click();
  await expect(drawer.getByLabel("미리보기 시작 위치", { exact: true })).toHaveValue("4000");
  await drawer.getByRole("button", { name: "이전 본문 구간", exact: true }).click();
  await expect(drawer.getByLabel("미리보기 시작 위치", { exact: true })).toHaveValue("0");
  await drawer.getByLabel("미리보기 시작 위치", { exact: true }).fill("26000");
  await drawer.getByRole("button", { name: "이 위치 미리보기", exact: true }).click();
  await expect(view).toHaveValue(fixture.tail);
  await view.focus(); await view.press("Control+Home"); await view.press("Control+a");
  const apply = drawer.getByRole("button", { name: "선택한 본문을 고정 범위로 사용", exact: true });
  await expect(apply).toBeEnabled();
  await apply.focus(); await apply.press("Enter");
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26000");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue(String(fixture.end));
  expect((await (await page.request.get(root)).json()).items).toEqual([]);
  await view.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "review-preview-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await view.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  for (const control of [view, apply, drawer.getByLabel("미리보기 시작 위치", { exact: true })]) {
    const bounds = await control.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  }
  await page.screenshot({ path: path.join(artifacts, "review-preview-narrow.png") });
  await drawer.getByRole("button", { name: "선택 구간 고정", exact: true }).click();
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.tail);
  const listing = await (await page.request.get(root)).json();
  expect(listing.items).toHaveLength(1);
  const saved = await (await page.request.get(`${root}/${listing.items[0].preparation_id}`)).json();
  expect(saved.selection).toMatchObject({ start: 26000, end: fixture.end, text: fixture.tail, chars: Array.from(fixture.tail).length });
  const changed = "Changed source after the preview. 새 본문🙂";
  const update = spawnSync(backend.pythonExe, ["-c", `import sys
from app.services import paper_review
from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
topic,run,paper,text=sys.argv[1:]
review=paper_reviews.get_review(topic,run,paper)
(build_run_paths(run).raw_dir/(safe_filename(paper)+'.txt')).write_bytes(text.encode('utf-8'))
review.update(paper_review.read_local_full_text_evidence(run_id=run,paper_id=paper,max_text_chars=24000))
paper_reviews.upsert_review(review)
`, topicA, fixture.run, fixture.paper, changed], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(update.status, update.stderr).toBe(0);
  await drawer.getByLabel("미리보기 시작 위치", { exact: true }).fill("0");
  await drawer.getByRole("button", { name: "이 위치 미리보기", exact: true }).click();
  await expect(drawer.getByText(/review_preparation_source_changed/)).toBeVisible();
  await expect(view).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: "선택 구간 고정", exact: true })).toBeDisabled();
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26000");
  await drawer.getByRole("button", { name: "저장 본문 출처 새로고침", exact: true }).click();
  await expect(open).toBeEnabled();
  await expect(drawer.getByText(/출처가 변경되어 숫자 위치만 유지했습니다/)).toBeVisible();
  await open.click();
  await expect(view).toHaveValue(changed);
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.tail);
  expect(await (await page.request.get(`${root}/${saved.preparation_id}`)).json()).toEqual(saved);
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${saved.preparation_id}`, exact: true }).click();
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.tail);
  expect(await (await page.request.get(`${root}/${saved.preparation_id}`)).json()).toEqual(saved);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(view).toHaveCount(0);
  const forbidden = await page.request.post(root.replace(`/topics/${topicA}/`, `/topics/${topicB}/`) + "/preview", { data: { run_id: fixture.run, paper_id: fixture.paper, expected_source_digest: saved.source.source_digest, start: 0, length: 4000 } });
  expect(forbidden.status()).toBe(404);
  const expectedErrors = ledger.consoleErrors.splice(0);
  expect(expectedErrors.some(value => value.includes("ERR_FAILED"))).toBe(true);
  expect(expectedErrors.every(value => value.includes("ERR_FAILED") || value.includes("ERR_ABORTED") || value.includes("409 (Conflict)"))).toBe(true);
  ledger.handledExpectedErrors.push(...expectedErrors);
  await assertHygiene(ledger);
  expect(ledger.mutationRequests.filter(value => !value.endsWith("/preview"))).toEqual([`POST ${root}`]);
  fs.writeFileSync(path.join(artifacts, "review-preview-observations.json"), JSON.stringify({ saved, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { previewReadOnly: true, cancelledPreview: true, unicodeSelection: true, changedSourceRefused: true, keyboard: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
