import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

for (const transport of ["CLI", "CODEX_V2", "HTTP", "ANTHROPIC"] as const) {
const variant = transport === "ANTHROPIC" ? "HTTP" : transport === "CODEX_V2" ? "CLI" : transport;
test(`${transport} history safely saves a received fixture, cancels and survives lost response and restart`, async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const consoleErrors: { text: string; url: string }[] = [];
  page.on("console", message => { if (message.type() === "error") consoleErrors.push({ text: message.text(), url: message.location().url }); });
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const fixtureEnv = { ...process.env, XRML_DATA_DIR: backend.dataRoot };
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
`, topicA], { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seed.status, seed.stderr).toBe(0);
  const fixture = JSON.parse(seed.stdout);
  const root = `/api/topics/${topicA}/paper-bank/${fixture.item.paperVersionId}/review-preparations`;
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  const sources = drawer.getByLabel("저장 본문 출처");
  await expect(sources).toBeVisible();
  const label = (await sources.locator("option").allTextContents()).find(value => value.includes(fixture.run) && value.includes(fixture.paper));
  expect(label).toBeTruthy(); await sources.selectOption({ label: label! });
  await drawer.getByLabel("시작 위치", { exact: true }).fill("26000");
  await drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true }).fill("26010");
  const captured = page.waitForResponse(response => new URL(response.url()).pathname === root && response.request().method() === "POST");
  await drawer.getByRole("button", { name: "선택 구간 고정", exact: true }).click();
  expect((await captured).status()).toBe(200);
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.expected);
  const preparation = await (await captured).json();
  const base = `${root}/${preparation.preparation_id}/transport-executions`;
  const seeded = spawnSync(backend.pythonExe, ["-c", `import json,sys
from tests.test_review_transport_history import seed_transport as seed_cli
from tests.test_review_http_dispatch import seed_http
from tests.test_anthropic_binding import seed_anthropic
from functools import partial
seed_transport=seed_anthropic if sys.argv[2]=='ANTHROPIC' else seed_http if sys.argv[2]=='HTTP' else partial(seed_cli,codex_version='2') if sys.argv[2]=='CODEX_V2' else seed_cli
preparation=json.loads(sys.argv[1])
items={}
states=('refusal','incomplete') if sys.argv[2]=='ANTHROPIC' else ('failed','incomplete','cancelled') if sys.argv[2]=='HTTP' else ()
for state in (*states,'READY','RESULT_RECEIVED','INTERRUPTED'):
    _,saved=seed_transport(preparation,request=state,state=state)
    items[state]=saved['contract']['execution_id']
print(json.dumps(items))
`, JSON.stringify(preparation), transport], { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 30000 });
  expect(seeded.status, seeded.stderr).toBe(0);
  const identities = JSON.parse(seeded.stdout);
  const summary = drawer.getByText("검토 실행 조건·이력", { exact: true });
  await summary.focus(); await summary.press("Enter");
  const panel = drawer.getByRole("region", { name: "검토 실행 기록 확인", exact: true });
  const selected = panel.getByRole("region", { name: `선택한 ${variant} 실행 기록`, exact: true });
  await expect(selected.getByText(/이 시도의 결과가 불명확합니다/)).toBeVisible();
  await expect(panel.getByText(/결과가 불명확한 기록이 있습니다/)).toBeVisible();
  await expect(panel.getByText(/이 화면에는 실제 실행 승인을 발급하는 기능이 없습니다/)).toBeVisible();
  await expect(panel.getByRole("button", { name: /승인|실행 시작|새 요청|철회/ })).toHaveCount(0);
  async function checkTerminalReasons() {
    if (variant !== "HTTP") return;
    for (const [status, message] of (transport === "ANTHROPIC" ? [
      ["refusal", "Provider가 이 검토 요청을 거절했습니다."],
      ["incomplete", "Provider가 응답을 끝까지 완료하지 못했다고 보고했습니다. 검토 결과는 저장되지 않았습니다."],
    ] : [
      ["failed", "Provider가 정상 완료하지 못했다고 보고했습니다."],
      ["incomplete", "Provider가 응답을 끝까지 완료하지 못했다고 보고했습니다. 검토 결과는 저장되지 않았습니다."],
      ["cancelled", "Provider가 실행 취소를 보고했습니다. 사용자가 앱에서 취소한 기록과 구분됩니다."],
    ])) {
      const reason = status === "refusal" ? "provider_refused" : `provider_${status}`;
      const open = panel.getByRole("button", { name: `HTTP 실행 기록 열기 · ${identities[status]}`, exact: true });
      await open.focus(); await open.press("Enter");
      await expect(selected.getByText(`기록 사유: ${message} (${reason})`, { exact: true })).toBeVisible();
      await expect(selected.getByText(/마지막 저장 상태: 실패/)).toBeVisible();
      await expect(selected.getByText(/이 시도의 결과가 불명확합니다/)).toHaveCount(0);
      await expect(selected.getByRole("button", { name: /저장된 HTTP 입력·결과 열기|이 HTTP 요청 취소|HTTP 수신 결과 저장 다시 시도/ })).toHaveCount(0);
      const record = await (await page.request.get(`${base}/${identities[status]}`)).json();
      expect(record.current).toMatchObject({ state: "FAILED", reason, outcome_unknown: false, receipt_id: null });
      if (status === "incomplete") {
        await selected.getByText(`기록 사유: ${message} (${reason})`, { exact: true })
          .evaluate(element => element.scrollIntoView({ block: "center" }));
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.screenshot({ path: path.join(artifacts, `review-${transport.toLowerCase()}-incomplete-${page.viewportSize()!.width}.png`) });
      }
    }
  }
  await checkTerminalReasons();
  await panel.getByRole("button", { name: `${variant} 실행 기록 열기 · ${identities.RESULT_RECEIVED}`, exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 결과 수신됨/)).toBeVisible();
  await expect(selected.getByText(/고정 입력: 26000–26010 · 10자/)).toBeVisible();
  await expect(selected.getByText(/합성 시험 자료.*FIXTURE_SYNTHETIC/)).toBeVisible();
  if (transport === "CODEX_V2") {
    await expect(selected).toContainText("공식 CLI");
    const record = await (await page.request.get(`${base}/${identities.RESULT_RECEIVED}`)).json();
    expect(record.contract.transport.schema_version).toBe("CODEX_PREPARED_REVIEW_CLI_V2");
  }
  await expect(selected.getByText(variant === "HTTP" ? /발급 주체: fabricated-http-test-issuer/ : /발급 주체: fabricated-test-issuer/)).toBeVisible();
  if (variant === "HTTP") {
    await expect(selected.getByText(transport === "ANTHROPIC" ? "요청 대상: https://api.anthropic.com/v1/messages" : "요청 대상: https://paper-provider.invalid/v1/responses", { exact: true })).toBeVisible();
    await expect(selected.getByText(/요청 reasoning/)).toHaveCount(0);
    if (transport === "ANTHROPIC") {
      await expect(selected.getByText(/Anthropic Messages API · API 버전: 2023-06-01/)).toBeVisible();
      await expect(selected.getByText(/요청 설정: 응답 저장/)).toHaveCount(0);
    }
  }
  await selected.getByText(`${variant} 고정 입력 본문 확인`, { exact: true }).click();
  await expect(selected.getByLabel(`${variant} 고정 입력 본문`, { exact: true })).toHaveText(fixture.expected);
  let lost = false;
  const sent: unknown[] = [];
  await page.route(`**${base}/${identities.RESULT_RECEIVED}/controls`, async route => {
    sent.push(route.request().postDataJSON());
    if (!lost) {
      lost = true;
      expect((await route.fetch()).status()).toBe(200);
      await route.abort("failed");
    } else await route.continue();
  });
  const save = selected.getByRole("button", { name: `${variant} 수신 결과 저장 다시 시도`, exact: true });
  await save.focus(); await save.press("Enter");
  const retry = panel.getByRole("button", { name: `같은 ${variant} 관리 요청 다시 보내기`, exact: true });
  await expect(retry).toBeVisible();
  await expect(drawer.getByLabel("시작 위치", { exact: true })).toHaveValue("26000");
  await expect(drawer.getByLabel("끝 위치 (포함하지 않음)", { exact: true })).toHaveValue("26010");
  await retry.focus(); await retry.press("Enter");
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  expect(sent).toHaveLength(2); expect(sent[1]).toEqual(sent[0]);
  const saved = await (await page.request.get(`${base}/${identities.RESULT_RECEIVED}`)).json();
  const receiptRoot = root.split("/review-preparations")[0] + "/review-inputs";
  const receipt = await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json();
  expect(receipt.input_origin).toBe("FIXTURE_SYNTHETIC");
  expect(receipt.packet).toEqual(saved.contract.prepared.packet);
  if (transport === "CODEX_V2") expect(receipt.validation.execution.schema_version).toBe("PREPARED_REVIEW_CLI_TRANSPORT_V2");
  expect((await (await page.request.get(receiptRoot)).json()).items).toHaveLength(1);
  await selected.getByRole("button", { name: `저장된 ${variant} 입력·결과 열기`, exact: true }).click();
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })).toBeVisible();
  await panel.getByRole("button", { name: `${variant} 실행 기록 열기 · ${identities.READY}`, exact: true }).click();
  await selected.getByRole("button", { name: `이 ${variant} 요청 취소`, exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 취소됨/)).toBeVisible();
  await panel.getByRole("button", { name: `${variant} 실행 기록 열기 · ${identities.RESULT_RECEIVED}`, exact: true }).click();
  await selected.getByText(`${variant} 고정 계약·상태 원문`, { exact: true }).click();
  await selected.getByText(/마지막 저장 상태: 결과 저장됨/).evaluate(element => element.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(artifacts, `review-transport-${transport.toLowerCase()}-desktop.png`) });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await selected.getByText(/마지막 저장 상태: 결과 저장됨/).evaluate(element => element.scrollIntoView({ block: "center" }));
  const bounds = await selected.boundingBox();
  expect(bounds).not.toBeNull(); expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, `review-transport-${transport.toLowerCase()}-narrow.png`) });
  const remove = spawnSync(backend.pythonExe, ["-c", `import sys
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
root=build_run_paths(sys.argv[1]).raw_dir.resolve()
target=(root/(safe_filename(sys.argv[2])+'.txt')).resolve()
assert target.is_relative_to(root)
target.unlink()
`, fixture.run, fixture.paper], { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(remove.status, remove.stderr).toBe(0);
  const before = await (await page.request.get(base)).json();
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${preparation.preparation_id}`, exact: true }).click();
  await summary.click();
  await checkTerminalReasons();
  await panel.getByRole("button", { name: `${variant} 실행 기록 열기 · ${identities.RESULT_RECEIVED}`, exact: true }).click();
  await expect(selected.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  await selected.getByText(`${variant} 고정 입력 본문 확인`, { exact: true }).click();
  await expect(selected.getByLabel(`${variant} 고정 입력 본문`, { exact: true })).toHaveText(fixture.expected);
  const after = await (await page.request.get(base)).json();
  expect(after.items.map(({ contract, events, current }: typeof saved) => ({ contract, events, current }))).toEqual(before.items.map(({ contract, events, current }: typeof saved) => ({ contract, events, current })));
  expect(after.outcome_unknown_pending).toBe(true);
  expect(await (await page.request.get(`${receiptRoot}/${saved.current.receipt_id}`)).json()).toEqual(receipt);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(page.getByRole("region", { name: `선택한 ${variant} 실행 기록`, exact: true })).toHaveCount(0);
  expect((await page.request.get(base.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  expect(consoleErrors).toEqual([{ text: expect.stringContaining("ERR_FAILED"), url: `http://127.0.0.1:8002${base}/${identities.RESULT_RECEIVED}/controls` }]);
  expect(ledger.consoleErrors).toEqual(consoleErrors.map(error => error.text));
  const expectedErrors = ledger.consoleErrors.splice(0);
  ledger.handledExpectedErrors.push(...expectedErrors); await assertHygiene(ledger);
  expect(ledger.mutationRequests).toHaveLength(4);
  expect(ledger.mutationRequests.every(value => value.includes("/review-preparations"))).toBe(true);
  fs.writeFileSync(path.join(artifacts, `review-transport-${transport.toLowerCase()}-observations.json`), JSON.stringify({ preparation, before, after, receipt, sent, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { defaultDenied: true, syntheticOnly: true, lostResponseDeduplicated: true, cancelled: true, unknownVisible: true, missingSourceReadback: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
}
