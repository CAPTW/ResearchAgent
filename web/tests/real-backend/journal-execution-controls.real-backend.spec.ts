import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test("saved execution history supports explicit expiry and fenced cancellation across restart", async ({ page, backend, context }, info) => {
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
  const seed = spawnSync(process.env.XRML_M2_PYTHON_EXE!, ["-c", `import json,sys
from datetime import datetime,timedelta,timezone
from app.domain.journal_execution import Prepare
from app.repositories import local_synthesis,journal_executions as r
topic,job_id=sys.argv[1:]
job=local_synthesis.get(topic,job_id)
now=datetime.now(timezone.utc)-timedelta(minutes=1)
ids={}
for state in ['RUNNING','LEASED','READY']:
 body=Prepare(request_id='browser-control-'+state,expected_input_digest=job['input_digest'],provider_name='synthetic',model='offline-'+state)
 saved,_=r.prepare(topic,job_id,body,'offline-fixture',now=now)
 identity=saved['contract']['execution_id']; ids[state]=identity
 if state!='READY':
  claimed=r.claim(topic,job_id,identity,expected_attempt=0,lease_seconds=5,now=now)
  if state=='RUNNING':r.start(claimed,now=now)
for state in ['WORKER_RUNNING','WORKER_LEASED']:
 body=Prepare(request_id='browser-control-'+state,expected_input_digest=job['input_digest'],provider_name='synthetic',model='offline-'+state)
 saved,_=r.prepare(topic,job_id,body,'offline-fixture',now=now)
 identity=saved['contract']['execution_id']; ids[state]=identity
 claimed=r.claim(topic,job_id,identity,expected_attempt=0,now=now)
 if state=='WORKER_RUNNING':r.start(claimed,now=now)
 r.abandon(claimed,reason='shutdown',now=now)
from tests.test_journal_dispatch import grant,BINDING,NOW,body as dispatch_body
from app.repositories import journal_dispatch
state='AUTHORITY_EXPIRED'
body=Prepare(request_id='browser-control-'+state,expected_input_digest=job['input_digest'],provider_name='synthetic',model='offline-'+state)
saved,_=r.prepare(topic,job_id,body,'offline-fixture',now=NOW)
identity=saved['contract']['execution_id']; ids[state]=identity
approved=grant(saved,expires_at=NOW+timedelta(seconds=30))
_,claimed=journal_dispatch.admit(topic,job_id,identity,dispatch_body(),'owner',approved,BINDING,now=NOW)
assert r.start(claimed,now=NOW+timedelta(seconds=31)) is None
from app.repositories import journal_authority as authority
for state in ['REVOKED_LEASED','REVOKED_RUNNING']:
 body=Prepare(request_id='browser-control-'+state,expected_input_digest=job['input_digest'],provider_name='synthetic',model='offline-'+state)
 saved,_=r.prepare(topic,job_id,body,'offline-fixture',now=NOW)
 identity=saved['contract']['execution_id']; ids[state]=identity
 authorization='browser-authority-'+state
 approved=grant(saved,authorization_id=authorization)
 authority.register(topic,job_id,identity,approved,BINDING,issuer='offline-owner',decision_ref='synthetic-owner-decision',expected_sequence=0,now=NOW)
 ref=authority.resolve(topic,job_id,identity,authorization,'owner',BINDING,now=NOW)
 _,claimed=journal_dispatch.admit(topic,job_id,identity,dispatch_body(),'owner',ref,BINDING,now=NOW)
 if state=='REVOKED_RUNNING':r.start(claimed,now=NOW)
 if state=='REVOKED_LEASED':authority.revoke(topic,job_id,identity,authorization,request_id='withdraw',issuer='offline-owner',reason='테스트용 실행 허용 철회',now=NOW)
print(json.dumps(ids))
`, topicA, job], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 20000 });
  expect(seed.status, seed.stderr).toBe(0);
  const ids = JSON.parse(seed.stdout) as Record<string, string>;
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과" });
  const record = (state: string) => results.getByRole("listitem").filter({ has: page.getByRole("heading", { name: `synthetic · offline-${state}`, exact: true }) });
  await expect(results.getByText(/저장된 실행 기록 8개/)).toBeVisible();
  const before = await (await page.request.get(`${root}/journal-executions/${ids.RUNNING}`)).json();
  await record("RUNNING").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  const history = results.getByRole("region", { name: "저널 실행 이력과 제어" });
  await expect(history.getByText(/최근 저장 상태: 응답 대기 기록 · 시도 1/)).toBeVisible();
  await expect(history.getByRole("heading", { name: "실행 이력과 상태 관리" })).toBeFocused();
  expect(await (await page.request.get(`${root}/journal-executions/${ids.RUNNING}`)).json()).toEqual(before);
  await history.getByRole("button", { name: "접수 기한 만료 확인" }).click();
  await expect(history.getByText(/최근 저장 상태: 응답 확인 불가 · 시도 1/)).toBeVisible();
  await expect(history.getByText("기한 만료 확인 요청 사용자: local-internal-user")).toBeVisible();
  await expect(history.getByText(/외부 호출의 중단·비용 취소를 보장하지 않습니다/)).toBeVisible();
  await history.getByRole("button", { name: "이 요청의 결과 저장 취소" }).click();
  await expect(history.getByText(/최근 저장 상태: 결과 저장 취소 · 시도 1/)).toBeVisible();
  await expect(history.getByText("결과 저장 취소 요청 사용자: local-internal-user")).toBeVisible();
  expect(await history.getByRole("button", { name: "이 요청의 결과 저장 취소" }).count()).toBe(0);
  const cancelled = await (await page.request.get(`${root}/journal-executions/${ids.RUNNING}`)).json();
  expect(cancelled.current.outcome_unknown).toBe(true);
  expect(cancelled.current.result).toBeNull();
  expect(cancelled.events.map((event: { state: string }) => event.state)).toEqual(["LEASED", "RUNNING", "INTERRUPTED", "CANCELLED"]);
  await record("LEASED").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText(/최근 저장 상태: 호출 선점 기록/)).toBeVisible();
  await history.getByRole("button", { name: "접수 기한 만료 확인" }).click();
  await expect(history.getByText(/최근 저장 상태: 실패 · 시도 1/)).toBeVisible();
  await expect(history.getByText("호출 전 결과 접수 기한 만료")).toBeVisible();
  const expired = await (await page.request.get(`${root}/journal-executions/${ids.LEASED}`)).json();
  expect(expired.current.outcome_unknown).toBe(false);
  await record("READY").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText(/최근 저장 상태: 입력 준비 기록 · 시도 0/)).toBeVisible();
  await history.getByRole("button", { name: "이 요청의 결과 저장 취소" }).click();
  await expect(history.getByText(/최근 저장 상태: 결과 저장 취소 · 시도 0/)).toBeVisible();
  const readyCancelled = await (await page.request.get(`${root}/journal-executions/${ids.READY}`)).json();
  for (const [state, reason] of [["WORKER_RUNNING", "호출 후 실행 처리가 종료되어 응답 확인 불가"], ["WORKER_LEASED", "호출 전에 실행 처리가 종료됨"]]) {
    await record(state).getByRole("button", { name: "실행 이력·제어 열기" }).click();
    await expect(history.getByText(reason, { exact: true })).toBeVisible();
  }
  const workerStopped = await (await page.request.get(`${root}/journal-executions/${ids.WORKER_RUNNING}`)).json();
  expect(workerStopped.current.state).toBe("INTERRUPTED");
  expect(workerStopped.current.outcome_unknown).toBe(true);
  const admissionExpired = await (await page.request.get(`${root}/journal-executions/${ids.AUTHORITY_EXPIRED}`)).json();
  expect(admissionExpired.current.state).toBe("FAILED");
  expect(admissionExpired.current.outcome_unknown).toBe(false);
  // Withdraw the second synthetic allowance through the actual authenticated UI.
  await record("REVOKED_RUNNING").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await history.getByText("실행 허용·예약 기록", { exact: true }).click();
  await history.getByRole("button", { name: "허용 출처 확인" }).click();
  const withdrawalPanel = history.getByRole("region", { name: "저장된 실행 허용 출처" });
  await withdrawalPanel.getByRole("textbox", { name: "실행 허용 철회 사유" }).fill("테스트용 실행 허용 철회");
  await withdrawalPanel.evaluate(el => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, "authority-withdrawal-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await withdrawalPanel.getByRole("textbox").evaluate(el => el.scrollIntoView({ block: "center" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, "authority-withdrawal-narrow.png") });
  await withdrawalPanel.getByRole("button", { name: "이 실행 허용 철회", exact: true }).click();
  await expect(withdrawalPanel.getByText("실행 허용 철회를 기록했습니다.", { exact: true })).toBeVisible();
  await expect(history.getByText(/최근 저장 상태: 응답 확인 불가/)).toBeVisible();
  await expect(withdrawalPanel.getByRole("textbox")).toHaveCount(0);
  await page.setViewportSize({ width: 1536, height: 1024 });
  const revokedBefore = await (await page.request.get(`${root}/journal-executions/${ids.REVOKED_LEASED}`)).json();
  const revokedAfter = await (await page.request.get(`${root}/journal-executions/${ids.REVOKED_RUNNING}`)).json();
  expect(revokedBefore.current.state).toBe("FAILED");
  expect(revokedBefore.current.outcome_unknown).toBe(false);
  expect(revokedAfter.current.state).toBe("INTERRUPTED");
  expect(revokedAfter.current.outcome_unknown).toBe(true);
  const ref = revokedAfter.events[0].dispatch.authority;
  const authorityUrl = `${root}/journal-executions/${ids.REVOKED_RUNNING}/authority?authorization_id=${encodeURIComponent(ref.authorization_id)}&expected_record_digest=${ref.record_digest}`;
  const authorityRecord = await (await page.request.get(authorityUrl)).json();
  expect(authorityRecord.record.decision_ref).toBe("synthetic-owner-decision");
  expect(authorityRecord.revocation.reason).toBe("테스트용 실행 허용 철회");
  expect(authorityRecord.revocation.actor_id).toBe("local-internal-user");
  const replay = await page.request.post(`${root}/journal-executions/${ids.REVOKED_RUNNING}/authority/revoke`, { data: {
    authorization_id: ref.authorization_id, expected_record_digest: ref.record_digest,
    request_id: authorityRecord.revocation.request_id, reason: authorityRecord.revocation.reason,
  } });
  expect(replay.ok()).toBe(true);
  expect((await replay.json()).applied).toBe(false);
  expect(await (await page.request.get(`${root}/journal-executions/${ids.REVOKED_RUNNING}`)).json()).toEqual(revokedAfter);
  await page.goto("about:blank"); await backend.restart();
  for (const [state, value] of [["RUNNING", cancelled], ["LEASED", expired], ["READY", readyCancelled], ["WORKER_RUNNING", workerStopped], ["AUTHORITY_EXPIRED", admissionExpired], ["REVOKED_LEASED", revokedBefore], ["REVOKED_RUNNING", revokedAfter]] as const) {
    expect(await (await page.request.get(`${root}/journal-executions/${ids[state]}`)).json()).toEqual(value);
  }
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job)}`);
  await page.getByText("저장된 생성 결과", { exact: true }).click();
  await record("RUNNING").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText(/최근 저장 상태: 결과 저장 취소/)).toBeVisible();
  await expect(history.getByText("기한 만료 확인 요청 사용자: local-internal-user")).toBeVisible();
  await expect(page.getByText("주제 데이터를 불러오는 중입니다.", { exact: true })).not.toBeVisible();
  await record("WORKER_RUNNING").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText("호출 후 실행 처리가 종료되어 응답 확인 불가", { exact: true })).toBeVisible();
  await expect(history.getByText(/최근 저장 상태: 응답 확인 불가/)).toBeVisible();
  await record("AUTHORITY_EXPIRED").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText("호출 전에 실행 허용 기한이 만료됨", { exact: true })).toBeVisible();
  await expect(history.getByText("실행 요청 사용자: owner", { exact: true })).toBeVisible();
  await history.getByText("실행 허용·예약 기록", { exact: true }).click();
  await expect(history.getByText("이 시도의 출력 토큰 예약 상한: 4000", { exact: true })).toBeVisible();
  await expect(history.getByText(/예약 상한은 실제 토큰 사용량·요금·모델 실행 증명이 아닙니다/)).toBeVisible();
  expect(await (await page.request.get(authorityUrl)).json()).toEqual(authorityRecord);
  await record("REVOKED_LEASED").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText("호출 전에 실행 허용이 철회됨", { exact: true })).toBeVisible();
  await record("REVOKED_RUNNING").getByRole("button", { name: "실행 이력·제어 열기" }).click();
  await expect(history.getByText("호출 선점 후 실행 허용이 철회되어 응답 확인 불가", { exact: true })).toBeVisible();
  await expect(history.getByText("실행 허용 철회 사용자: local-internal-user", { exact: true })).toBeVisible();
  await history.getByText("실행 허용·예약 기록", { exact: true }).click();
  await history.getByRole("button", { name: "허용 출처 확인" }).click();
  const authorityPanel = history.getByRole("region", { name: "저장된 실행 허용 출처" });
  await expect(authorityPanel.getByText("승인 출처 식별자: synthetic-owner-decision", { exact: true })).toBeVisible();
  await expect(authorityPanel.getByText("철회 사유: 테스트용 실행 허용 철회", { exact: true })).toBeVisible();
  await expect(authorityPanel.getByText(/별도의 승인 진위·실제 모델 실행 증명은 아닙니다/)).toBeVisible();
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  await history.getByRole("heading", { name: "실행 이력과 상태 관리" }).evaluate(el => el.scrollIntoView({ block: "start" }));
  expect((await history.getByRole("heading", { name: "실행 이력과 상태 관리" }).boundingBox())!.y).toBeGreaterThanOrEqual(62);
  await page.screenshot({ path: path.join(artifacts, "execution-controls-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await history.getByRole("heading", { name: "실행 이력과 상태 관리" }).evaluate(el => el.scrollIntoView({ block: "start" }));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "execution-controls-narrow.png") });
  await authorityPanel.evaluate(el => el.scrollIntoView({ block: "center" }));
  await expect(authorityPanel.getByText("철회 사유: 테스트용 실행 허용 철회", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "authority-source-narrow.png") });
  await assertHygiene(ledger);
  expect(ledger.mutationRequests.every(value => value.includes("/local-synthesis"))).toBe(true);
  expect(fs.readdirSync(path.join(backend.dataRoot, "runs")).sort()).toEqual(runs);
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { explicitRecovery: true, cancellation: true, serverActor: true, readOnlyHistory: true, restartPreserved: true, automaticRuns: 0, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
