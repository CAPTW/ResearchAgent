import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, appOrigin, topicA, topicB, approvalFixtureSessionSecret, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

test.use({ codexApprovalFixture: true });

test("authenticated synthetic owner registers paper and Journal approval without implicit execution", async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  const ledger = await installLedger(context, page);
  const consoleErrors: { text: string; url: string }[] = [];
  page.on("console", message => { if (message.type() === "error") consoleErrors.push({ text: message.text(), url: message.location().url }); });
  const artifacts = process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!;
  const fixtureEnv = { ...process.env, XRML_DATA_DIR: backend.dataRoot, TEMP: backend.tempRoot, TMP: backend.tempRoot,
    TMPDIR: backend.tempRoot, AUTH_MODE: "oidc", SESSION_SECRET: approvalFixtureSessionSecret,
    XRML_CODEX_APPROVAL_FIXTURE: "1", XRML_ENVIRONMENT: "development", XRML_SHARED_DEPLOYMENT: "0",
    APP_BASE_URL: appOrigin, COOKIE_SECURE: "0", TOPIC_SCHEDULER_ENABLED: "0", XRML_RUN_EXECUTOR_MODE: "disabled",
    OIDC_ISSUER: "", OIDC_CLIENT_ID: "", OIDC_CLIENT_SECRET: "", OIDC_AUDIENCE: "", ADMIN_USER_IDS: "", ADMIN_USER_EMAILS: "" };
  function fixture(script: string, ...args: string[]) {
    const result = spawnSync(backend.pythonExe, ["-c", `import json,sys\nfrom tests.helpers import codex_approval_app_fixture as f\nf.fixture_root()\n${script}`, ...args],
      { cwd: backend.sourceRoot, env: fixtureEnv, encoding: "utf8", windowsHide: true, timeout: 30000 });
    expect(result.status, result.stderr).toBe(0);
    return JSON.parse(result.stdout);
  }
  const cookie = fixture("print(json.dumps(f.session_cookie()))");
  await context.addCookies([{ ...cookie, url: appOrigin, httpOnly: true, secure: false, sameSite: "Lax" }]);
  const auth = await (await page.request.get("/auth/me")).json();
  expect(auth).toMatchObject({ auth_mode: "oidc", authenticated: true, is_admin: false, product_session_backend: "server_side_file",
    user: { user_id: "local-internal-user", auth_source: "oidc" } });

  const bank = await (await page.request.get(`/api/topics/${topicA}/paper-bank?pageSize=100`)).json();
  const paper = bank.items[0], occurrence = paper.compatibility.occurrences[0];
  const source = fixture(`from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from app.services import paper_review
from tests.test_ai_review_provider import _review
run,paper,topic,title=sys.argv[1:]
body='e\\u0301 한글🙂 synthetic methods and limitations '*100
artifact=build_run_paths(run).raw_dir/(safe_filename(paper)+'.txt')
artifact.write_bytes(body.encode('utf-8'))
review=_review(1,topic_id=topic,run_id=run,paper_id=paper,title=title)
review.update(paper_review.read_local_full_text_evidence(run_id=run,paper_id=paper,max_text_chars=24000))
paper_reviews.upsert_review(review)
print(json.dumps(dict(expected=body[:10]),ensure_ascii=False))`, occurrence.runId, occurrence.paperId, topicA, paper.title);
  const preparationRoot = `/api/topics/${topicA}/paper-bank/${paper.paperVersionId}/review-preparations`;
  const sources = await (await page.request.get(`${preparationRoot}/sources`)).json();
  const selectedSource = sources.sources.find((value: { run_id: string; paper_id: string }) => value.run_id === occurrence.runId && value.paper_id === occurrence.paperId);
  expect(selectedSource).toBeTruthy();
  const fixedResponse = await page.request.post(preparationRoot, { data: { request_id: "approval-browser-range", run_id: selectedSource.run_id,
    paper_id: selectedSource.paper_id, expected_source_digest: selectedSource.source_digest, start: 0, end: 10 } });
  expect(fixedResponse.status()).toBe(200);
  const preparation = await fixedResponse.json();
  const paperBase = `${preparationRoot}/${preparation.preparation_id}/transport-executions`;
  const preview = await (await page.request.get(`${paperBase}/codex-preview`)).json();
  const preparedResponse = await page.request.post(`${paperBase}/codex-requests`, { data: { request_id: "approval-browser-paper", acknowledge_unknown: false,
    expected_preparation_digest: preview.preparation_digest, expected_binding_sha256: preview.binding_sha256 } });
  expect(preparedResponse.status()).toBe(200);
  const ready = await preparedResponse.json(), paperUrl = `${paperBase}/${ready.contract.execution_id}`;
  fixture("print(json.dumps(f.seed_paper_offer(*sys.argv[1:])))", topicA, paper.paperVersionId, preparation.preparation_id, ready.contract.execution_id);
  const paperSourcePath = path.join(backend.dataRoot, "approval-source-paper.json");
  const paperSourceBytes = fs.readFileSync(paperSourcePath);
  const paperLaunches = path.join(backend.dataRoot, "fake-paper-codex", "launches.log");
  const journalLaunches = path.join(backend.dataRoot, "fake-codex-app", "launches.log");
  const noLaunch = () => { expect(fs.existsSync(paperLaunches)).toBe(false); expect(fs.existsSync(journalLaunches)).toBe(false); };
  const drawer = page.getByRole("region", { name: "논문 상세", exact: true });
  const paperHistory = drawer.getByRole("region", { name: "검토 실행 기록 확인", exact: true });
  const selectedPaper = paperHistory.getByRole("region", { name: "선택한 CLI 실행 기록", exact: true });
  const paperApproval = paperHistory.getByRole("region", { name: "논문 서버 승인 등록", exact: true });
  const paperCodex = paperHistory.getByRole("region", { name: "서버 연결 Codex 논문 검토", exact: true });
  async function openPaper() {
    await page.goto(`/app/topics/${topicA}/papers?paper=${paper.paperVersionId}`);
    await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
    await drawer.getByRole("button", { name: `준비 기록 열기 · ${preparation.preparation_id}`, exact: true }).click();
    await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(source.expected);
    await drawer.getByText("검토 실행 조건·이력", { exact: true }).click();
  }
  await openPaper();
  await expect(selectedPaper.getByText(/마지막 저장 상태: 요청 준비됨/)).toBeVisible();
  const paperInspect = paperApproval.getByRole("button", { name: "선택한 논문 요청의 승인 등록 조건 확인", exact: true });
  await paperInspect.focus(); await expect(paperInspect).toBeFocused(); await paperInspect.press("Enter");
  const paperRegister = paperApproval.getByRole("button", { name: "확인한 논문 승인 기록 등록", exact: true });
  await expect(paperRegister).toBeEnabled();
  noLaunch();
  // Change only a disposable source offer after preview. The real endpoint must
  // reject the stale expected hash before creating an APPROVED event.
  const changed = JSON.parse(paperSourceBytes.toString("utf8"));
  changed[ready.contract.execution_id].decision_ref += "-changed";
  fs.writeFileSync(paperSourcePath, JSON.stringify(changed), "utf8");
  const beforeDrift = await (await page.request.get(paperUrl)).json();
  const metadataBeforeDrift = fixture("print(json.dumps(f.metadata_digest()))");
  const rejected = page.waitForResponse(response => response.url().endsWith(`${paperUrl}/codex-approval`) && response.request().method() === "POST");
  await paperRegister.click(); expect((await rejected).status()).toBe(409);
  await expect(paperApproval.getByRole("alert")).toContainText("거절");
  expect(await (await page.request.get(paperUrl)).json()).toEqual(beforeDrift); noLaunch();
  expect(fixture("print(json.dumps(f.metadata_digest()))")).toBe(metadataBeforeDrift);
  fs.writeFileSync(paperSourcePath, paperSourceBytes);
  await paperInspect.click(); await expect(paperRegister).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await paperRegister.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "paper-approval-narrow.png") });
  let paperBody: unknown;
  await page.route(`**${paperUrl}/codex-approval`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    paperBody = route.request().postDataJSON();
    expect((await route.fetch()).status()).toBe(200); await route.abort("failed");
  });
  await paperRegister.focus(); await expect(paperRegister).toBeFocused(); await paperRegister.press("Enter");
  await expect(paperApproval.getByRole("alert")).toContainText("응답");
  await page.unroute(`**${paperUrl}/codex-approval`);
  await paperHistory.getByRole("button", { name: "검토 실행 이력 새로고침", exact: true }).click();
  await expect(selectedPaper.getByText(/마지막 저장 상태: 승인 기록됨/)).toBeVisible();
  fs.unlinkSync(paperSourcePath);
  const paperReplay = page.waitForRequest(request => request.url().endsWith(`${paperUrl}/codex-approval`) && request.method() === "POST");
  const paperReplayResponse = page.waitForResponse(response => response.url().endsWith(`${paperUrl}/codex-approval`) && response.request().method() === "POST");
  await paperApproval.getByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인", exact: true }).click();
  expect((await paperReplay).postDataJSON()).toEqual(paperBody);
  const paperApprovalSaved = await (await paperReplayResponse).json();
  await expect(paperApproval.getByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인", exact: true })).toHaveCount(0);
  const approvedPaper = await (await page.request.get(paperUrl)).json();
  expect(approvedPaper.events.map((event: { state: string }) => event.state)).toEqual(["READY", "APPROVED"]); noLaunch();

  // API setup freezes a separate Journal input; the UI performs registration.
  const currentBank = await (await page.request.get(`/api/topics/${topicA}/paper-bank?pageSize=100`)).json();
  const journalPaper = currentBank.items.find((item: { paperVersionId: string }) => item.paperVersionId === paper.paperVersionId);
  const captured = await page.request.post(`/api/topics/${topicA}/local-synthesis`, { data: { request_id: "approval-browser-journal-input",
    selection: [{ paper_version_id: journalPaper.paperVersionId, metadata_digest: journalPaper.metadataDigest }] } });
  expect(captured.status()).toBe(201);
  const job = (await captured.json()).job, journalRoot = `/api/topics/${topicA}/local-synthesis/${job.job_id}`;
  for (let step = 0; step < job.total_steps; step++) expect((await page.request.post(`${journalRoot}/step`, { data: { expected_step: step } })).status()).toBe(200);
  const frozenInput = await (await page.request.get(journalRoot)).json();
  const journalPrepared = await page.request.post(`${journalRoot}/journal-executions`, { data: { request_id: "approval-browser-journal",
    expected_input_digest: frozenInput.input_digest, provider_name: "codex_cli", model: "offline-requested-model", max_attempts: 2, max_output_tokens: 1024 } });
  expect(journalPrepared.status()).toBe(201);
  const journalReady = (await journalPrepared.json()).execution, journalUrl = `${journalRoot}/journal-executions/${journalReady.contract.execution_id}`;
  const composedPreview = await (await page.request.get(`${journalUrl}/codex-preview`)).json();
  const composedDispatch = await (await page.request.get(`${journalUrl}/codex-dispatch`)).json();
  expect(composedPreview.available).toBe(true);
  expect(composedDispatch.enabled).toBe(true);
  expect(composedPreview.manifest.schema_version).toBe("CODEX_JOURNAL_CLI_V2");
  expect(composedPreview.manifest).toEqual(composedDispatch.manifest);
  expect(composedPreview.binding).toEqual(composedDispatch.binding);
  expect(composedPreview.manifest.runtime_identity).toEqual(preview.manifest.runtime_identity);
  expect(composedDispatch.authorities).toEqual([]);
  fixture("print(json.dumps(f.seed_journal_offer(*sys.argv[1:])))", topicA, job.job_id, journalReady.contract.execution_id);
  const results = page.getByRole("region", { name: "저장된 저널 생성 결과", exact: true });
  const journalHistory = results.getByRole("region", { name: "저널 실행 이력과 제어", exact: true });
  const journalApproval = journalHistory.getByRole("region", { name: "Journal 승인 등록", exact: true });
  async function openJournal() {
    await page.goto(`/app/topics/${topicA}/reports?synthesis=${encodeURIComponent(job.job_id)}`);
    await page.getByText("저장된 생성 결과", { exact: true }).click();
    await results.getByRole("button", { name: "실행 이력·제어 열기", exact: true }).click();
  }
  await openJournal();
  const journalInspect = journalApproval.getByRole("button", { name: "Journal 승인 후보 확인", exact: true });
  await expect(journalInspect).toBeEnabled(); await journalInspect.focus(); await expect(journalInspect).toBeFocused(); await journalInspect.press("Enter");
  const journalRegister = journalApproval.getByRole("button", { name: "확인한 Journal 승인 등록", exact: true });
  await expect(journalRegister).toBeEnabled();
  await journalRegister.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "journal-approval-narrow.png") });
  let journalBody: unknown;
  await page.route(`**${journalUrl}/codex-approval`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    journalBody = route.request().postDataJSON();
    expect((await route.fetch()).status()).toBe(200); await route.abort("failed");
  });
  await journalRegister.click(); await expect(journalApproval.getByRole("alert")).toContainText("응답");
  await page.unroute(`**${journalUrl}/codex-approval`);
  await journalHistory.getByRole("button", { name: "실행 이력 새로고침", exact: true }).click();
  const journalRetry = journalApproval.getByRole("button", { name: "같은 Journal 승인 등록 다시 확인", exact: true });
  await expect(journalRetry).toBeEnabled();
  fs.unlinkSync(path.join(backend.dataRoot, "approval-source-journal.json"));
  const journalReplay = page.waitForRequest(request => request.url().endsWith(`${journalUrl}/codex-approval`) && request.method() === "POST");
  await journalRetry.click(); expect((await journalReplay).postDataJSON()).toEqual(journalBody); await expect(journalRetry).toHaveCount(0);
  const authority = await (await page.request.get(`${journalUrl}/authority?authorization_id=${encodeURIComponent((journalBody as { authorization_id: string }).authorization_id)}&expected_record_digest=${(journalBody as { expected_record_digest: string }).expected_record_digest}`)).json();
  expect(await (await page.request.get(journalUrl)).json()).toEqual(journalReady); noLaunch();

  await page.goto("about:blank"); await backend.restart();
  expect((await (await page.request.get("/auth/me")).json()).authenticated).toBe(true);
  expect(fs.existsSync(paperSourcePath)).toBe(false);
  expect(fs.existsSync(path.join(backend.dataRoot, "approval-source-journal.json"))).toBe(false);
  expect(await (await page.request.post(`${paperUrl}/codex-approval`, { data: paperBody })).json()).toEqual(paperApprovalSaved);
  expect(await (await page.request.get(paperUrl)).json()).toEqual(approvedPaper);
  const restartedJournal = await (await page.request.post(`${journalUrl}/codex-approval`, { data: journalBody })).json();
  expect(restartedJournal.execution).toEqual(journalReady); expect(restartedJournal.authority).toEqual(authority); noLaunch();
  expect((await (await page.request.get(`${journalUrl}/codex-preview`)).json()).manifest).toEqual(composedPreview.manifest);

  // Only separate explicit execution controls may launch the fake processes.
  await openPaper(); await expect(selectedPaper.getByText(/마지막 저장 상태: 승인 기록됨/)).toBeVisible();
  const paperDispatchInspect = paperCodex.getByRole("button", { name: "선택한 Codex 요청의 서버 허용 확인", exact: true });
  await expect(paperDispatchInspect).toBeEnabled(); await paperDispatchInspect.focus(); await expect(paperDispatchInspect).toBeFocused(); await paperDispatchInspect.press("Enter");
  await paperCodex.getByRole("button", { name: "서버가 허용한 Codex 검토 실행", exact: true }).click();
  await expect(selectedPaper.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeVisible();
  const paperSaved = await (await page.request.get(paperUrl)).json();
  await selectedPaper.getByRole("button", { name: "저장된 CLI 입력·결과 열기", exact: true }).click();
  const paperReceiptRoot = preparationRoot.replace("review-preparations", "review-inputs");
  const paperReceipt = await (await page.request.get(`${paperReceiptRoot}/${paperSaved.current.receipt_id}`)).json();
  expect(paperReceipt.input_origin).toBe("FIXTURE_SYNTHETIC");
  expect(paperReceipt.packet.evidence_spans[0].text).toBe(source.expected);
  await expect(drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })
    .getByText(paperReceipt.draft.contribution_summary.trim(), { exact: true })).toBeVisible();
  await openJournal();
  const journalDispatch = journalHistory.getByRole("region", { name: "서버 허용 Codex 실행", exact: true });
  await journalDispatch.getByRole("button", { name: "Codex 실행 허용 확인·새로고침", exact: true }).click();
  await journalDispatch.getByLabel("서버 실행 허용 선택").selectOption((journalBody as { authorization_id: string }).authorization_id);
  await journalDispatch.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청", exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(journalUrl)).json()).current.state).toBe("SUCCEEDED");
  await results.getByRole("button", { name: "검토 후보로 보존·열기", exact: true }).click();
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보", exact: true });
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집", exact: true });
  await expect(editor.getByRole("button", { name: "저널로 채택", exact: true })).toBeDisabled();
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("합성 승인·Python 연결 시험. 실제 모델 또는 과학적 검토 아님.");
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const download = page.waitForEvent("download");
  await candidate.getByRole("article", { name: "보존된 저널 검토 기록", exact: true })
    .getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기", exact: true }).click();
  const exportPath = path.join(artifacts, "approval-journal-reviewed-export.json");
  await (await download).saveAs(exportPath);
  const bundle = JSON.parse(fs.readFileSync(exportPath, "utf8"));
  expect(bundle.candidate.execution.adapter_manifest.schema_version).toBe("CODEX_JOURNAL_CLI_V2");
  expect(bundle.candidate.validation.semantic_support).toBe("NOT_ASSESSED");
  const launches = (file: string) => fs.readFileSync(file, "utf8").trim().split(/\r?\n/);
  expect(launches(paperLaunches)).toEqual(["launch"]); expect(launches(journalLaunches)).toEqual(["launch"]);
  expect((await page.request.get(paperUrl.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  expect((await page.request.get(journalUrl.replace(`/topics/${topicA}/`, `/topics/${topicB}/`))).status()).toBe(404);
  await page.goto(`/app/topics/${topicB}/papers`);
  await expect(paperApproval).toHaveCount(0); await expect(journalApproval).toHaveCount(0);
  expect(ledger.requests.some(request => request.pathname === "/auth/login")).toBe(false);
  expect(consoleErrors).toEqual([
    { text: expect.stringContaining("409"), url: `${appOrigin}${paperUrl}/codex-approval` },
    { text: expect.stringContaining("ERR_FAILED"), url: `${appOrigin}${paperUrl}/codex-approval` },
    { text: expect.stringContaining("ERR_FAILED"), url: `${appOrigin}${journalUrl}/codex-approval` },
  ]);
  expect(ledger.consoleErrors).toEqual(consoleErrors.map(error => error.text));
  ledger.handledExpectedErrors.push(...ledger.consoleErrors.splice(0)); await assertHygiene(ledger);
  fs.writeFileSync(path.join(artifacts, "codex-approval-observations.json"), JSON.stringify({ syntheticSession: { auth_mode: auth.auth_mode,
    authenticated: auth.authenticated, is_admin: auth.is_admin }, paperBody, journalBody, approvedPaper, authority, paperSaved, paperReceipt, ledger }, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { syntheticOnly: true, authenticatedNonadmin: true,
    realApprovalEndpoints: true, composedRuntimePreviewMatchesDispatch: true, staleSourceRejected: true, registrationDidNotLaunch: true, lostResponseExactReplay: true,
    sourceRemovedRestartReadback: true, separateManualDispatch: true, humanReviewBeforeAcceptedExport: true, passed: info.status === "passed" },
    timing: {}, hygiene: { consoleErrors: 0, pageErrors: 0, offOriginAttempts: 0 } });
});
