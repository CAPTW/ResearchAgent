import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("unavailable extraction does not block another source and preserved ranges reopen after restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const seed = spawnSync(backend.pythonExe, ["-c", `import json,sys
from tests.test_review_preparation_sources import seed_sources
print(json.dumps(seed_sources(sys.argv[1]),ensure_ascii=False))
`, topicA], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const fixture = JSON.parse(seed.stdout);
  const [bad, good] = fixture.entries;
  const root = `/api/topics/${topicA}/paper-bank/${fixture.version}/review-preparations`;
  const url = `/app/topics/${topicA}/papers?paper=${fixture.version}`;
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  const warning = drawer.getByRole("region", { name: "사용할 수 없는 저장 본문 출처" });
  const source = drawer.getByLabel("저장 본문 출처", { exact: true });
  const start = drawer.getByLabel("시작 위치", { exact: true });
  const end = drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true });
  const freeze = drawer.getByRole("button", { name: "선택 구간 고정", exact: true });
  const refresh = drawer.getByRole("button", { name: "저장 본문 출처 새로고침", exact: true });
  const preserved = drawer.getByLabel("보존된 준비 본문");
  async function showWarning() {
    await warning.evaluate(element => element.scrollIntoView({ block: "center", behavior: "instant" }));
    const header = await page.locator(".v2-topbar").boundingBox();
    const heading = await warning.getByRole("heading").boundingBox();
    expect(heading!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
  }
  function change(entry: { path: string; text: string }, restore = false) {
    const result = spawnSync(backend.pythonExe, ["-c", `import os,sys
from pathlib import Path
target=Path(sys.argv[1]).resolve(); root=Path(os.environ['XRML_DATA_DIR']).resolve()
assert target.is_relative_to(root)
target.write_bytes(sys.argv[2].encode('utf-8'))
`, entry.path, restore ? entry.text : "Changed disposable extraction"], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 15000 });
    expect(result.status, result.stderr).toBe(0);
  }
  await page.goto(url);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await expect(source).toHaveValue(JSON.stringify([bad.run_id, bad.paper_id]));
  await start.fill("0"); await end.fill("12");
  await freeze.focus(); await freeze.press("Enter");
  await expect(preserved).toHaveText(bad.text.slice(0, 12));
  change(bad);
  await refresh.click();
  await expect(warning).toContainText(bad.run_id);
  await expect(drawer.getByText(/선택했던 출처를 현재 사용할 수 없습니다/)).toBeVisible();
  await expect(source).toHaveValue("");
  await expect(end).toHaveValue("12");
  await expect(freeze).toBeDisabled();
  await source.focus(); await source.press("End"); await source.press("Enter");
  await expect(source).toHaveValue(JSON.stringify([good.run_id, good.paper_id]));
  await start.fill("0"); await end.fill("12");
  await freeze.focus(); await freeze.press("Enter");
  await expect(preserved).toHaveText(good.text.slice(0, 12));
  const records = (await (await page.request.get(root)).json()).items;
  expect(records).toHaveLength(2);
  const saved = await Promise.all(records.map(async (record: { preparation_id: string }) =>
    (await page.request.get(`${root}/${record.preparation_id}`)).json()));
  expect(saved.every(record => record.execution.started === false)).toBe(true);
  await showWarning();
  await page.screenshot({ path: path.join(artifacts, "source-availability-desktop.png") });
  change(good);
  await refresh.click();
  await expect(warning.getByRole("listitem")).toHaveCount(2);
  await expect(drawer.getByText(/준비할 수 있는 저장 본문이 없습니다/)).toBeVisible();
  await expect(freeze).toBeDisabled();
  await expect(end).toHaveValue("12");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await showWarning();
  const bounds = await warning.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: path.join(artifacts, "source-availability-narrow.png") });
  await page.goto("about:blank"); await backend.restart();
  await page.goto(url); await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await expect(warning.getByRole("listitem")).toHaveCount(2);
  for (const record of saved) {
    await drawer.getByRole("button", { name: `준비 기록 열기 · ${record.preparation_id}`, exact: true }).click();
    await expect(preserved).toHaveText(record.selection.text);
    expect(await (await page.request.get(`${root}/${record.preparation_id}`)).json()).toEqual(record);
  }
  change(good, true); await refresh.click();
  await expect(source).toHaveValue(JSON.stringify([good.run_id, good.paper_id]));
  await expect(warning.getByRole("listitem")).toHaveCount(1);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(page.getByLabel("사용할 수 없는 저장 본문 출처")).toHaveCount(0);
  expect((await page.request.get(`${root}/sources`.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  await assertHygiene(ledger);
  expect(ledger.mutationRequests).toHaveLength(2);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { mixedSources: true, noAutoSelection: true, savedCoordinates: true, allUnavailableVisible: true, restartExact: true, explicitRecovery: true, noExecution: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});

test("tail range preparation survives a lost response, missing source and restart without execution", async ({ page, backend, context }, info) => {
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
source='a'*26000+'e\\u0301 한글🙂 later methods and results '*300
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
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  const sources = drawer.getByLabel("저장 본문 출처");
  await expect(sources).toBeVisible();
  const options = await sources.locator("option").allTextContents();
  const label = options.find(value => value.includes(fixture.run) && value.includes(fixture.paper));
  expect(label).toBeTruthy();
  await sources.selectOption({ label: label! });
  await drawer.getByLabel("시작 위치", { exact: true }).fill("26000");
  await drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true }).fill("26010");

  let lost = false;
  await page.route(`**${root}`, async route => {
    if (route.request().method() === "POST" && !lost) {
      lost = true;
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.abort("failed");
    } else await route.continue();
  });
  const freeze = drawer.getByRole("button", { name: "선택 구간 고정", exact: true });
  await freeze.focus(); await freeze.press("Enter");
  const retry = drawer.getByRole("button", { name: "같은 고정 요청 다시 보내기", exact: true });
  await expect(retry).toBeVisible();
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26000");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue("26010");
  await retry.focus(); await retry.press("Enter");
  const preserved = drawer.getByLabel("보존된 준비 본문");
  await expect(preserved).toHaveText(fixture.expected);
  const listing = await (await page.request.get(root)).json();
  expect(listing.items).toHaveLength(1);
  const saved = await (await page.request.get(`${root}/${listing.items[0].preparation_id}`)).json();
  expect(saved.selection).toMatchObject({ start: 26000, end: 26010, text: fixture.expected, chars: 10 });
  expect(saved.state).toBe("PREPARED_NOT_EXECUTED");
  expect(saved.execution).toEqual({ started: false, full_document_review: "NOT_ESTABLISHED" });
  const [download] = await Promise.all([page.waitForEvent("download"), drawer.getByRole("button", { name: "준비 기록 JSON 내려받기" }).click()]);
  const output = path.join(artifacts, "prepared-range.json");
  await download.saveAs(output);
  expect(JSON.parse(fs.readFileSync(output, "utf8"))).toEqual(saved);
  await preserved.focus();
  await page.screenshot({ path: path.join(artifacts, "review-preparation-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await preserved.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "review-preparation-narrow.png") });
  const geometry = await drawer.evaluate(element => ({
    viewport: innerWidth,
    drawer: element.getBoundingClientRect().toJSON(),
    ancestors: [element, ...function* () { let parent = element.parentElement; while (parent) { yield parent; parent = parent.parentElement; } }()].map(node => ({
      className: node.className, width: node.getBoundingClientRect().width, scrollWidth: node.scrollWidth,
      columns: getComputedStyle(node).gridTemplateColumns, minWidth: getComputedStyle(node).minWidth,
    })),
  }));
  fs.writeFileSync(path.join(artifacts, "review-preparation-geometry.json"), JSON.stringify(geometry, null, 2));
  expect(geometry.drawer.x).toBeGreaterThanOrEqual(0);
  expect(geometry.drawer.right).toBeLessThanOrEqual(geometry.viewport);
  const visibleText = await preserved.boundingBox();
  expect(visibleText).not.toBeNull();
  expect(visibleText!.x).toBeGreaterThanOrEqual(0);
  expect(visibleText!.x + visibleText!.width).toBeLessThanOrEqual(geometry.viewport);
  for (const control of [sources, drawer.getByLabel("시작 위치", { exact: true }), drawer.getByLabel("끝 위치 (포함하지 않음)"), drawer.getByRole("button", { name: "논문 상세 닫기", exact: true })]) {
    const bounds = await control.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(geometry.viewport);
  }

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
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${saved.preparation_id}`, exact: true }).click();
  await expect(preserved).toHaveText(fixture.expected);
  expect(await (await page.request.get(`${root}/${saved.preparation_id}`)).json()).toEqual(saved);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(page.getByLabel("보존된 준비 본문")).toHaveCount(0);
  expect((await page.request.get(`${root}/${saved.preparation_id}`.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  const expectedErrors = ledger.consoleErrors.splice(0);
  expect(expectedErrors.length).toBeGreaterThan(0);
  expect(expectedErrors.every(message => message.includes("ERR_FAILED"))).toBe(true);
  ledger.handledExpectedErrors.push(...expectedErrors);
  await assertHygiene(ledger);
  expect(ledger.mutationRequests.length).toBe(2);
  expect(ledger.mutationRequests.every(value => value.includes("/review-preparations"))).toBe(true);
  fs.writeFileSync(path.join(artifacts, "review-preparation-observations.json"), JSON.stringify({ saved, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { tailRange: true, lostResponseDeduplicated: true, missingSourceReadback: true, restartEqual: true, noExecution: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
