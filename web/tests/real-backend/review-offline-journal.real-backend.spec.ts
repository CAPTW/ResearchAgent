import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test, expect, topicA, topicB, installLedger, assertHygiene } from "./m2-paper-bank.real-backend.helpers";

for (const mode of ["atomic", "durable", "offline transport"] as const) {
const durable = mode !== "atomic", transport = mode === "offline transport";
test(`${mode} execution: prepared body range reaches synthetic review receipt, explicit Journal adoption and restart`, async ({ page, backend, context }, info) => {
  test.setTimeout(90000);
  for (const flag of ["XRML_JOURNAL_OFFLINE_ENABLED", transport ? "XRML_PAPER_TRANSPORT_OFFLINE_ENABLED"
    : durable ? "XRML_REVIEW_EXECUTION_OFFLINE_ENABLED" : "XRML_REVIEW_OFFLINE_ENABLED"]) {
    expect(process.env[flag], `${flag}=1 is required for this isolated synthetic journey`).toBe("1");
  }
  const ledger = await installLedger(context, page);
  const observedWrites: { operation: string; responseMs: number }[] = [];
  const artifacts = path.join(process.env.XRML_REAL_BACKEND_PLAYWRIGHT_OUTPUT_DIR!, mode.replaceAll(" ", "-"));
  const researchLabel = "고정 연구 e\u0301 한글🙂";
  fs.mkdirSync(artifacts, { recursive: true });
  const seed = spawnSync(backend.pythonExe, ["-c", `import json,sys
from app.services import paper_bank,paper_review,topic_profiles
from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from tests.test_ai_review_provider import _review
topic=sys.argv[1]
profile=topic_profiles.update_topic_profile(topic,dict(title=sys.argv[2]))
item=paper_bank.list_topic_papers(topic,page_size=100)['items'][0]
occ=item['compatibility']['occurrences'][0]
source='a'*26000+'e\\u0301 한글🙂 later methods and results '*300
artifact=build_run_paths(occ['runId']).raw_dir/(safe_filename(occ['paperId'])+'.txt')
artifact.write_bytes(source.encode('utf-8'))
review=_review(1,topic_id=topic,run_id=occ['runId'],paper_id=occ['paperId'],title=item['title'])
review.update(paper_review.read_local_full_text_evidence(run_id=occ['runId'],paper_id=occ['paperId'],max_text_chars=24000))
paper_reviews.upsert_review(review)
print(json.dumps(dict(item=item,topic_name=profile['topic'],other_topic_name=topic_profiles.get_topic_profile(sys.argv[3])['topic'],run=occ['runId'],paper=occ['paperId'],expected=source[26000:26010]),ensure_ascii=False))
`, topicA, researchLabel, topicB], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 30000 });
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

  const captureResponse = page.waitForResponse(response => new URL(response.url()).pathname === root && response.request().method() === "POST");
  await drawer.getByRole("button", { name: "선택 구간 고정", exact: true }).click();
  expect((await captureResponse).status()).toBe(200);
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.expected);
  const preparations = await (await page.request.get(root)).json();
  const preparation = await (await page.request.get(`${root}/${preparations.items[0].preparation_id}`)).json();
  expect(preparation.schema_version).toBe("PAPER_REVIEW_PREPARATION_V2");
  expect(preparation.context).toMatchObject({ topic_title: researchLabel, topic_name: fixture.topic_name,
    paper_title: fixture.item.title, authors: fixture.item.authors, metadata_digest: fixture.item.metadataDigest });
  const storedContext = drawer.getByRole("region", { name: "보존된 본문 구간 준비", exact: true })
    .getByRole("region", { name: "보존된 연구 맥락", exact: true });
  await expect(storedContext.getByText(`저장된 주제 제목: ${researchLabel}`, { exact: true })).toBeVisible();
  await expect(storedContext.getByText(`연구 주제: ${fixture.topic_name}`, { exact: true })).toBeVisible();
  await storedContext.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "saved-context-wide.png") });
  let lost = false;
  let confirmLoss!: () => void;
  let failLoss!: (reason: unknown) => void;
  const injectedLoss = new Promise<void>((resolve, reject) => { confirmLoss = resolve; failLoss = reject; });
  const submitted: unknown[] = [];
  const executionRoot = `${root}/${preparation.preparation_id}/${transport ? "transport-executions" : "executions"}`;
  const dispatchSuffix = transport ? "/offline-dispatch" : "/dispatch";
  await page.route(durable ? `**${executionRoot}/*${dispatchSuffix}` : `**${root}/${preparation.preparation_id}/offline-reviews`, async route => {
    submitted.push(route.request().postDataJSON());
    if (!lost) {
      lost = true;
      try {
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        await route.abort("failed");
        confirmLoss();
      } catch (error) { failLoss(error); throw error; }
    } else await route.continue();
  });
  let receiptId: string;
  let savedExecution: unknown;
  if (durable) {
    const toggle = drawer.getByText(transport ? "검토 실행 조건·이력" : "검토 실행 이력", { exact: true });
    await toggle.focus(); await toggle.press("Enter");
    const execution = drawer.getByRole("region", { name: transport ? "검토 실행 기록 확인" : "고정 입력 검토 실행", exact: true });
    const prepareName = transport ? "새 오프라인 연결 시험 요청 준비" : "새 합성 검토 실행 요청 준비";
    const prepareStarted = Date.now();
    const prepareResponse = page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === `${executionRoot}${transport ? "/offline-requests" : ""}`);
    await execution.getByRole("button", { name: prepareName, exact: true }).click();
    const preparedResponse = await prepareResponse;
    expect(preparedResponse.status()).toBe(200);
    const firstExecution = await preparedResponse.json();
    observedWrites.push({ operation: "prepare_execution", responseMs: Date.now() - prepareStarted });
    await expect(execution.getByText(/마지막 저장 상태: 요청 준비됨/)).toBeVisible();
    const cancel = execution.getByRole("button", { name: transport ? "이 오프라인 요청 취소" : "이 실행 요청 취소", exact: true });
    const cancelStarted = Date.now();
    const cancelResponse = page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === `${executionRoot}/${firstExecution.contract.execution_id}/controls`);
    await cancel.focus(); await cancel.press("Enter");
    expect((await cancelResponse).status()).toBe(200);
    observedWrites.push({ operation: "cancel_execution", responseMs: Date.now() - cancelStarted });
    await expect(execution.getByText(/마지막 저장 상태: 취소됨/)).toBeVisible();
    await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.expected);
    const nextPreparation = page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === `${executionRoot}${transport ? "/offline-requests" : ""}`);
    await execution.getByRole("button", { name: prepareName, exact: true }).click();
    const nextPrepared = await nextPreparation;
    expect(nextPrepared.status()).toBe(200);
    const nextExecution = await nextPrepared.json();
    const approvalResponse = page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === `${executionRoot}/${nextExecution.contract.execution_id}/${transport ? "offline-approval" : "controls"}`);
    await execution.getByRole("button", { name: transport ? "이 고정 요청의 오프라인 시험 승인" : "이 요청의 오프라인 실행 승인", exact: true }).click();
    expect((await approvalResponse).status()).toBe(200);
    await expect(execution.getByText(transport ? /마지막 저장 상태: 승인 기록됨/ : /마지막 저장 상태: 오프라인 실행 승인됨/)).toBeVisible();
    if (transport) {
      const approved = (await (await page.request.get(executionRoot)).json()).items[0];
      expect(approved.contract.mode).toBe("OFFLINE_SYNTHETIC_TRANSPORT");
      expect(approved.contract.transport).not.toHaveProperty("runtime_identity");
      expect(approved.authority.issuer_id).toBe("xrml-paper-offline-issuer-v1");
      expect(approved.events.map((event: {state: string}) => event.state)).toEqual(["READY", "APPROVED"]);
      await expect(execution.getByText(/실행 방식: 앱 안에서 고정 합성 응답/)).toBeVisible();
    }
    const start = execution.getByRole("button", { name: transport ? "승인된 오프라인 연결 시험 시작" : "승인된 합성 검토 실행 시작", exact: true });
    await start.focus();
    // Observe recovery only after the successful response has actually been
    // lost. Slow fixture writes must not consume the 5s UI recovery assertion.
    await Promise.all([start.press("Enter"), injectedLoss]);
    const retry = execution.getByRole("button", { name: transport ? "같은 오프라인 관리 요청 다시 보내기" : "같은 실행 요청 다시 보내기", exact: true });
    await expect(retry).toBeVisible();
    // Refresh stays disabled until this replay response arrives. Start the
    // terminal-state observation after the submission has actually completed.
    const replayResponse = page.waitForResponse(response =>
      new URL(response.url()).pathname.startsWith(`${executionRoot}/`)
      && new URL(response.url()).pathname.endsWith(dispatchSuffix)
      && response.request().method() === "POST");
    await retry.focus(); await retry.press("Enter");
    expect((await replayResponse).status()).toBe(200);
    await expect.poll(async () => {
      await execution.getByRole("button", { name: "검토 실행 이력 새로고침", exact: true }).click();
      await expect(execution.getByText("검토 실행 이력 확인 중", { exact: true })).toHaveCount(0);
      // A separate API read can be newer than this manual UI refresh. Wait for
      // the state the next control will actually use, not an unseen sequence.
      return execution.getByText(/^마지막 저장 상태:/).textContent();
    }).toContain("마지막 저장 상태: 결과 저장됨");
    const detail = (await (await page.request.get(executionRoot)).json()).items[0];
    expect(detail.current.state).toBe("RESULT_SAVED");
    savedExecution = detail;
    expect(detail.events.map((event: { state: string }) => event.state)).toEqual(["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]);
    receiptId = detail.current.receipt_id;
    if (transport) {
      const revocationResponse = page.waitForResponse(response => response.request().method() === "POST"
        && new URL(response.url()).pathname === `${executionRoot}/${detail.contract.execution_id}/offline-revocation`);
      await execution.getByRole("button", { name: "이 오프라인 시험 승인 철회", exact: true }).click();
      expect((await revocationResponse).status()).toBe(200);
      await expect(execution.getByText("승인 철회 기록 있음", { exact: true })).toBeVisible();
      savedExecution = (await (await page.request.get(executionRoot)).json()).items[0];
      expect((savedExecution as typeof detail).current).toEqual(detail.current);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
    await execution.scrollIntoViewIfNeeded();
    await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: path.join(artifacts, "review-execution-narrow.png") });
    await page.setViewportSize({ width: 1536, height: 1024 });
    await execution.getByRole("button", { name: transport ? "저장된 오프라인 입력·결과 열기" : "저장된 검토 입력·결과 열기", exact: true }).click();
  } else {
  const make = drawer.getByRole("button", { name: "이 준비 구간으로 합성 검토 결과 만들기", exact: true });
  await make.focus(); await Promise.all([make.press("Enter"), injectedLoss]);
  const retry = drawer.getByRole("button", { name: "같은 합성 검토 요청 다시 보내기", exact: true });
  await expect(retry).toBeVisible();
  await retry.focus(); await retry.press("Enter");
  await expect(drawer.getByText("원래 정규화 본문 위치: 26000–26010 · 선택 10자", { exact: true })).toBeVisible();
  expect(submitted).toHaveLength(2);
  expect(submitted[1]).toEqual(submitted[0]);
  const capability = await (await page.request.get(`${root}/${preparation.preparation_id}/offline-review`)).json();
  expect(capability.latest.adapter_version).toBe("2");
  receiptId = capability.latest.receipt_id;
  }
  expect(submitted).toHaveLength(2);
  expect(submitted[1]).toEqual(submitted[0]);
  const receipt = await (await page.request.get(root.replace("review-preparations", "review-inputs") + "/" + receiptId)).json();
  expect(receipt.packet.packet_version).toBe("ai-review-packet-v4");
  expect(receipt.packet.source_preparation.context).toEqual(preparation.context);
  expect(receipt.packet).toMatchObject({ topic_name: fixture.topic_name, title: fixture.item.title, authors: fixture.item.authors, abstract: null });
  expect(receipt.packet.evidence_spans[0].text).toBe(fixture.expected);
  expect(receipt.packet.source_preparation.preparation_id).toBe(preparation.preparation_id);
  expect(receipt.draft.novelty_signal).toBe("NOT_ASSESSED");
  expect(receipt.draft.review_level).toBe("full_text_grounded");
  expect(receipt.draft.cited_evidence_span_ids.length).toBeGreaterThanOrEqual(1);
  const offeredSpanIds = receipt.packet.evidence_spans.map((span: { span_id: string }) => span.span_id);
  expect(receipt.draft.cited_evidence_span_ids.every((spanId: string) => offeredSpanIds.includes(spanId))).toBe(true);
  expect(receipt.input_origin).toBe("FIXTURE_SYNTHETIC");
  if (transport) expect(receipt.validation.execution.schema_version).toBe("PREPARED_REVIEW_OFFLINE_TRANSPORT_V1");
  const executionProof = receipt.validation.prepared_execution;
  expect(executionProof.schema_version).toBe("PREPARED_REVIEW_RESULT_V3");
  expect(executionProof.contract.schema_version).toBe("PREPARED_REVIEW_REQUEST_V3");
  expect(executionProof.contract.provider_request.schema_name).toBe("PREPARED_PAPER_REVIEW_DRAFT_V3");
  expect(JSON.parse(executionProof.contract.provider_request.prompt)).toEqual(receipt.packet);
  expect(JSON.parse(executionProof.response.raw_text)).toEqual(receipt.draft);
  const provenance = drawer.getByLabel("보존된 실행 요청·응답 출처", { exact: true });
  const receiptContext = drawer.getByRole("region", { name: "선택한 검토 입력·결과", exact: true })
    .getByRole("region", { name: "보존된 연구 맥락", exact: true });
  await expect(receiptContext.getByText(`저장된 주제 제목: ${researchLabel}`, { exact: true })).toBeVisible();
  const provenanceToggle = provenance.locator("summary");
  await provenanceToggle.focus(); await provenanceToggle.press("Enter");
  await expect(provenance.getByText("요청 모델: synthetic-prepared-review-v2", { exact: true })).toBeVisible();
  await expect(provenance.getByText("보존된 V3 검사 계약은", { exact: false })).toBeVisible();
  await expect(provenance.getByText("본문 구간에 근거한 검토와 제공된 근거 구간 인용 1개 이상을 요구합니다.", { exact: false })).toBeVisible();
  await expect(provenance.getByText(`요청 SHA-256: ${executionProof.contract.request_sha256}`, { exact: true })).toBeVisible();
  expect(await (await page.request.get(`${root}/${preparation.preparation_id}`)).json()).toEqual(preparation);
  await provenance.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "review-offline-receipt-wide.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  const v3Explanation = provenance.getByText("보존된 V3 검사 계약은", { exact: false });
  await v3Explanation.scrollIntoViewIfNeeded();
  await expect(v3Explanation).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "review-v3-contract-narrow.png") });
  await page.setViewportSize({ width: 1536, height: 1024 });
  const item = fixture.item;
  const components = ["/evidence_spans/0/text"];
  const choose = drawer.getByRole("button", { name: "보고서 입력에 이 기록 선택", exact: true });
  await choose.focus(); await choose.press("Enter");
  await expect(page.getByRole("region", { name: "보고서에 선택한 검토 기록" })).toContainText(receipt.receipt_id);
  await drawer.getByRole("button", { name: "논문 상세 닫기" }).click();
  const freezeStarted = Date.now();
  const freezeResponse = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/topics/${topicA}/local-synthesis`);
  await page.getByRole("button", { name: "선택 보고서 입력 고정" }).click();
  const frozenResponse = await freezeResponse;
  expect(frozenResponse.status()).toBe(201);
  expect((await frozenResponse.json()).created).toBe(true);
  observedWrites.push({ operation: "freeze_report_input", responseMs: Date.now() - freezeStarted });
  const panel = page.getByRole("region", { name: "선택 문헌 근거 초안" });
  await expect(panel.getByText("수동 진행 가능 · 0 / 3 단계")).toBeVisible();
  const jobId = new URL(page.url()).searchParams.get("synthesis")!;
  const jobRoot = `/api/topics/${topicA}/local-synthesis/${jobId}`;
  const frozen = await (await page.request.get(jobRoot)).json();
  expect(frozen.inputs.schema_version).toBe("LOCAL_EVIDENCE_SYNTHESIS_V2");
  expect(frozen.inputs.papers[0].review_input).toMatchObject({ schema_version: "FROZEN_REVIEW_INPUT_V1", receipt: { receipt_id: receipt.receipt_id, packet: receipt.packet, draft: receipt.draft }, selected_component_ids: components });
  await panel.getByText("고정 입력 1편과 근거", { exact: true }).click();
  await panel.getByText(item.title, { exact: true }).click();
  await expect(panel.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toBeVisible();
  await expect(panel.getByRole("heading", { name: "과거 검토 결과 · 원문 사실과 구분되는 해석" })).toBeVisible();
  await panel.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "review-offline-frozen-wide.png") });
  for (let step=1; step<=3; step++) {
    const stepResponse = page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === `${jobRoot}/step`);
    await panel.getByRole("button", { name: "다음 한 단계 실행" }).click();
    expect((await stepResponse).status()).toBe(200);
    await expect(panel.getByText(`${step===3?"근거 초안 준비됨":"수동 진행 가능"} · ${step} / 3 단계`)).toBeVisible();
  }
  await panel.getByText("저널 입력·결과 파일 교환", { exact: true }).click();
  const exchange = panel.getByRole("region", { name: "저널 결과 교환" });
  await exchange.getByRole("button", { name: "저널 고정 입력 확인" }).click();
  await expect(exchange.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toBeVisible();
  const packet = await (await page.request.get(jobRoot + "/journal-exchange")).json();
  expect(packet.review_inputs[0]).toMatchObject({ receipt_id: receipt.receipt_id, selected_component_ids: components, review_result: receipt.draft, input_scope: receipt.packet.input_scope, source_preparation: receipt.packet.source_preparation });
  expect(packet.review_inputs[0].schema_version).toBe("JOURNAL_REVIEW_INPUT_V2");
  const summary = packet.review_inputs[0].validation.prepared_execution;
  expect(summary).toMatchObject({ schema_version: "PREPARED_REVIEW_RESULT_SUMMARY_V1", request_sha256: executionProof.contract.request_sha256, raw_text_sha256: executionProof.response.raw_text_sha256, independent_live_proof: "NOT_VERIFIED" });
  expect(summary).not.toHaveProperty("contract");
  expect(summary).not.toHaveProperty("response");
  const refs = packet.references.filter((ref: { kind: string }) => ref.kind === "review_input_excerpt");
  expect(refs).toHaveLength(components.length);
  expect(refs.every((ref: { review_receipt_id: string }) => ref.review_receipt_id === receipt.receipt_id)).toBe(true);
  await panel.getByText("저장된 생성 결과", { exact: true }).click();
  const results = panel.getByRole("region", { name: "저장된 저널 생성 결과" });
  await results.getByText("새 생성 요청 준비", { exact: true }).click();
  const form = results.getByRole("form", { name: "생성 요청 준비" });
  await form.getByLabel("오프라인 합성 요청 준비").check();
  const preparedResponse = page.waitForResponse(response=>response.url().endsWith(`${jobRoot}/journal-executions`) && response.request().method()==="POST");
  await form.getByRole("button", { name: "로컬 생성 요청 준비" }).click();
  const prepared = (await (await preparedResponse).json()).execution;
  expect(prepared.contract.provider_request.prompt).toContain(receipt.receipt_id);
  const offline = results.getByRole("region", { name: "오프라인 합성 실행", exact: true });
  const journalApproval = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `${jobRoot}/journal-executions/${prepared.contract.execution_id}/offline-approvals`);
  await offline.getByRole("button", { name: "오프라인 합성 실행 승인 기록", exact: true }).click();
  expect((await journalApproval).status()).toBe(201);
  await expect(offline.getByText(/승인만 기록했습니다/)).toBeVisible();
  // Dispatch acknowledges admission; the worker result and UI readback follow.
  const journalExecutionRoot = `${jobRoot}/journal-executions/${prepared.contract.execution_id}`;
  const journalStarted = Date.now();
  const journalDispatch = page.waitForResponse(response => new URL(response.url()).pathname === `${journalExecutionRoot}/offline-dispatch` && response.request().method() === "POST");
  // Observe the application's existing read-only poll, not just admission.
  // The 90s journey budget still covers storage and worker completion.
  const journalTerminal = page.waitForResponse(async response => {
    if (response.request().method() !== "GET" || new URL(response.url()).pathname !== journalExecutionRoot || response.status() !== 200) return false;
    return ["SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED"].includes((await response.json()).current.state);
  });
  await offline.getByRole("button", { name: "승인한 오프라인 합성 실행", exact: true }).click();
  expect((await journalDispatch).status()).toBe(200);
  expect((await (await journalTerminal).json()).current.state).toBe("SUCCEEDED");
  observedWrites.push({ operation: "journal_worker_result_readback", responseMs: Date.now() - journalStarted });
  await expect(results.getByText(/최근 저장 상태: 응답 저장 완료 · 시도 1/)).toBeVisible();
  const preservationResponse = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `${journalExecutionRoot}/candidate`);
  await results.getByRole("button", { name: "검토 후보로 보존·열기" }).click();
  const preserved = await preservationResponse;
  expect(preserved.status()).toBe(201);
  const preservedCandidate = (await preserved.json()).candidate;
  const candidate = results.getByRole("article", { name: "생성 결과 저널 후보" });
  const editor = candidate.getByRole("region", { name: "저널 근거 판단 편집" });
  await editor.getByLabel("문장 1 근거 판단").selectOption("limited");
  await editor.getByLabel("문장 1 검토 메모").fill("합성 결과이며 과거 검토 입력 연결만 확인했습니다.");
  await editor.getByLabel("저널 전체 검토 메모").fill("원문 전체 검토·실제 외부 모델 실행은 미확인입니다.");
  const adoptionResponse = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `${jobRoot}/journal-candidates/${preservedCandidate.candidate_id}/reviews`);
  await editor.getByRole("button", { name: "저널로 채택", exact: true }).click();
  const adopted = await adoptionResponse;
  expect(adopted.status()).toBe(201);
  expect((await adopted.json()).created).toBe(true);
  await expect(candidate.getByText("현재 사람의 검토 상태: 저널로 채택", { exact: true })).toBeVisible();
  const review = candidate.getByRole("article", { name: "보존된 저널 검토 기록" });
  const [download] = await Promise.all([page.waitForEvent("download"), review.getByRole("button", { name: "채택 저널 검토·근거 JSON 내려받기" }).click()]);
  const output = path.join(artifacts, "review-offline-journal.json"); await download.saveAs(output);
  const bundle = JSON.parse(fs.readFileSync(output, "utf8"));
  expect(bundle.candidate.packet.review_inputs).toEqual(packet.review_inputs);
  expect(bundle.candidate.packet.review_inputs[0].source_preparation.context).toEqual(preparation.context);
  await candidate.getByText("선택한 과거 검토 문맥 1개", { exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => { const box = await page.locator("#v2-sidebar").boundingBox(); return box ? box.x + box.width : 0; }).toBeLessThanOrEqual(0);
  await candidate.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true }).scrollIntoViewIfNeeded();
  const candidateProvenance = candidate.getByLabel("보존된 실행 요청·응답 출처", { exact: true });
  await candidateProvenance.locator("summary").focus(); await candidateProvenance.locator("summary").press("Enter");
  await expect(candidateProvenance.getByText(/이 저널 입력에는 요청·응답의 식별 요약만 포함/)).toBeVisible();
  await candidateProvenance.scrollIntoViewIfNeeded();
  const provenanceBounds = await candidateProvenance.boundingBox();
  expect(provenanceBounds).not.toBeNull();
  expect(provenanceBounds!.x).toBeGreaterThanOrEqual(0);
  expect(provenanceBounds!.x + provenanceBounds!.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({ path: path.join(artifacts, "review-offline-journal-narrow.png") });
  await page.goto(`/app/topics/${topicB}/reports`);
  await expect(page.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toHaveCount(0);
  expect((await page.request.get(jobRoot.replace(`/topics/${topicA}/`,`/topics/${topicB}/`))).status()).toBe(404);
  const change = spawnSync(backend.pythonExe, ["-c", `from app.repositories import paper_reviews
from app.repositories.runs import build_run_paths
from app.repositories.files import safe_filename
from app.services import paper_review
import sys
topic,run,paper=sys.argv[1:]
source=paper_reviews.get_review(topic,run,paper)
(build_run_paths(run).raw_dir/(safe_filename(paper)+'.txt')).write_text('Changed file after journal capture',encoding='utf-8')
source.update(paper_review.read_local_full_text_evidence(run_id=run,paper_id=paper,max_text_chars=24000))
paper_reviews.upsert_review(source)
`, topicA, receipt.run_id, receipt.paper_id], { cwd: backend.sourceRoot, env: { ...process.env, XRML_DATA_DIR: backend.dataRoot }, encoding: "utf8", windowsHide: true, timeout: 15000 });
  expect(change.status, change.stderr).toBe(0);
  const edit = await page.request.patch(`/topic-profiles/${topicA}`, { data: {
    title: "Changed display label after preparation", topic: fixture.other_topic_name,
  } });
  expect(edit.status()).toBe(200);
  await page.goto("about:blank"); await backend.restart();
  await page.goto(`/app/topics/${topicA}/papers?paper=${fixture.item.paperVersionId}`);
  await drawer.getByText("본문 구간 검토 준비", { exact: true }).click();
  await drawer.getByRole("button", { name: `준비 기록 열기 · ${preparation.preparation_id}`, exact: true }).click();
  await expect(drawer.getByLabel("보존된 준비 본문")).toHaveText(fixture.expected);
  await expect(storedContext.getByText(`연구 주제: ${fixture.topic_name}`, { exact: true })).toBeVisible();
  expect((await (await page.request.get(root.replace("review-preparations", "review-inputs") + "/" + receiptId)).json()).packet).toEqual(receipt.packet);
  await storedContext.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "renamed-topic-preparation-narrow.png") });
  await page.goto(`/app/topics/${topicA}/reports?synthesis=${jobId}`);
  await panel.getByText("고정 입력 1편과 근거", { exact: true }).click();
  await panel.getByText(item.title, { exact: true }).click();
  await expect(panel.getByText(`고정한 검토 기록: ${receipt.receipt_id}`, { exact: true })).toBeVisible();
  const reopenedContext = panel.getByRole("region", { name: "보존된 연구 맥락", exact: true }).first();
  await expect(reopenedContext.getByText(`저장된 주제 제목: ${researchLabel}`, { exact: true })).toBeVisible();
  await expect(reopenedContext.getByText("저장된 주제 제목: Changed display label after preparation", { exact: true })).toHaveCount(0);
  await reopenedContext.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(artifacts, "saved-context-restart-narrow.png") });
  const reopened = await (await page.request.get(jobRoot)).json();
  expect(reopened.inputs).toEqual(frozen.inputs);
  expect(await (await page.request.get(jobRoot + "/journal-exchange")).json()).toEqual(packet);
  const reread = await (await page.request.get(`${jobRoot}/journal-candidates/${bundle.candidate.candidate_id}/reviews/${bundle.review.review_id}/export?format=json`)).json();
  expect(JSON.parse(reread.content)).toEqual(bundle);
  if (durable) {
    const history = (await (await page.request.get(executionRoot)).json()).items;
    expect(history).toHaveLength(2);
    expect(history[0]).toEqual(savedExecution);
    expect(history[1].current).toMatchObject({ state: "CANCELLED", outcome_unknown: false });
    expect(history[0].current).toMatchObject({ state: "RESULT_SAVED", receipt_id: receiptId });
    expect(history[0].events.filter((event: { state: string }) => event.state === "RUNNING")).toHaveLength(1);
  }
  const expectedErrors = ledger.consoleErrors.splice(0);
  expect(expectedErrors.length).toBeGreaterThan(0);
  expect(expectedErrors.every(message => message.includes("ERR_FAILED"))).toBe(true);
  ledger.handledExpectedErrors.push(...expectedErrors);
  await assertHygiene(ledger);
  expect(ledger.mutationRequests.every(value=>value.includes("/local-synthesis") || value.includes("/review-preparations"))).toBe(true);
  fs.writeFileSync(path.join(artifacts, "observed-write-times.json"), JSON.stringify(observedWrites, null, 2));
  backend.writeJourneyResult({ journey: info.title, migrationIds: [], assertions: { explicitReceiptSelection: true, selectedComponentsFrozen: true, syntheticOnly: true, humanAccepted: true, exportRestartEqual: true, passed: info.status === "passed" }, timing: {}, hygiene: { consoleErrors: ledger.consoleErrors.length, pageErrors: ledger.pageErrors.length, offOriginAttempts: ledger.offOriginAttempts.length } });
});
}
