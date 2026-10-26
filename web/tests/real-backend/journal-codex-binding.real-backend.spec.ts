import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

for (const profile of ["V1", "V2"]) test(`fake Codex ${profile} result retains its command conditions through human review and restart`, async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
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
  // Explicit test fixture, not a new application issuer or a real Codex call.
  const seed = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `import json,sys,time
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from app.domain.journal_execution import Prepare
from app.repositories import local_synthesis,journal_executions as r,journal_authority as a
from app.services.providers.codex_journal import journal_dispatcher
from app.services.journal_dispatch import StoredJournalAuthority
from tests.test_codex_journal import adapter,approval,runtime_at
from tests.test_journal_dispatch import body
topic,job_id,workspace,profile=sys.argv[1:]; job=local_synthesis.get(topic,job_id); now=datetime.now(timezone.utc)
execution,_=r.prepare(topic,job_id,Prepare(request_id='cli-fixture',expected_input_digest=job['input_digest'],provider_name='codex_cli',model='offline-requested-model'),'offline-fixture',now=now)
scope=(topic,job_id,execution['contract']['execution_id'])
if profile=='V2':
 from app.services.providers.codex_journal_v2 import CodexJournalV2Provider,journal_dispatcher
 provider=CodexJournalV2Provider(execution['contract'],runtime=runtime_at(Path(workspace)),reasoning_effort='low',timeout_seconds=30)
else: provider=adapter(execution,Path(workspace))
grant=approval(execution,provider); grant=grant.model_copy(update=dict(granted_at=now,expires_at=now+timedelta(minutes=2),proof=replace(grant.proof,accepted_at=now,expires_at=now+timedelta(minutes=5))))
a.register(*scope,grant,provider.binding,issuer='synthetic-cli-fixture',decision_ref='fake-python-executable-only',expected_sequence=0,now=now)
controller=journal_dispatcher(provider,authority=StoredJournalAuthority(grant.authorization_id,clock=lambda:now),clock=lambda:now)
try:
 controller.dispatch(*scope,body(),'owner'); deadline=time.monotonic()+10
 while r.detail(*scope)['current']['state'] in ['READY','LEASED','RUNNING'] and time.monotonic()<deadline: time.sleep(.01)
 saved=r.detail(*scope); assert saved['current']['state']=='SUCCEEDED',saved
 assert controller.dispatch(*scope,body(),'owner')==saved
finally: assert controller.stop()
assert (Path(workspace)/'launches.log').read_text().splitlines()==['launch']
print(json.dumps(saved))
`, topicA, job, path.join(backend.dataRoot, "fake-cli"), profile], {
    cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot },
    encoding: "utf8", windowsHide: true, timeout: 25000,
  });
  expect(seed.status, seed.stderr).toBe(0);
  const saved = JSON.parse(seed.stdout);
  const url = `${root}/journal-executions/${saved.contract.execution_id}`;
  const manifest = saved.events[0].adapter_manifest;
  expect(manifest.schema_version).toBe(`CODEX_JOURNAL_CLI_${profile}`);
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  await expect(results.getByText(/어댑터 보고 모델: codex_cli · 실제 보고 모델 미확인/)).toBeVisible();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  await history.getByText("고정된 전송 조건", { exact: true }).focus();
  await history.getByText("고정된 전송 조건", { exact: true }).press("Enter");
  await expect(history.getByText(/입력 SHA-256:/)).toContainText(manifest.stdin_sha256);
  await expect(history.getByText(/요청 모델: offline-requested-model/)).toBeVisible();
  await expect(history.getByText(/출력 토큰.*강제/)).toBeVisible();
  await expect(history.getByText(/요청 주소:/)).toHaveCount(0);
  await history.getByText("요청 내용·토큰 사용 기록", { exact: true }).click();
  const usage = history.getByRole("region", { name: "시도 1 토큰 기록" });
  await expect(usage.getByText("보고된 합계 토큰").locator("+ dd")).toHaveText("미보고");
  await history.getByText("보존된 어댑터 입력 보기", { exact: true }).click();
  const input = history.getByLabel("보존된 문헌 입력");
  expect(await input.textContent()).toBe(saved.contract.provider_request.prompt);
  await input.focus(); await expect(input).toBeFocused();
  await history.getByText("고정된 전송 조건", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, `codex-${profile.toLowerCase()}-binding-wide.png`) });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await history.getByText("고정된 전송 조건", { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, `codex-${profile.toLowerCase()}-binding-narrow.png`) });
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보" });
  await expect(candidate.getByText("어댑터가 보고한 Provider·모델: codex_cli · 실제 보고 모델 미확인", { exact: true })).toBeVisible();
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집" });
  await expect(editor.getByRole("button", { name: "저널로 채택", exact: true })).toBeDisabled();
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("Fake Python CLI 연결 시험. 실제 모델·과학적 뒷받침은 평가하지 않음.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const review = candidate.getByRole("article", { name: "보존된 저널 검토 기록" });
  const [download] = await Promise.all([page.waitForEvent("download"), review.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click()]);
  const exportFile = path.join(artifacts, `codex-${profile.toLowerCase()}-fixture-reviewed-journal.json`);
  await download.saveAs(exportFile);
  const bundle = JSON.parse(fs.readFileSync(exportFile, "utf8"));
  expect(bundle.candidate.validation.semantic_support).toBe("NOT_ASSESSED");
  if (profile === "V2") {
    expect(bundle.candidate.execution.adapter_manifest).toEqual(manifest);
    expect(bundle.candidate.execution.adapter_manifest.raw_envelope_retained).toBe(false);
    await expect(candidate).toContainText("전체");
  }
  await page.goto(`/app/topics/${topicB}/reports`);
  expect((await page.request.get(url.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  await page.goto("about:blank"); await backend.restart();
  expect(await (await page.request.get(url)).json()).toEqual(saved);
  const readback = await (await page.request.get(`${root}/journal-candidates/${bundle.candidate.candidate_id}/reviews/${bundle.review.review_id}/export?format=json`)).json();
  expect(JSON.parse(readback.content)).toEqual(bundle);
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job)}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await results.getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await history.getByText("고정된 전송 조건", { exact: true }).click();
  await expect(history.getByText(/입력 SHA-256:/)).toContainText(manifest.stdin_sha256);
  await assertHygiene(ledger);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { fakeProcessOnly: true, cliManifestReadback: true, humanAcceptance: true, restartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
