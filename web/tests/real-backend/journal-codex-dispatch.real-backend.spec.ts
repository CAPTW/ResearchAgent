import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test.use({ codexDispatchFixture: true });

test("Codex V2 app dispatch replays a lost response and retains reviewed output after restart", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
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
  const input = await (await page.request.get(root)).json();
  const response = await page.request.post(`${root}/journal-executions`, { data: {
    request_id: "app-dispatch-fixture", expected_input_digest: input.input_digest,
    provider_name: "codex_cli", model: "offline-requested-model", max_attempts: 2, max_output_tokens: 1024,
  } });
  expect(response.status()).toBe(201);
  const saved = (await response.json()).execution;
  const url = `${root}/journal-executions/${saved.contract.execution_id}`;
  // Trusted authority is a TEST fixture registered out of band. No browser
  // approval flag, product issuer endpoint, actual Codex or account is involved.
  const seed = spawnSync(backend.pythonExe, ["-c", `import json,sys
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from app.repositories import journal_authority as a,journal_executions as r
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
from tests.test_codex_journal import approval,runtime_at
topic,job,execution,root=sys.argv[1:]; saved=r.detail(topic,job,execution); now=datetime.now(timezone.utc)
provider=CodexJournalV2Provider(saved['contract'],runtime=runtime_at(Path(root)),reasoning_effort='low',timeout_seconds=30)
grant=approval(saved,provider); grant=grant.model_copy(update=dict(actor_id=saved['contract']['actor_id'],granted_at=now,expires_at=now+timedelta(minutes=2),proof=replace(grant.proof,accepted_at=now,expires_at=now+timedelta(minutes=5))))
record,_=a.register(topic,job,execution,grant,provider.binding,issuer='fixture-only-issuer',decision_ref='synthetic Python only; no scientific evaluation',expected_sequence=0,now=now)
print(json.dumps(record))
`, topicA, job, saved.contract.execution_id, path.join(backend.dataRoot, "fake-codex-app")], {
    cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 25000,
  });
  expect(seed.status, seed.stderr).toBe(0);
  const authorization = JSON.parse(seed.stdout).grant.authorization_id;
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  const dispatch = history.getByRole("region", { name: "서버 허용 Codex 실행" });
  const inspect = dispatch.getByRole("button", { name: "Codex 실행 허용 확인·새로고침" });
  await inspect.focus(); await inspect.press("Enter");
  await expect(dispatch.getByText(/현재 서버가 실행에 사용할 전송 조건/)).toBeVisible();
  await dispatch.getByLabel("서버 실행 허용 선택").selectOption(authorization);
  expect(await (await page.request.get(url)).json()).toEqual(saved);
  let firstBody: unknown;
  await page.route(`**${url}/codex-dispatch`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    firstBody = route.request().postDataJSON();
    await route.fetch(); await route.abort("failed");
  });
  await dispatch.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }).click();
  await expect(dispatch.getByRole("alert")).toContainText("Codex 실행 응답을 확인하지 못했습니다");
  await expect(dispatch.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toBeEnabled();
  await page.unroute(`**${url}/codex-dispatch`);
  await history.getByRole("button", { name: "실행 이력 새로고침" }).click();
  await expect(dispatch.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toBeVisible();
  await expect.poll(async () => (await (await page.request.get(url)).json()).current.state).toBe("SUCCEEDED");
  const request = page.waitForRequest(r => r.url().endsWith(`${url}/codex-dispatch`) && r.method() === "POST");
  await dispatch.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" }).click();
  expect((await request).postDataJSON()).toEqual(firstBody);
  await expect(dispatch.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toHaveCount(0);
  expect(fs.readFileSync(path.join(backend.dataRoot, "fake-codex-app", "launches.log"), "utf8").trim().split(/\r?\n/)).toEqual(["launch"]);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await dispatch.getByRole("heading").scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "codex-app-dispatch-narrow.png") });
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보" });
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집" });
  await expect(editor.getByRole("button", { name: "저널로 채택", exact: true })).toBeDisabled();
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("합성 Python 응답의 연결 시험. 실제 모델 또는 과학적 검토 아님.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const review = candidate.getByRole("article", { name: "보존된 저널 검토 기록" });
  const download = page.waitForEvent("download");
  await review.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click();
  const output = path.join(artifacts, "codex-app-dispatch-export.json");
  await (await download).saveAs(output);
  const bundle = JSON.parse(fs.readFileSync(output, "utf8"));
  expect(bundle.candidate.execution.adapter_manifest.schema_version).toBe("CODEX_JOURNAL_CLI_V2");
  expect(bundle.candidate.validation.semantic_support).toBe("NOT_ASSESSED");
  const finished = await (await page.request.get(url)).json();
  expect(finished.current.attempt).toBe(1);
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(url)).json()).toEqual(finished);
  expect((await (await page.request.post(`${url}/codex-dispatch`, { data: firstBody })).json()).execution).toEqual(finished);
  const readback = await (await page.request.get(`${root}/journal-candidates/${bundle.candidate.candidate_id}/reviews/${bundle.review.review_id}/export?format=json`)).json();
  expect(JSON.parse(readback.content)).toEqual(bundle);
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job)}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText(/시도 1 \/ 최대 2회/)).toBeVisible();
  expect(fs.readFileSync(path.join(backend.dataRoot, "fake-codex-app", "launches.log"), "utf8").trim().split(/\r?\n/)).toEqual(["launch"]);
  const expected = ledger.consoleErrors.filter(value => value.includes("net::ERR_FAILED"));
  expect(expected).toHaveLength(1);
  ledger.handledExpectedErrors.push(...expected);
  ledger.consoleErrors = ledger.consoleErrors.filter(value => !value.includes("net::ERR_FAILED"));
  await assertHygiene(ledger);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { syntheticOnly: true, actualAppDispatch: true,
    lostResponseSingleProcess: true, reviewedExportRestartEqual: true, passed: info.status === "passed" }, timing: {},
    hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
