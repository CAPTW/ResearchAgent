import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ReviewPreparationBinding, ReviewResearchContext } from "../../lib/reviewPreparation";
import { FrozenReviewInputView, JournalReviewInputContext, PreservedReviewContext, ReviewInputHistory } from "../components/ReviewInputHistory";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const base = "/api/topics/topic%20one/paper-bank/version%2Fone/review-inputs";
const summary = { receipt_id: "receipt-one", recorded_at: "2026-09-28T09:00:00Z", input_origin: "FIXTURE_SYNTHETIC", run_id: "run-1", paper_id: "paper-1", source_text_sha256: "a".repeat(64), review_level: "full_text_grounded", validation_status: "validated",
  input_scope: { schema_version: "AI_REVIEW_INPUT_SCOPE_V1", source: { run_id: "run-1", paper_id: "paper-1", recorded_source_sha256: "a".repeat(64), preserved_text_sha256: "b".repeat(64), preserved_text_chars: 20000, document_chars: null }, components: [{ json_pointer: "/selected_text_excerpts/0/text", text_sha256: "c".repeat(64), chars: 4, source_start: 0, source_end: 4, mapping: "exact" }], covered_stored_chars: 4, document_coverage: "UNKNOWN", full_document_review: "NOT_ESTABLISHED", packet_payload_sha256: "d".repeat(64) } };
const list = { topicId: "topic one", paperVersionId: "version/one", page: 1, pageSize: 10, hasMore: false, items: [summary] };
const detail = { ...summary, packet: { selected_text_excerpts: [{ source: "text_excerpt", text: "e\u0301 한", char_count: 4 }], evidence_spans: [], abstract: "Extra abstract context", existing_review: { method_summary: "Prior review context" } }, draft: { contribution_summary: "Preserved contribution", method_summary: "Saved method", limitations: ["Bounded input only"], unsupported_claims: ["Unverified claim"] } };
const extraction = { schema_version: "REVIEW_EXTRACTION_V1", artifact: { kind: "text", sha256: "e".repeat(64), bytes: 140 }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_ERRORS_IGNORE", selection: { sha256: "f".repeat(64), chars: 40, max_chars: 40, available_chars: 90, truncation: "TRUNCATED" }, pdf_extraction: null };
const savedContext: ReviewResearchContext = { schema_version: "REVIEW_CONTEXT_V1", topic_title: "Frozen topic title", topic_name: "Frozen research question", paper_title: "Frozen paper title", authors: ["Frozen Author", "Second Author"], published_at: null, metadata_digest: "f".repeat(64), topic_updated_at: "2026-09-27T09:00:00Z" };
const preparedScope = { ...summary.input_scope, schema_version: "AI_REVIEW_INPUT_SCOPE_V2", source: { ...summary.input_scope.source, preserved_text_chars: 4 }, components: [{ ...summary.input_scope.components[0], json_pointer: "/evidence_spans/0/text" }] };
const contextBinding: ReviewPreparationBinding = { schema_version: "REVIEW_PREPARATION_BINDING_V2", preparation_id: "preparation-one", preparation_sha256: "e".repeat(64), topic_id: "topic one", paper_version_id: "version/one", source: { run_id: "run-1", paper_id: "paper-1", source_chars: 30000, source_digest: "f".repeat(64), source_kind: "text", artifact_sha256: "a".repeat(64), artifact_bytes: 40000, artifact_ref: "run:raw/paper.txt", extraction_truncation: "UNKNOWN" }, selection: { start: 26000, end: 26004, chars: 4, sha256: "c".repeat(64) }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT", context: savedContext };
const contextReceipt = { ...detail, input_scope: preparedScope, packet: { packet_version: "ai-review-packet-v4", input_scope: preparedScope, topic_name: savedContext.topic_name, title: savedContext.paper_title, authors: savedContext.authors, published_at: savedContext.published_at, selected_text_excerpts: [], evidence_spans: [{ span_id: "prepared-selection", text: "e\u0301 한", char_start: 0, char_end: 4 }], source_preparation: contextBinding } };
const legacyContextReceipt = { ...detail, packet: { ...detail.packet, packet_version: "ai-review-packet-v2", input_scope: summary.input_scope, topic_name: "Preserved legacy topic", title: "Preserved legacy paper", authors: ["Legacy Author"] } };
const absentPreparationContext = "별도의 준비 맥락 기록 없음 · 이 기록에는 새 준비 맥락 항목이 없습니다. 현재 메타데이터로 채우지 않습니다.";
beforeEach(() => vi.resetAllMocks());
async function open() {
  const view = render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one"/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("검토 입력·결과 이력"));
  await screen.findByRole("button", { name: /입력·결과 열기/ });
  return view;
}

it("opens exact saved packet and readable result with honest scope, using only GET requests and keyboard controls", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : detail) as never);
  const user = userEvent.setup();
  await open();
  const button = screen.getByRole("button", { name: /입력·결과 열기/ });
  expect(button.textContent).toBe("입력·결과 열기");
  expect(button.getAttribute("aria-label")).toBe("입력·결과 열기 · receipt-one");
  button.focus(); await user.keyboard("{Enter}");
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText("합성 실행 · 실제 AI 본문 검토가 아닙니다.")).toBeTruthy();
  expect(within(receipt).getByText("저장 본문 20000자 중 위치 확인된 4자")).toBeTruthy();
  expect(within(receipt).getByText(/전체 문서 길이: 미확인 · 전체 문서 검토: 확인되지 않음/)).toBeTruthy();
  expect(within(receipt).queryByText(/%/)).toBeNull();
  expect(within(receipt).getByText(/12,000자.*초록·근거 발췌·기존 검토/)).toBeTruthy();
  expect(within(receipt).getByText("Preserved contribution")).toBeTruthy();
  expect(within(receipt).getByText("Bounded input only")).toBeTruthy();
  const excerpt = within(receipt).getByLabelText("선택 발췌 1");
  expect(excerpt.closest("details")?.open).toBe(false);
  fireEvent.click(within(receipt).getByText("발췌 1 · 출처: text_excerpt · 4자"));
  expect(excerpt.closest("details")?.open).toBe(true);
  fireEvent.click(within(receipt).getByText("보존된 입력 전체(JSON)"));
  expect(JSON.parse((await within(receipt).findByLabelText("보존된 검토 입력 JSON")).textContent!)).toEqual(detail.packet);
  expect(within(receipt).getByLabelText("선택 발췌 1").textContent).toBe("e\u0301 한");
  expect(api.fetchJson).toHaveBeenCalledWith(`${base}?page=1&pageSize=10`);
  expect(api.fetchJson).toHaveBeenCalledWith(`${base}/receipt-one`);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("selects registered nonempty packet components only after explicit keyboard confirmation", async () => {
  const onSelectForReport = vi.fn();
  const components = [...summary.input_scope.components, { ...summary.input_scope.components[0], json_pointer: "/evidence_spans/0/text", chars: 22 }, { ...summary.input_scope.components[0], json_pointer: "/evidence_spans/1/text", chars: 0 }];
  const saved = { ...detail, packet: { ...detail.packet, packet_version: "ai-review-packet-v2", evidence_spans: [{ span_id: "span-1", text: "Extra evidence context" }, { span_id: "span-2", text: "" }], input_scope: { ...summary.input_scope, components } } };
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : saved) as never);
  render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one" onSelectForReport={onSelectForReport}/>);
  fireEvent.click(screen.getByText("검토 입력·결과 이력"));
  fireEvent.click(await screen.findByRole("button", { name: /입력·결과 열기/ }));
  const choose = await screen.findByRole("button", { name: "보고서 입력에 이 기록 선택" });
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "보존된 검토 입력과 결과" })));
  expect(onSelectForReport).not.toHaveBeenCalled();
  choose.focus();
  expect(document.activeElement).toBe(choose);
  await userEvent.setup().keyboard("{Enter}");
  expect(onSelectForReport).toHaveBeenCalledExactlyOnceWith({ receipt_id: "receipt-one", component_ids: ["/evidence_spans/0/text", "/selected_text_excerpts/0/text"] });
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("explains why legacy input without registered V2 components cannot be selected", async () => {
  const onSelectForReport = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : detail) as never);
  render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one" onSelectForReport={onSelectForReport}/>);
  fireEvent.click(screen.getByText("검토 입력·결과 이력"));
  fireEvent.click(await screen.findByRole("button", { name: /입력·결과 열기/ }));
  expect(await screen.findByRole("button", { name: "보고서 입력에 이 기록 선택" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/등록된 V2\/V3\/V4 입력 구간이 없어/)).toBeTruthy();
  expect(onSelectForReport).not.toHaveBeenCalled();
});

it("shows only frozen V4 context and lets the preserved body flow into report selection", async () => {
  const onSelectForReport = vi.fn();
  const saved = { ...contextReceipt, packet: { ...contextReceipt.packet, title: "Other packet title must not replace frozen context" } };
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : saved) as never);
  render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one" openReceipt={{ receiptId: "receipt-one", revision: 1 }} onSelectForReport={onSelectForReport}/>);
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  // The receipt moves focus in an effect after its asynchronous load. Wait for
  // that initial focus before sending keyboard input to another control.
  const receiptHeading = within(receipt).getByRole("heading", { name: "보존된 검토 입력과 결과" });
  await waitFor(() => expect(document.activeElement).toBe(receiptHeading));
  const context = within(receipt).getByRole("region", { name: "보존된 연구 맥락" });
  expect(within(context).getByText("저장된 주제 제목: Frozen topic title")).toBeTruthy();
  expect(within(context).getByText("연구 주제: Frozen research question")).toBeTruthy();
  expect(within(context).getByText("논문 제목: Frozen paper title")).toBeTruthy();
  expect(within(context).getByText("저자: Frozen Author, Second Author")).toBeTruthy();
  expect(within(context).getByText("발행일: 미기록")).toBeTruthy();
  expect(within(context).getByText(/추가로 검토한 본문이나 근거가 아닙니다/)).toBeTruthy();
  expect(within(receipt).queryByText("Other packet title must not replace frozen context")).toBeNull();
  expect(within(receipt).getByText("원래 정규화 본문 위치: 26000–26004 · 선택 4자")).toBeTruthy();
  expect(within(receipt).getByText("검토 입력에 보존된 고정 본문")).toBeTruthy();
  expect(within(receipt).getByText("준비 구간 1 · prepared-selection")).toBeTruthy();
  expect(within(receipt).queryByText("보존된 선택 발췌 없음")).toBeNull();
  expect(within(receipt).queryByText(/12,000자 기본 한도/)).toBeNull();
  const choose = within(receipt).getByRole("button", { name: "보고서 입력에 이 기록 선택" });
  expect(choose).toHaveProperty("disabled", false);
  choose.focus();
  expect(document.activeElement).toBe(choose);
  await userEvent.setup().keyboard("{Enter}");
  expect(onSelectForReport).toHaveBeenCalledExactlyOnceWith({ receipt_id: "receipt-one", component_ids: ["/evidence_spans/0/text"] });
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("retains frozen V4 context in the frozen input and Journal without looking up current metadata", () => {
  const view = render(<FrozenReviewInputView value={{ schema_version: "FROZEN_REVIEW_INPUT_V1", selected_component_ids: ["/evidence_spans/0/text"], receipt: contextReceipt }}/>);
  expect(within(screen.getByRole("region", { name: "보존된 연구 맥락" })).getByText("연구 주제: Frozen research question")).toBeTruthy();
  expect(screen.getByText("e\u0301 한")).toBeTruthy();
  view.rerender(<JournalReviewInputContext value={{ schema_version: "JOURNAL_REVIEW_INPUT_V2", paper_version_id: "version/one", receipt_id: "receipt-one", input_origin: "FIXTURE_SYNTHETIC", run_id: "run-1", paper_id: "paper-1", review_level: "full_text_grounded", status: "needs_review", selected_component_ids: ["/evidence_spans/0/text"], input_scope: preparedScope, source_preparation: contextBinding, source_extraction: null, review_result: detail.draft, validation: {} }}/>);
  const context = screen.getByRole("region", { name: "보존된 연구 맥락" });
  expect(within(context).getByText("논문 제목: Frozen paper title")).toBeTruthy();
  expect(within(context).getByText(`논문 메타데이터 식별값: ${savedContext.metadata_digest}`)).toBeTruthy();
  expect(screen.getByText(/이번 보고서의 선택 구간: \/evidence_spans\/0\/text/)).toBeTruthy();
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("labels the absent legacy preparation context field and rejects unreadable saved context without substitution", () => {
  const props = { receiptId: "legacy", inputOrigin: "MANUAL_IMPORT_UNVERIFIED", scope: preparedScope, extraction: null, result: detail.draft };
  const legacy = { ...contextBinding, schema_version: "REVIEW_PREPARATION_BINDING_V1", context: undefined };
  const view = render(<PreservedReviewContext {...props} preparation={legacy}/>);
  expect(screen.getByText(absentPreparationContext)).toBeTruthy();
  expect(screen.getByText(/현재 메타데이터로 채우지 않습니다/)).toBeTruthy();
  expect(screen.queryByText(/논문 제목:/)).toBeNull();
  view.rerender(<PreservedReviewContext {...props} preparation={{ ...contextBinding, context: { ...savedContext, authors: "not an author list" } }}/>);
  expect(screen.getByText(/보존된 연구 맥락을 해석할 수 없습니다/)).toBeTruthy();
  expect(screen.queryByText(/논문 제목:/)).toBeNull();
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("identifies only the missing preparation context field while retaining a legacy V2 packet's title and topic", async () => {
  const original = JSON.stringify(legacyContextReceipt);
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : legacyContextReceipt) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText(absentPreparationContext)).toBeTruthy();
  expect(within(receipt).queryByText(/연구 맥락이 보존되지 않았습니다/)).toBeNull();
  expect(within(receipt).queryByText(/저장된 주제 제목:|연구 주제:|논문 제목:/)).toBeNull();
  fireEvent.click(within(receipt).getByText("보존된 입력 전체(JSON)"));
  const packet = JSON.parse((await within(receipt).findByLabelText("보존된 검토 입력 JSON")).textContent!);
  expect(packet).toEqual(legacyContextReceipt.packet);
  expect(packet).toMatchObject({ topic_name: "Preserved legacy topic", title: "Preserved legacy paper" });
  expect(packet).not.toHaveProperty("source_preparation");
  expect(JSON.stringify(legacyContextReceipt)).toBe(original);
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
});

it("preserves legacy metadata JSON in frozen input and limits the Journal notice to its absent preparation field", () => {
  const original = JSON.stringify(legacyContextReceipt);
  const view = render(<FrozenReviewInputView value={{ schema_version: "FROZEN_REVIEW_INPUT_V1", selected_component_ids: ["/selected_text_excerpts/0/text"], receipt: legacyContextReceipt }}/>);
  expect(screen.getByText(absentPreparationContext)).toBeTruthy();
  const frozenJson = screen.getByText("고정한 검토 기록 원문(JSON)").closest("details")!.querySelector("pre")!;
  const frozen = JSON.parse(frozenJson.textContent!);
  expect(frozen).toEqual(legacyContextReceipt);
  expect(frozen.packet).toMatchObject({ topic_name: "Preserved legacy topic", title: "Preserved legacy paper" });
  expect(frozen.packet).not.toHaveProperty("source_preparation");

  view.rerender(<JournalReviewInputContext value={{ schema_version: "JOURNAL_REVIEW_INPUT_V1", paper_version_id: "version/one", receipt_id: "receipt-one", input_origin: "FIXTURE_SYNTHETIC", run_id: "run-1", paper_id: "paper-1", review_level: "full_text_grounded", status: "needs_review", selected_component_ids: ["/selected_text_excerpts/0/text"], input_scope: summary.input_scope, source_extraction: null, review_result: detail.draft, validation: {} }}/>);
  expect(screen.getByText(absentPreparationContext)).toBeTruthy();
  expect(screen.queryByText(/연구 맥락이 보존되지 않았습니다/)).toBeNull();
  expect(screen.queryByText(/저장된 주제 제목:|연구 주제:|논문 제목:/)).toBeNull();
  const resultJson = screen.getByText("과거 검토 결과 원문(JSON)").closest("details")!.querySelector("pre")!;
  expect(JSON.parse(resultJson.textContent!)).toEqual(detail.draft);
  expect(JSON.stringify(legacyContextReceipt)).toBe(original);
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it.each([
  ["PROVIDER_RESPONSE", "Provider 응답 · 실제 모델·연구 품질은 독립적으로 검증되지 않았습니다."],
  ["MANUAL_IMPORT_UNVERIFIED", "수동 반입 · 실제 전달 범위와 모델 실행은 미확인입니다."],
])("labels %s without claiming an actual full-document review", async (input_origin, label) => {
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : { ...detail, input_origin, source_text_sha256: "", input_scope: { schema_version: "LEGACY_REVIEW_INPUT_UNKNOWN" } }) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText(label)).toBeTruthy();
  expect(within(receipt).getByText(/저장 본문 길이·처리 위치 미확인/)).toBeTruthy();
  expect(within(receipt).queryByText(/저장 본문 0자|위치 확인된 0자|%/)).toBeNull();
  fireEvent.click(within(receipt).getByText("결과 원문과 기록 식별자"));
  expect(within(receipt).getByText("기록된 출처 식별값 (source_text_sha256): 미확인")).toBeTruthy();
  expect(within(receipt).getByText(/원문 전체 바이트의 검증을 뜻하지 않습니다/)).toBeTruthy();
});

it("shows the preserved artifact and normalized selection limit without claiming complete review", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : { ...detail, packet: { ...detail.packet, source_extraction: extraction } }) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText("정규화 후 선택: 40자 · 선택 한도: 40자 · 선택 전 텍스트: 90자")).toBeTruthy();
  expect(within(receipt).getByText("선택 범위 잘림: 잘림 확인")).toBeTruthy();
  fireEvent.click(within(receipt).getByText("원문·추출 기록 상세"));
  expect(within(receipt).getByText("당시 읽은 파일: 텍스트 · 140바이트")).toBeTruthy();
  expect(within(receipt).getByText(`파일 SHA-256: ${extraction.artifact.sha256}`)).toBeTruthy();
  expect(within(receipt).getByText(`선택 텍스트 SHA-256: ${extraction.selection.sha256}`)).toBeTruthy();
  expect(within(receipt).getByText(/잘림 관측 없음도 논문 전체 확보나 AI 전체 검토를 뜻하지 않습니다/)).toBeTruthy();
  expect(within(receipt).getByText(/해석할 수 없는 UTF-8 바이트는 무시/)).toBeTruthy();
  expect(within(receipt).queryByText(/%|전체 검토 완료/)).toBeNull();
});

it("keeps a missing legacy extraction record unknown instead of inferring it from preserved snippets", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : detail) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText(/보존된 원문·추출 범위 미확인/)).toBeTruthy();
  expect(within(receipt).getByText(/현재 파일에서 추정하지 않습니다/)).toBeTruthy();
  expect(within(receipt).queryByText(/선택 범위 잘림:|당시 읽은 파일:/)).toBeNull();
});

it("distinguishes unknown PDF totals from visited and retained pages", async () => {
  const pdf = { schema_version: "PDF_EXTRACTION_PROVENANCE_V1", source_pdf: { sha256: "1".repeat(64), bytes: 500 }, extractor: { name: "pymupdf", version: null, normalization: "XRML_WHITESPACE_V1" }, max_chars: 50, document_page_count: null, visited_page_numbers: [1, 2], retained_page_numbers: [1], truncation: "UNKNOWN", document_text_chars: null, outputs: { text_sha256: "2".repeat(64), text_bytes: 65, pages_sha256: "3".repeat(64), pages_bytes: 120 } };
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : { ...detail, packet: { ...detail.packet, source_extraction: { ...extraction, artifact: { ...pdf.source_pdf, kind: "pdf_extract" }, decoding: "PDF_ENGINE", selection: { ...extraction.selection, available_chars: null, truncation: "UNKNOWN" }, pdf_extraction: pdf } } }) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText("정규화 후 선택: 40자 · 선택 한도: 40자 · 선택 전 텍스트: 미확인")).toBeTruthy();
  expect(within(receipt).getByText("선택 범위 잘림: 미확인")).toBeTruthy();
  fireEvent.click(within(receipt).getByText("원문·추출 기록 상세"));
  expect(within(receipt).getByText("PDF 전체 페이지 수: 미확인")).toBeTruthy();
  expect(within(receipt).getByText("추출기가 방문한 페이지: 1, 2 (2개)")).toBeTruthy();
  expect(within(receipt).getByText("텍스트가 보존된 페이지: 1 (1개)")).toBeTruthy();
  expect(within(receipt).getByText("PDF 추출 잘림: 미확인 · 추출 한도: 50자")).toBeTruthy();
  expect(within(receipt).getByText(`원본 PDF: 500바이트 · SHA-256: ${pdf.source_pdf.sha256}`)).toBeTruthy();
  expect(within(receipt).queryByText(/PDF 전체 페이지 수: 2|%/)).toBeNull();
});

it("distinguishes empty history from a failed lookup and retries independently", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("store offline")).mockResolvedValueOnce({ ...list, items: [] });
  render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one"/>);
  fireEvent.click(screen.getByText("검토 입력·결과 이력"));
  await screen.findByText(/store offline/);
  expect(screen.queryByText(/보존된 검토 입력 기록이 없습니다/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "검토 입력 이력 새로고침" }));
  await screen.findByText(/보존된 검토 입력 기록이 없습니다/);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("retains a readable selection while list refresh fails and separately retries a failed detail", async () => {
  let listReads = 0, detailReads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.includes("?")) { if (++listReads > 1) throw new Error("list unavailable"); return list as never; }
    if (++detailReads === 1) throw new Error("receipt unavailable");
    return detail as never;
  });
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  await screen.findByText(/receipt unavailable/);
  fireEvent.click(screen.getByRole("button", { name: "선택 기록 다시 확인" }));
  await screen.findByText("Preserved contribution");
  fireEvent.click(screen.getByRole("button", { name: "검토 입력 이력 새로고침" }));
  await screen.findByText(/list unavailable/);
  expect(screen.getByText("Preserved contribution")).toBeTruthy();
});

it("paginates history and drops a previous topic's pending detail on scope change", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path === `${base}/receipt-one`) return new Promise(resolve => { finish = resolve; }) as never;
    if (path.startsWith(base)) return { ...list, page: path.includes("page=2") ? 2 : 1, hasMore: !path.includes("page=2") } as never;
    return { ...list, topicId: "other", items: [], hasMore: false } as never;
  });
  const { rerender } = await open();
  fireEvent.click(screen.getByRole("button", { name: "다음 검토 입력 페이지" }));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledWith(`${base}?page=2&pageSize=10`));
  fireEvent.click(await screen.findByRole("button", { name: /입력·결과 열기/ }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  rerender(<ReviewInputHistory topicId="other" paperVersionId="version/one"/>);
  fireEvent.click(screen.getByText("검토 입력·결과 이력"));
  await screen.findByText(/보존된 검토 입력 기록이 없습니다/);
  await act(async () => finish(detail));
  expect(screen.queryByText("Preserved contribution")).toBeNull();
  expect(screen.queryByRole("region", { name: "선택한 검토 입력·결과" })).toBeNull();
});

it.each(["FIXTURE_SYNTHETIC", "PROVIDER_RESPONSE", "MANUAL_IMPORT_UNVERIFIED"].flatMap(origin => ["UTF8_STRICT", "UTF8_SIG_STRICT"].map(decoding => [origin, decoding])))("opens a %s V3 receipt with %s without report selection and distinguishes coordinate bases", async (origin, decoding) => {
  const onSelectForReport = vi.fn();
  const scope = { ...summary.input_scope, schema_version: "AI_REVIEW_INPUT_SCOPE_V2", source: { ...summary.input_scope.source, preserved_text_chars: 4, coordinate_space: "PREPARED_SELECTION_CODEPOINTS" }, components: [{ ...summary.input_scope.components[0], json_pointer: "/evidence_spans/0/text" }] };
  const preparation = { schema_version: "REVIEW_PREPARATION_BINDING_V1", preparation_id: "rprep-one", preparation_sha256: "e".repeat(64), topic_id: "topic one", paper_version_id: "version/one", source: { run_id: "run-1", paper_id: "paper-1", source_chars: 30000, source_digest: "f".repeat(64), source_kind: "text", artifact_sha256: "a".repeat(64), artifact_bytes: 40000, artifact_ref: "run:raw/paper.txt", extraction_truncation: "UNKNOWN" }, selection: { start: 26000, end: 26004, chars: 4, sha256: "c".repeat(64) }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT" };
  const saved = { ...detail, input_origin: origin, input_scope: scope, packet: { packet_version: "ai-review-packet-v3", input_scope: scope, selected_text_excerpts: [], evidence_spans: [{ span_id: "prepared-selection", text: "e\u0301 한", char_start: 0, char_end: 4 }], source_preparation: { ...preparation, decoding, source: { ...preparation.source, source_kind: decoding === "UTF8_SIG_STRICT" ? "pages" : "text", artifact_ref: decoding === "UTF8_SIG_STRICT" ? "run:raw/paper.pages.json" : preparation.source.artifact_ref } } }, draft: { ...detail.draft, confidence: 0.0, novelty_signal: "NOT_ASSESSED" } };
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : saved) as never);
  render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one" openReceipt={{ receiptId: "receipt-one", revision: 1 }} onSelectForReport={onSelectForReport}/>);
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(screen.getByText("검토 입력·결과 이력").closest("details")?.open).toBe(true);
  expect(within(receipt).getByText("보존된 선택 구간 4자 중 위치 확인된 4자")).toBeTruthy();
  expect(within(receipt).getByText("원래 정규화 본문 위치: 26000–26004 · 선택 4자")).toBeTruthy();
  expect(within(receipt).getByText(absentPreparationContext)).toBeTruthy();
  expect(within(receipt).getByText(/구간 내부 위치 0부터 계산/)).toBeTruthy();
  if (origin === "FIXTURE_SYNTHETIC") expect(within(receipt).getByText(/연구 품질·신규성·신뢰도는 미평가/)).toBeTruthy();
  else expect(within(receipt).queryByText(/합성 연결 시험의 작성 결과/)).toBeNull();
  expect(within(receipt).getByText("미평가")).toBeTruthy();
  expect(within(receipt).getByText("검토 입력에 보존된 고정 본문")).toBeTruthy();
  expect(within(receipt).queryByText("합성 검토에 사용한 고정 본문")).toBeNull();
  expect(within(receipt).getByText(origin === "FIXTURE_SYNTHETIC" ? "합성 실행 · 실제 AI 본문 검토가 아닙니다." : origin === "PROVIDER_RESPONSE" ? "Provider 응답 · 실제 모델·연구 품질은 독립적으로 검증되지 않았습니다." : "수동 반입 · 실제 전달 범위와 모델 실행은 미확인입니다.")).toBeTruthy();
  expect(within(receipt).queryByText("보존된 선택 발췌 없음")).toBeNull();
  expect(JSON.parse(within(receipt).getByLabelText("보존된 검토 결과 JSON").textContent!).novelty_signal).toBe("NOT_ASSESSED");
  expect(within(receipt).getByText(decoding === "UTF8_STRICT" ? /읽기: UTF-8 엄격 검증/ : /읽기: UTF-8 BOM 허용·엄격 검증/)).toBeTruthy();
  // Finish the initial receipt focus before moving it for keyboard selection.
  await waitFor(() => expect(document.activeElement).toBe(within(receipt).getByRole("heading", { name: "보존된 검토 입력과 결과" })));
  expect(onSelectForReport).not.toHaveBeenCalled();
  const choose = within(receipt).getByRole("button", { name: "보고서 입력에 이 기록 선택" });
  expect(choose).toHaveProperty("disabled", false);
  choose.focus();
  expect(document.activeElement).toBe(choose);
  await userEvent.setup().keyboard("{Enter}");
  expect(onSelectForReport).toHaveBeenCalledExactlyOnceWith({ receipt_id: "receipt-one", component_ids: ["/evidence_spans/0/text"] });
});

it("refreshes and retries the same external receipt on a later open request without carrying it to another topic", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (!path.startsWith(base)) return { ...list, items: [], topicId: "other" } as never;
    if (path.includes("?")) return list as never;
    if (++reads === 1) throw new Error("new receipt temporarily unavailable");
    return detail as never;
  });
  const view = render(<ReviewInputHistory topicId="topic one" paperVersionId="version/one" openReceipt={{ receiptId: "receipt-one", revision: 1 }}/>);
  await screen.findByText(/new receipt temporarily unavailable/);
  view.rerender(<ReviewInputHistory topicId="topic one" paperVersionId="version/one" openReceipt={{ receiptId: "receipt-one", revision: 2 }}/>);
  await screen.findByText("Preserved contribution");
  expect(reads).toBe(2);
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([path]) => path === `${base}?page=1&pageSize=10`)).toHaveLength(2);
  view.rerender(<ReviewInputHistory topicId="other" paperVersionId="version/one"/>);
  expect(screen.getByText("검토 입력·결과 이력").closest("details")?.open).toBe(false);
  expect(screen.queryByText("Preserved contribution")).toBeNull();
});

const preparedExecution = {
  schema_version: "PREPARED_REVIEW_RESULT_V1",
  contract: { schema_version: "PREPARED_REVIEW_REQUEST_V1", provider_name: "xrml_offline_review", model: "requested-synthetic-v1", provider_request: { schema_name: "PREPARED_PAPER_REVIEW_DRAFT_V1", instructions: "fixture instructions", prompt: "preserved full input", metadata: {}, schema: {}, max_output_tokens: 2200, temperature: 0.2 }, request_sha256: "1".repeat(64), contract_sha256: "2".repeat(64) },
  response: { provider_name: "xrml_offline_review", model: "reported-synthetic-alias", response_id: "synthetic-response-one", raw_text: "{\"fixture\":true}", raw_text_sha256: "3".repeat(64) },
  reported_model_identity: "NOT_VERIFIED", independent_live_proof: "NOT_VERIFIED", scientific_support: "NOT_ASSESSED", full_document_review: "NOT_ESTABLISHED", human_adoption: "NOT_ESTABLISHED",
};
const executionVersion = (version: "V1" | "V2" | "V3") => ({ ...preparedExecution, schema_version: `PREPARED_REVIEW_RESULT_${version}`, contract: { ...preparedExecution.contract, schema_version: `PREPARED_REVIEW_REQUEST_${version}`, provider_request: { ...preparedExecution.contract.provider_request, schema_name: `PREPARED_PAPER_REVIEW_DRAFT_${version}` } } });
const legacyLevelExplanation = "이 과거 기록은 본문 구간 입력에 초록 기반 결과 유형을 저장했습니다. 당시 계약은 입력과 결과 유형의 일치를 확인하지 않았습니다.";
const bodyOnlyPacket = { ...contextReceipt.packet, review_level: "full_text_grounded", abstract: null };
const abstractOnlyDraft = { ...detail.draft, review_level: "abstract_only", cited_evidence_span_ids: [] };
function preservedExecution(version: "V1" | "V2" | "V3", packet: unknown, payload: unknown) {
  const proof = executionVersion(version);
  return { ...proof, contract: { ...proof.contract, packet, provider_request: { ...proof.contract.provider_request, prompt: JSON.stringify(packet) } },
    response: { ...proof.response, payload, raw_text: JSON.stringify(payload, null, 2) } };
}

it.each(["V1", "V2"] as const)("explains an evidenced historical %s body-only input / abstract-only result mismatch without changing the saved receipt", async version => {
  const proof = preservedExecution(version, bodyOnlyPacket, abstractOnlyDraft);
  const validation = { prepared_execution: proof };
  const saved = { ...detail, review_level: "abstract_only", packet: bodyOnlyPacket, draft: abstractOnlyDraft, validation };
  const original = JSON.stringify(saved);
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? { ...list, items: [{ ...summary, review_level: "abstract_only" }] } : saved) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  expect(within(receipt).getByText(legacyLevelExplanation)).toBeTruthy();
  expect(within(receipt).getByText("전체 문서 길이: 미확인 · 전체 문서 검토: 확인되지 않음")).toBeTruthy();
  const raw = within(receipt).getByText("당시 형식·근거 연결 검사 기록").closest("details")!.querySelector("pre")!;
  expect(JSON.parse(raw.textContent!)).toEqual(validation);
  expect(JSON.parse(raw.textContent!).prepared_execution.response.raw_text).toBe(proof.response.raw_text);
  expect(JSON.parse(within(receipt).getByLabelText("보존된 검토 결과 JSON").textContent!)).toEqual(abstractOnlyDraft);
  expect(JSON.stringify(saved)).toBe(original);
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
});

it.each([
  { label: "preserved abstract present", version: "V2" as const, packet: { ...bodyOnlyPacket, abstract: "Actual preserved abstract" }, payload: abstractOnlyDraft },
  { label: "abstract field absent", version: "V2" as const, packet: { review_level: "full_text_grounded" }, payload: abstractOnlyDraft },
  { label: "abstract-only request", version: "V2" as const, packet: { ...bodyOnlyPacket, review_level: "abstract_only" }, payload: abstractOnlyDraft },
  { label: "matching body result", version: "V2" as const, packet: bodyOnlyPacket, payload: { ...abstractOnlyDraft, review_level: "full_text_grounded" } },
  { label: "packet missing", version: "V2" as const, packet: null, payload: abstractOnlyDraft },
  { label: "payload missing", version: "V2" as const, packet: bodyOnlyPacket, payload: null },
  { label: "V3 is not a legacy contract", version: "V3" as const, packet: bodyOnlyPacket, payload: abstractOnlyDraft },
])("does not infer a historical type mismatch when $label", ({ version, packet, payload }) => {
  const validation = { prepared_execution: preservedExecution(version, packet, payload) };
  render(<PreservedReviewContext receiptId="saved" inputOrigin="MANUAL_IMPORT_UNVERIFIED" scope={{}} extraction={null} result={abstractOnlyDraft} validation={validation}/>);
  expect(screen.queryByText(legacyLevelExplanation)).toBeNull();
  const raw = screen.getByText("당시 형식·근거 연결 검사 기록").closest("details")!.querySelector("pre")!;
  expect(JSON.parse(raw.textContent!)).toEqual(validation);
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it.each(["V1", "V2", "V3"] as const)("shows a collapsed preserved %s execution contract with distinct requested and reported targets without claiming live or scientific proof", async version => {
  const validation = { status: "accepted", prepared_execution: executionVersion(version) };
  render(<FrozenReviewInputView value={{ schema_version: "FROZEN_REVIEW_INPUT_V1", selected_component_ids: ["/selected_text_excerpts/0/text"], receipt: { ...detail, status: "accepted", validation, packet: { ...detail.packet, input_scope: summary.input_scope } } }}/>);
  const toggle = screen.getByText("보존된 실행 요청·응답 출처");
  const provenance = toggle.closest("details")!;
  expect(provenance.open).toBe(false);
  // user-event's Enter behavior omits summary activation. The real-backend
  // browser journey verifies native Enter on this same disclosure.
  toggle.focus(); expect(document.activeElement).toBe(toggle);
  await userEvent.setup().click(toggle);
  expect(provenance.open).toBe(true);
  expect(within(provenance).getByText("요청 Provider: xrml_offline_review")).toBeTruthy();
  expect(within(provenance).getByText("요청 모델: requested-synthetic-v1")).toBeTruthy();
  expect(within(provenance).getByText("응답에 기록된 모델: reported-synthetic-alias")).toBeTruthy();
  expect(within(provenance).getByText("응답 식별자: synthetic-response-one")).toBeTruthy();
  expect(within(provenance).getByText(`요청 SHA-256: ${"1".repeat(64)}`)).toBeTruthy();
  expect(within(provenance).getByText(`계약 SHA-256: ${"2".repeat(64)}`)).toBeTruthy();
  expect(within(provenance).getByText(`응답 원문 SHA-256: ${"3".repeat(64)}`)).toBeTruthy();
  expect(within(provenance).getByText("실제 모델 신원: 독립 검증 안 됨 · 실제 외부 실행: 독립 검증 안 됨")).toBeTruthy();
  expect(within(provenance).getByText("과학적 뒷받침: 미평가 · 전체 문서 검토: 확인되지 않음 · 사람의 채택: 확인되지 않음")).toBeTruthy();
  expect(within(provenance).getByText(version === "V1" ? /새 V2의 필수 서술·배열 항목 공백 검사를 이 과거 기록에 소급 적용하지 않습니다/ : /필수 서술과 배열 항목이 공백뿐인 응답을 거부합니다/)).toBeTruthy();
  if (version === "V2") expect(within(provenance).getByText(/내용의 충실도나 과학적 타당성 평가가 아닙니다/)).toBeTruthy();
  if (version === "V3") {
    expect(within(provenance).getByText(/본문 구간에 근거한 검토와 제공된 근거 구간 인용 1개 이상을 요구합니다/)).toBeTruthy();
    expect(within(provenance).getByText(/이 계약은 논문 전체 검토나 과학적 타당성 검증을 뜻하지 않습니다/)).toBeTruthy();
  } else expect(within(provenance).queryByText(/제공된 근거 구간 인용 1개 이상을 요구합니다/)).toBeNull();
  expect(within(provenance).queryByText(/fixture instructions|preserved full input/)).toBeNull();
  expect(within(provenance).queryByText(legacyLevelExplanation)).toBeNull();
  const raw = screen.getByText("당시 형식·근거 연결 검사 기록").closest("details")!;
  expect(JSON.parse(raw.querySelector("pre")!.textContent!)).toEqual(validation);
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it.each([
  ["V1", "V2", "V2"], ["V2", "V1", "V1"], ["V1", "V1", "V2"], ["V2", "V2", "V1"],
  ["V2", "UNKNOWN", "V2"], ["V2", "V2", "UNKNOWN"],
  ["V1", "V3", "V3"], ["V2", "V3", "V3"], ["V3", "V1", "V1"], ["V3", "V2", "V2"],
  ["V3", "V3", "V1"], ["V3", "V3", "V2"], ["V3", "UNKNOWN", "V3"], ["V3", "V3", "UNKNOWN"],
])("leaves result %s / request %s / draft %s provenance unreadable without rewriting stored JSON", (resultVersion, requestVersion, draftVersion) => {
  const proof = { ...preparedExecution, schema_version: `PREPARED_REVIEW_RESULT_${resultVersion}`, contract: { ...preparedExecution.contract, schema_version: `PREPARED_REVIEW_REQUEST_${requestVersion}`, provider_request: { ...preparedExecution.contract.provider_request, schema_name: `PREPARED_PAPER_REVIEW_DRAFT_${draftVersion}` } } };
  const validation = { prepared_execution: proof };
  render(<PreservedReviewContext receiptId="mixed" inputOrigin="MANUAL_IMPORT_UNVERIFIED" scope={{}} extraction={null} result={detail.draft} validation={validation}/>);
  expect(screen.getByText("보존된 실행 요청·응답 출처를 해석할 수 없습니다.")).toBeTruthy();
  expect(screen.queryByText(/요청 Provider:|요청 모델:|보존된 V[123] 검사 계약/)).toBeNull();
  const raw = screen.getByText("당시 형식·근거 연결 검사 기록").closest("details")!.querySelector("pre")!;
  expect(JSON.parse(raw.textContent!)).toEqual(validation);
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it.each(["V1", "V2"] as const)("keeps a %s abstract-only historical result and empty citations unchanged without applying the V3 rule", version => {
  const draft = { ...detail.draft, review_level: "abstract_only", cited_evidence_span_ids: [] };
  const rawText = JSON.stringify(draft, null, 2);
  const proof = { ...executionVersion(version), response: { ...preparedExecution.response, raw_text: rawText } };
  const validation = { prepared_execution: proof };
  const original = JSON.stringify(validation);
  render(<FrozenReviewInputView value={{ schema_version: "FROZEN_REVIEW_INPUT_V1", selected_component_ids: ["/selected_text_excerpts/0/text"], receipt: { ...detail, draft, validation } }}/>);
  expect(screen.queryByText("보존된 실행 요청·응답 출처를 해석할 수 없습니다.")).toBeNull();
  expect(screen.queryByText(/제공된 근거 구간 인용 1개 이상을 요구합니다/)).toBeNull();
  expect(screen.queryByText(legacyLevelExplanation)).toBeNull();
  const record = screen.getByText("고정한 검토 기록 원문(JSON)").closest("details")!.querySelector("pre")!;
  const receipt = JSON.parse(record.textContent!);
  expect(receipt.draft).toEqual(draft);
  expect(receipt.validation.prepared_execution.response.raw_text).toBe(rawText);
  expect(JSON.stringify(validation)).toBe(original);
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("preserves a V1 historical whitespace response without applying the new V2 content rule", () => {
  const draft = { ...detail.draft, contribution_summary: " \t\n", limitations: ["  ", "Original limitation"] };
  const rawText = JSON.stringify(draft, null, 2);
  const proof = { ...preparedExecution, response: { ...preparedExecution.response, raw_text: rawText } };
  const validation = { prepared_execution: proof };
  const original = JSON.stringify(validation);
  render(<FrozenReviewInputView value={{ schema_version: "FROZEN_REVIEW_INPUT_V1", selected_component_ids: ["/selected_text_excerpts/0/text"], receipt: { ...detail, draft, validation } }}/>);
  expect(screen.getByText(/새 V2의 필수 서술·배열 항목 공백 검사를 이 과거 기록에 소급 적용하지 않습니다/)).toBeTruthy();
  const record = screen.getByText("고정한 검토 기록 원문(JSON)").closest("details")!.querySelector("pre")!;
  const receipt = JSON.parse(record.textContent!);
  expect(receipt.draft).toEqual(draft);
  expect(receipt.validation.prepared_execution.response.raw_text).toBe(rawText);
  expect(JSON.stringify(validation)).toBe(original);
  expect(screen.queryByText("보존된 실행 요청·응답 출처를 해석할 수 없습니다.")).toBeNull();
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("reads a V2 Journal execution summary without inventing full request or response contents", () => {
  const proof = { schema_version: "PREPARED_REVIEW_RESULT_SUMMARY_V1", request_sha256: "1".repeat(64), contract_sha256: "2".repeat(64), raw_text_sha256: "3".repeat(64), requested: { provider_name: "codex_cli", model: "requested-model" }, reported: { provider_name: "codex_cli", model: "not_reported", response_id: null }, reported_model_identity: "NOT_VERIFIED", independent_live_proof: "NOT_VERIFIED", scientific_support: "NOT_ASSESSED", full_document_review: "NOT_ESTABLISHED", human_adoption: "NOT_ESTABLISHED" };
  const validation = { prepared_execution: proof };
  render(<JournalReviewInputContext value={{ schema_version: "JOURNAL_REVIEW_INPUT_V2", paper_version_id: "version", receipt_id: "receipt-one", input_origin: "PROVIDER_RESPONSE", run_id: "run", paper_id: "paper", review_level: "full_text_grounded", status: "needs_review", selected_component_ids: ["/evidence_spans/0/text"], input_scope: summary.input_scope, source_extraction: null, review_result: detail.draft, validation }}/>);
  const provenance = screen.getByText("보존된 실행 요청·응답 출처").closest("details")!;
  expect(provenance.open).toBe(false);
  expect(within(provenance).getByText("요청 모델: requested-model")).toBeTruthy();
  expect(within(provenance).getByText("응답에 기록된 모델: 미보고 (실제 모델 미확인)")).toBeTruthy();
  expect(within(provenance).getByText("응답 식별자: 미확인")).toBeTruthy();
  expect(within(provenance).getByText(/이 저널 입력에는 요청·응답의 식별 요약만 포함/)).toBeTruthy();
  expect(within(provenance).queryByText(/보존된 V[123] 검사 계약/)).toBeNull();
  expect(within(provenance).queryByText(legacyLevelExplanation)).toBeNull();
  const raw = screen.getByText("당시 형식·근거 연결 검사 기록").closest("details")!;
  expect(JSON.parse(raw.querySelector("pre")!.textContent!)).toEqual(validation);
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("keeps missing historical execution provenance absent and unknown shapes explicitly unreadable", () => {
  const props = { receiptId: "legacy", inputOrigin: "MANUAL_IMPORT_UNVERIFIED", scope: {}, extraction: null, result: detail.draft };
  const view = render(<PreservedReviewContext {...props}/>);
  expect(screen.queryByText("보존된 실행 요청·응답 출처")).toBeNull();
  expect(screen.queryByText(/요청 Provider:|요청 SHA-256:/)).toBeNull();
  view.rerender(<PreservedReviewContext {...props} validation={{ prepared_execution: { schema_version: "UNKNOWN", contract: preparedExecution.contract } }}/>);
  expect(screen.getByText(/보존된 실행 요청·응답 출처를 해석할 수 없습니다/)).toBeTruthy();
  expect(screen.queryByText(/요청 Provider:/)).toBeNull();
});

it.each(["V1", "V2", "V3"] as const)("reads prepared %s execution provenance from the ordinary saved receipt without an extra request or implicit action", async version => {
  const validation = { status: "accepted", prepared_execution: executionVersion(version) };
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?") ? list : { ...detail, validation }) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: /입력·결과 열기/ }));
  const receipt = await screen.findByRole("region", { name: "선택한 검토 입력·결과" });
  const provenance = within(receipt).getByText("보존된 실행 요청·응답 출처").closest("details")!;
  expect(provenance.open).toBe(false);
  fireEvent.click(within(provenance).getByText("보존된 실행 요청·응답 출처"));
  expect(within(provenance).getByText("요청 모델: requested-synthetic-v1")).toBeTruthy();
  expect(within(provenance).getByText("응답에 기록된 모델: reported-synthetic-alias")).toBeTruthy();
  const raw = within(receipt).getByText("당시 형식·근거 연결 검사 기록").closest("details")!;
  expect(JSON.parse(raw.querySelector("pre")!.textContent!)).toEqual(validation);
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});
