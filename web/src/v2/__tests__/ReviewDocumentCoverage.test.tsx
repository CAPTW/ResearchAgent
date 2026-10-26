import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { DocumentCoverageRecord, PdfExtractionRecord } from "../../lib/reviewPreparation";
import { ReviewDocumentCoverage } from "../components/ReviewDocumentCoverage";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/paper-bank/version/review-preparations";
const source = { run_id: "run", paper_id: "paper", source_digest: "a".repeat(64), source_chars: 20, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 40, artifact_ref: "run:extractions/extract-one/document.txt", extraction_truncation: "NOT_OBSERVED" };
const extraction: PdfExtractionRecord = { schema_version: "PAPER_PDF_EXTRACTION_V1", extraction_id: "extract-one", topic_id: "topic", paper_version_id: "version", created_at: "2026-10-15T09:00:00Z", created_by: "owner", policy_version: "LOCAL_PDF_TEXT_V1", source,
  pdf: { sha256: "c".repeat(64), bytes: 3000, artifact_ref: "run:raw/paper.pdf" },
  provenance: { schema_version: "PDF_EXTRACTION_PROVENANCE_V1", source_pdf: { sha256: "c".repeat(64), bytes: 3000 }, extractor: { name: "pymupdf", version: "fixture", normalization: "XRML_WHITESPACE_V1" }, max_chars: 1000000, document_page_count: 3, visited_page_numbers: [1, 2, 3], retained_page_numbers: [1, 3], truncation: "NOT_OBSERVED", document_text_chars: 20, outputs: { text_sha256: source.artifact_sha256, text_bytes: 40, pages_sha256: "d".repeat(64), pages_bytes: 300 } },
  page_ranges: [{ page_number: 1, start: 0, end: 10, chars: 10 }, { page_number: 3, start: 11, end: 20, chars: 9 }], no_text_page_numbers: [2], unvisited_page_numbers: [], execution: { started: false, full_document_review: "NOT_ESTABLISHED" }, scientific_support: "NOT_ASSESSED" };
const original = { preparation_id: "prep-old", start: 3, end: 8, source: { ...source, source_digest: "e".repeat(64), source_chars: 1000 }, selection: { start: 100, end: 105, chars: 5, sha256: "f".repeat(64) }, mapping: "EXACT_UNIQUE_TEXT_MATCH" as const };
const returned = { ...original, receipt_id: "receipt-one", class: "BODY_GROUNDED" as const, subtype: "NATIVE_ANTHROPIC_V2" as const };
const covered = [{ start: 3, end: 8 }], gaps = [{ start: 0, end: 3 }, { start: 8, end: 20 }];
const empty = { record_count: 0, covered_chars: 0, ranges: [], records: [] };
const value: DocumentCoverageRecord = { schema_version: "PAPER_REVIEW_DOCUMENT_COVERAGE_V1", extraction, coordinate_space: "NORMALIZED_EXTRACTION_CODEPOINTS", normalization: "XRML_WHITESPACE_V1",
  prepared: { record_count: 1, covered_chars: 5, ranges: covered, gaps, records: [original] },
  results: { validated_returned: { record_count: 1, covered_chars: 5, ranges: covered, gaps, records: [returned] }, body_grounded: { record_count: 1, covered_chars: 5, ranges: covered, gaps, records: [returned] }, other_returned: empty, unverified_provider: empty, gap_semantics: "NO_VALIDATED_SAVED_RESULT", attempt_history_included: false },
  pages: [{ ...extraction.page_ranges[0], text_status: "TEXT_EXTRACTED", prepared_chars: 5, body_result_chars: 5, body_result_ranges: covered, gaps: [{ start: 0, end: 3 }, { start: 8, end: 10 }] }, { ...extraction.page_ranges[1], text_status: "TEXT_EXTRACTED", prepared_chars: 0, body_result_chars: 0, body_result_ranges: [], gaps: [{ start: 11, end: 20 }] }],
  no_text_page_numbers: [2], unvisited_page_numbers: [], unaligned: { preparations: [{ preparation_id: "ambiguous", source, selection: original.selection, reason: "TEXT_AMBIGUOUS" }], receipts: [{ preparation_id: null, receipt_id: "legacy", reason: "LEGACY_BINDING_UNAVAILABLE" }] },
  full_document_review: "NOT_ESTABLISHED", scientific_support: "NOT_ASSESSED", alignment_claim: "EXACT_NORMALIZED_TEXT_ONLY_NOT_PDF_IDENTITY" };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockResolvedValue(value); });
it("offers a bounded analysis request directly from a saved-result gap", async () => {
  render(<ReviewDocumentCoverage root={root} record={extraction} gapPreparation={{ busy: false, disabledReason: "", selectionLimit: 12000, onBusyChange: vi.fn(), onUseRange: vi.fn() }}/>);
  await open();
  fireEvent.click(screen.getByRole("button", { name: "선택한 본문 결과 없는 구간 열기" }));
  expect(screen.getByLabelText("분석 요청 시작 위치")).toHaveProperty("value", "0");
  expect(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)")).toHaveProperty("value", "3");
  expect(screen.getByRole("button", { name: "선택 구간 분석 요청 준비" })).toHaveProperty("disabled", false);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, init]) => init === undefined)).toBe(true);
});
async function open() { fireEvent.click(screen.getByText("PDF 전체 추출 텍스트의 결과 범위")); return screen.findByText("준비 기록 1건 · 중복 제외 5자"); }

it("loads explicitly and separates original input, exact text alignment, body results, pages without text and unmatched history", async () => {
  const prep = vi.fn(), receipt = vi.fn(); render(<ReviewDocumentCoverage root={root} record={extraction} onOpenPreparation={prep} onOpenReceipt={receipt}/>);
  expect(api.fetchJson).not.toHaveBeenCalled(); await open(); expect(api.fetchJson).toHaveBeenCalledWith(`${root}/pdf-extractions/extract-one/coverage`);
  expect(screen.getByText("저장된 본문 결과가 없는 범위: 0–3, 8–20")).toBeTruthy(); expect(screen.getByText(/같은 PDF라는 입증이 아니며/)).toBeTruthy();
  expect(screen.getByText(/모델이 실제로 읽었다는 별도 입증이나 문장의 과학적 타당성 평가가 아닙니다/)).toBeTruthy();
  fireEvent.click(screen.getByText("원래 준비 기록과 대응 좌표")); expect(screen.getByText("추출 좌표: 3–8 · 원래 입력: 100–105")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "대응 준비 기록 열기 · prep-old" })); expect(prep).toHaveBeenCalledWith("prep-old");
  const body = screen.getByRole("region", { name: "본문에 연결된 구조화 저장 결과" }); fireEvent.click(within(body).getByText("본문에 연결된 구조화 저장 결과 원래 입력 범위"));
  fireEvent.click(await within(body).findByRole("button", { name: "결과 입력 열기 · receipt-one" })); expect(receipt).toHaveBeenCalledWith("receipt-one");
  fireEvent.click(screen.getByText("페이지별 보존 텍스트와 결과 범위")); expect(screen.getByText("3페이지 · 텍스트 11–20 · 9자")).toBeTruthy(); expect(screen.getByText(/방문했으나 보존 텍스트 없음: 2/)).toBeTruthy();
  expect(screen.getByText(/동일 텍스트가 여러 곳에 있어/)).toBeTruthy(); expect(screen.getByText(/보존된 구간 연결 계약 없음/)).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, init]) => init === undefined)).toBe(true);
});

it("removes totals on unavailable source and refreshes with GET only instead of presenting zero coverage", async () => {
  render(<ReviewDocumentCoverage root={root} record={extraction}/>); await open(); vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("review_preparation_source_changed"));
  fireEvent.click(screen.getByRole("button", { name: "PDF 결과 범위 새로고침" })); expect((await screen.findByRole("alert")).textContent).toContain("현재 범위를 0으로 표시하지 않았습니다");
  expect(screen.queryByText("준비 기록 1건 · 중복 제외 5자")).toBeNull(); expect(screen.queryByRole("region", { name: "저장된 결과 범위" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "PDF 결과 범위 새로고침" })); await screen.findByText("준비 기록 1건 · 중복 제외 5자");
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, init]) => init === undefined)).toBe(true);
});

it.each([
  ["other extraction", { ...value, extraction: { ...extraction, extraction_id: "other" } }],
  ["scientific claim", { ...value, scientific_support: "VERIFIED" }],
  ["wrong total", { ...value, prepared: { ...value.prepared, covered_chars: 0 } }],
  ["invalid page", { ...value, pages: [{ ...value.pages[0], end: 21 }] }],
])("does not publish malformed %s coverage", async (_name, response) => {
  vi.mocked(api.fetchJson).mockResolvedValue(response); render(<ReviewDocumentCoverage root={root} record={extraction}/>); fireEvent.click(screen.getByText("PDF 전체 추출 텍스트의 결과 범위"));
  await screen.findByRole("alert"); expect(screen.queryByText("준비 기록 1건 · 중복 제외 5자")).toBeNull(); expect(screen.queryByRole("region", { name: "저장된 결과 범위" })).toBeNull();
});

it("ignores a late old-extraction response after selection changes", async () => {
  let resolve!: (value: unknown) => void; vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(done => { resolve = done; }) as never);
  const view = render(<ReviewDocumentCoverage root={root} record={extraction}/>); fireEvent.click(screen.getByText("PDF 전체 추출 텍스트의 결과 범위")); await screen.findByText("PDF 결과 범위 확인 중");
  view.rerender(<ReviewDocumentCoverage root="/other" record={{ ...extraction, extraction_id: "other" }}/>); await act(async () => resolve(value));
  expect(screen.queryByText("준비 기록 1건 · 중복 제외 5자")).toBeNull(); expect(api.fetchJson).toHaveBeenCalledTimes(1);
});

it("previews a missing saved-result gap and applies Unicode selection only to the local request draft", async () => {
  const apply = vi.fn(), busy = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => path.endsWith("/preview") ? {
    schema_version: "PAPER_REVIEW_PREVIEW_V1", source, window: { start: 0, end: 3, chars: 3, text: "e\u0301🙂" }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT", state: "NOT_EXECUTED",
  } as never : value as never);
  render(<ReviewDocumentCoverage root={root} record={extraction} gapPreparation={{ busy: false, disabledReason: "", selectionLimit: 12000, onBusyChange: busy, onUseRange: apply }}/>); await open();
  fireEvent.click(screen.getByRole("button", { name: "선택한 본문 결과 없는 구간 열기" })); expect(apply).not.toHaveBeenCalled();
  expect(screen.getByText(/선택한 결과 미보존 범위 0–3/)).toBeTruthy(); fireEvent.click(screen.getByRole("button", { name: "본문 미리보기" }));
  const preview = await screen.findByDisplayValue("e\u0301🙂") as HTMLTextAreaElement; expect(apply).not.toHaveBeenCalled();
  preview.setSelectionRange(2, 4); fireEvent.select(preview); const button = screen.getByRole("button", { name: "선택한 본문을 분석 요청 범위로 사용" }); button.focus(); await userEvent.setup().keyboard("{Enter}");
  expect(screen.getByLabelText("분석 요청 시작 위치")).toHaveProperty("value", "2");
  expect(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)")).toHaveProperty("value", "3");
  expect(apply).not.toHaveBeenCalled();
  const posts = vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST"); expect(posts).toHaveLength(1); expect(posts[0][0]).toBe(`${root}/preview`);
  expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({ expected_source_digest: source.source_digest, start: 0, length: 3 });
});

it("honors pending preparation guards for gap actions without suppressing read-only coverage", async () => {
  const apply = vi.fn(); render(<ReviewDocumentCoverage root={root} record={extraction} gapPreparation={{ busy: false, disabledReason: "같은 저장 요청을 먼저 확인하세요.", selectionLimit: 12000, onBusyChange: vi.fn(), onUseRange: apply }}/>); await open();
  expect(screen.getByRole("button", { name: "선택한 본문 결과 없는 구간 열기" })).toHaveProperty("disabled", true); expect(screen.getByText("같은 저장 요청을 먼저 확인하세요.")).toBeTruthy();
  expect(apply).not.toHaveBeenCalled(); expect(vi.mocked(api.fetchJson).mock.calls.every(([, init]) => init === undefined)).toBe(true);
});

async function planner(onOpenPreparation = vi.fn()) {
  const view = render(<ReviewDocumentCoverage root={root} record={extraction} onOpenPreparation={onOpenPreparation} gapPreparation={{ busy: false, disabledReason: "", selectionLimit: 12000, onBusyChange: vi.fn(), onUseRange: vi.fn() }}/>);
  await open(); fireEvent.click(screen.getByRole("button", { name: "선택한 본문 결과 없는 구간 열기" }));
  return { view, onOpenPreparation, submit: screen.getByRole("button", { name: "선택 구간 분석 요청 준비" }) };
}
const writes = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");
const saved = { schema_version: "PAPER_REVIEW_PREPARATION_V2", preparation_id: "new-gap", topic_id: "topic", paper_version_id: "version", state: "PREPARED_NOT_EXECUTED", source, selection: { start: 0, end: 3, chars: 3, text: "e\u0301🙂", sha256: "1".repeat(64) }, execution: { started: false, full_document_review: "NOT_ESTABLISHED" } };

it("rechecks coverage and explicitly prepares the selected input with one keyboard submission", async () => {
  const { submit, onOpenPreparation } = await planner();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? saved : value) as never);
  submit.focus(); await userEvent.setup().keyboard("{Enter}");
  await screen.findByText(/분석 요청 준비를 저장했습니다/);
  expect(writes()).toHaveLength(1);
  expect(writes()[0][0]).toBe(root);
  expect(JSON.parse(String(writes()[0][1]?.body))).toEqual({ request_id: expect.any(String), run_id: source.run_id, paper_id: source.paper_id, expected_source_digest: source.source_digest, start: 0, end: 3 });
  expect(onOpenPreparation).toHaveBeenCalledExactlyOnceWith("new-gap");
  expect(submit).toHaveProperty("disabled", true);
  expect(vi.mocked(api.fetchJson).mock.calls.map(([path]) => path)).toEqual([`${root}/pdf-extractions/extract-one/coverage`, `${root}/pdf-extractions/extract-one/coverage`, root]);
});

it("preserves the same request after an unknown ACK and replays without a new coverage preflight", async () => {
  const { submit } = await planner();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => { if (init?.method === "POST") throw new Error("lost ACK"); return value as never; });
  fireEvent.click(submit); fireEvent.click(submit);
  const retry = await screen.findByRole("button", { name: "같은 분석 준비 요청 다시 확인" });
  expect(writes()).toHaveLength(1);
  expect(screen.getByLabelText("분석 요청 시작 위치").matches(":disabled")).toBe(true);
  expect(screen.getByRole("button", { name: "본문 결과 없는 구간 닫기" })).toHaveProperty("disabled", true);
  const disclosure = screen.getByText("PDF 전체 추출 텍스트의 결과 범위").closest("details")!;
  await act(async () => { disclosure.open = false; fireEvent(disclosure, new Event("toggle")); });
  await act(async () => { disclosure.open = true; fireEvent(disclosure, new Event("toggle")); });
  expect(await screen.findByRole("button", { name: "같은 분석 준비 요청 다시 확인" })).toBe(retry);
  const count = vi.mocked(api.fetchJson).mock.calls.length;
  vi.mocked(api.fetchJson).mockResolvedValue(saved as never);
  fireEvent.click(retry); await screen.findByText(/분석 요청 준비를 저장했습니다/);
  expect(writes()).toHaveLength(2); expect(writes()[0]).toEqual(writes()[1]);
  expect(api.fetchJson).toHaveBeenCalledTimes(count + 1);
});

it("does not post when a result now covers the selected gap, preserving the draft", async () => {
  const { submit } = await planner();
  const current = structuredClone(value);
  current.results.body_grounded = { ...current.results.body_grounded, covered_chars: 8, ranges: [{ start: 0, end: 8 }], gaps: [{ start: 8, end: 20 }] };
  vi.mocked(api.fetchJson).mockResolvedValue(current);
  fireEvent.click(submit); await screen.findByText(/선택한 구간의 저장 결과 범위가 바뀌었습니다/);
  expect(writes()).toHaveLength(0); expect(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)")).toHaveProperty("value", "3");
});

it("requires another explicit submission after newly discovered overlapping preparations", async () => {
  const { submit, onOpenPreparation } = await planner();
  const current = structuredClone(value);
  current.prepared = { record_count: 2, covered_chars: 7, ranges: [{ start: 0, end: 2 }, ...covered], gaps: [{ start: 2, end: 3 }, { start: 8, end: 20 }], records: [{ ...original, preparation_id: "already-prepared", start: 0, end: 2 }, original] };
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? saved : current) as never);
  fireEvent.click(submit); await screen.findByText(/겹치는 준비 기록이 새로 발견됐습니다/);
  expect(writes()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "기존 준비 기록 열기 · already-prepared" }));
  expect(onOpenPreparation).toHaveBeenCalledWith("already-prepared"); expect(writes()).toHaveLength(0);
  fireEvent.click(submit); await screen.findByText(/분석 요청 준비를 저장했습니다/);
  expect(writes()).toHaveLength(1);
});

it("keeps a preflight failure retryable without sending a save request", async () => {
  const { submit } = await planner(); vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("coverage unavailable"));
  fireEvent.click(submit); await screen.findByText(/coverage unavailable/); expect(writes()).toHaveLength(0);
  expect(submit).toHaveProperty("disabled", false); expect(screen.queryByText(/저장 응답 미확정/)).toBeNull();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? saved : value) as never);
  fireEvent.click(submit); await screen.findByText(/분석 요청 준비를 저장했습니다/);
});

it("keeps definite source conflict visible and never automatically retries", async () => {
  const { submit } = await planner();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => { if (init?.method === "POST") throw new api.ApiError("review_preparation_source_changed", 409, ""); return value as never; });
  fireEvent.click(submit); await screen.findByText(/현재 추출 출처를 확인할 수 없습니다/);
  expect(writes()).toHaveLength(1); expect(submit).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)")).toHaveProperty("value", "3");
  expect(screen.queryByRole("button", { name: "같은 분석 준비 요청 다시 확인" })).toBeNull();
});

it("does not save after leaving the topic during the read-only preflight", async () => {
  const { submit, view, onOpenPreparation } = await planner();
  let resolve!: (result: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(done => { resolve = done; }) as never);
  fireEvent.click(submit); await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(2));
  view.rerender(<ReviewDocumentCoverage root="/another-topic" record={{ ...extraction, topic_id: "another" }}/>);
  await act(async () => resolve(value)); expect(writes()).toHaveLength(0); expect(onOpenPreparation).not.toHaveBeenCalled();
});

it("rejects out-of-gap input and closes the draft without writing", async () => {
  const { submit } = await planner();
  fireEvent.change(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)"), { target: { value: "4" } });
  expect(submit).toHaveProperty("disabled", true); expect(screen.getByRole("alert").textContent).toContain("미보존 범위 안");
  fireEvent.click(screen.getByRole("button", { name: "본문 결과 없는 구간 닫기" }));
  expect(screen.queryByRole("form", { name: "결과 없는 구간 분석 요청 준비" })).toBeNull(); expect(writes()).toHaveLength(0);
});

it("bounds each request by the advertised limit and advances only on an explicit action", async () => {
  render(<ReviewDocumentCoverage root={root} record={extraction} gapPreparation={{ busy: false, disabledReason: "", selectionLimit: 2, onBusyChange: vi.fn(), onUseRange: vi.fn() }}/>);
  await open(); fireEvent.click(screen.getByRole("button", { name: "선택한 본문 결과 없는 구간 열기" }));
  expect(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)")).toHaveProperty("value", "2");
  fireEvent.change(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)"), { target: { value: "3" } });
  expect(screen.getByRole("button", { name: "선택 구간 분석 요청 준비" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "다음 같은 길이 구간 선택" }));
  expect(screen.getByLabelText("분석 요청 시작 위치")).toHaveProperty("value", "2");
  expect(screen.getByLabelText("분석 요청 끝 위치 (포함하지 않음)")).toHaveProperty("value", "3");
  expect(screen.getByRole("button", { name: "다음 같은 길이 구간 선택" })).toHaveProperty("disabled", true);
  expect(writes()).toHaveLength(0);
});

it("treats an unrelated saved response as unknown and allows only exact replay", async () => {
  const { submit, onOpenPreparation } = await planner();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? { ...saved, topic_id: "other-topic" } : value) as never);
  fireEvent.click(submit); await screen.findByRole("button", { name: "같은 분석 준비 요청 다시 확인" });
  expect(onOpenPreparation).not.toHaveBeenCalled(); expect(screen.queryByText(/분석 요청 준비를 저장했습니다/)).toBeNull();
  expect(writes()).toHaveLength(1);
});
