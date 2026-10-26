import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewPreparation } from "../components/ReviewPreparation";
import { PaperPdfExtraction } from "../components/PaperPdfExtraction";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/paper-bank/version/review-preparations";
it("prevents replacing the extraction while a gap preparation request is pending", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...inventory, extractions: [extracted] } as never);
  const controls = { busy: false, disabledReason: "", selectionLimit: 12000, onBusyChange: vi.fn(), onUseRange: vi.fn() };
  const view = render(<PaperPdfExtraction root={root} topicId="topic" paperVersionId="version" gapPreparation={controls}/>);
  fireEvent.click(screen.getByText("PDF 전체 텍스트 추출·범위 확인"));
  const open = await screen.findByRole("button", { name: "PDF 추출 기록 열기 · extract-one" });
  view.rerender(<PaperPdfExtraction root={root} topicId="topic" paperVersionId="version" gapPreparation={{ ...controls, busy: true }}/>);
  expect(open).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "PDF·추출 이력 새로고침" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("추출할 로컬 PDF")).toHaveProperty("disabled", true);
  fireEvent.click(open); expect(api.fetchJson).toHaveBeenCalledTimes(1);
  view.rerender(<PaperPdfExtraction root={root} topicId="topic" paperVersionId="version" gapPreparation={controls}/>);
  expect(open).toHaveProperty("disabled", false);
});
const legacy = { run_id: "run", paper_id: "paper", source_digest: "a".repeat(64), source_chars: 20000, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 24000, extraction_truncation: "TRUNCATED" };
const sources = { sources: [legacy], limits: { max_selected_chars: 12000 }, state: "NOT_EXECUTED" };
const pdf = { run_id: "run", paper_id: "paper", pdf_sha256: "c".repeat(64), pdf_bytes: 12000 };
const extracted = { schema_version: "PAPER_PDF_EXTRACTION_V1", extraction_id: "extract-one", topic_id: "topic", paper_version_id: "version", created_by: "owner", created_at: "2026-10-15T09:00:00Z", policy_version: "LOCAL_PDF_TEXT_V1",
  pdf: { sha256: pdf.pdf_sha256, bytes: pdf.pdf_bytes, artifact_ref: "run:raw/paper.pdf" },
  source: { ...legacy, source_digest: "d".repeat(64), source_chars: 20, artifact_bytes: 40, artifact_ref: "run:extractions/extract-one/document.txt", extraction_truncation: "NOT_OBSERVED" },
  provenance: { schema_version: "PDF_EXTRACTION_PROVENANCE_V1", source_pdf: { sha256: pdf.pdf_sha256, bytes: pdf.pdf_bytes }, extractor: { name: "pymupdf", version: "fixture", normalization: "XRML_WHITESPACE_V1" }, max_chars: 1000000, document_page_count: 3, visited_page_numbers: [1, 2, 3], retained_page_numbers: [1, 3], truncation: "NOT_OBSERVED", document_text_chars: 20, outputs: { text_sha256: legacy.artifact_sha256, text_bytes: 40, pages_sha256: "e".repeat(64), pages_bytes: 500 } },
  page_ranges: [{ page_number: 1, start: 0, end: 10, chars: 10 }, { page_number: 3, start: 11, end: 20, chars: 9 }], no_text_page_numbers: [2], unvisited_page_numbers: [], execution: { started: false, full_document_review: "NOT_ESTABLISHED" }, scientific_support: "NOT_ASSESSED" };
const inventory = { schema_version: "PAPER_PDF_EXTRACTION_INVENTORY_V1", pdfs: [pdf], extractions: [], unavailable: [], limits: { max_pdf_bytes: 25000000, max_source_chars: 1000000, max_artifact_bytes: 4194304, max_document_pages: 2000 }, state: "NOT_EXECUTED" };
const listing = { items: [], hasMore: false, page: 1, pageSize: 10 };
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");
async function openExtraction() {
  fireEvent.click(screen.getByText("본문 구간 검토 준비")); await screen.findByRole("combobox", { name: "저장 본문 출처" });
  fireEvent.click(screen.getByText("PDF 전체 텍스트 추출·범위 확인"));
  return screen.findByRole("region", { name: "로컬 PDF 추출" });
}
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/sources") ? sources : path.includes("?") ? { items: [], hasMore: false, page: 1, pageSize: 10 } : { schema_version: "PAPER_PDF_EXTRACTION_INVENTORY_V1", pdfs: [], extractions: [], unavailable: [], limits: { max_pdf_bytes: 25000000, max_source_chars: 1000000, max_artifact_bytes: 4194304, max_document_pages: 2000 }, state: "NOT_EXECUTED" }) as never); });

it("offers explicit local whole-PDF extraction without eagerly reading PDFs or replacing the selected range", async () => {
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  await screen.findByRole("combobox", { name: "저장 본문 출처" });
  fireEvent.change(screen.getByLabelText("시작 위치"), { target: { value: "100" } });
  fireEvent.change(screen.getByLabelText("끝 위치 (포함하지 않음)"), { target: { value: "123" } });
  expect(vi.mocked(api.fetchJson).mock.calls.some(([path]) => path.includes("/pdf-extractions"))).toBe(false);
  fireEvent.click(screen.getByText("PDF 전체 텍스트 추출·범위 확인"));
  await screen.findByText("이 논문에 연결된 로컬 PDF가 없습니다.");
  expect(api.fetchJson).toHaveBeenCalledWith(`${root}/pdf-extractions`);
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "123");
  expect(vi.mocked(api.fetchJson).mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

it("requires an explicit PDF choice and keyboard extraction, preserving unsaved coordinates and showing engine scope honestly", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => (init?.method === "POST" ? { extraction: extracted, created: true } : path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never);
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  const button = screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" }); expect(button).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("시작 위치"), { target: { value: "100" } }); fireEvent.change(screen.getByLabelText("끝 위치 (포함하지 않음)"), { target: { value: "500" } });
  await userEvent.setup().selectOptions(screen.getByLabelText("추출할 로컬 PDF"), JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]));
  button.focus(); await userEvent.setup().keyboard("{Enter}");
  const detail = await screen.findByRole("region", { name: "보존된 PDF 추출" });
  expect(within(detail).getByText(/방문했으나 보존 텍스트가 없는 페이지: 2/)).toBeTruthy();
  expect(within(detail).getByText(/빈 페이지로 판정한 것이 아니며/)).toBeTruthy();
  expect(within(detail).getByText(/모델 실행 안 함 · OCR 안 함 · 전체 문서 검토 미확인/)).toBeTruthy();
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100"); expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "500");
  expect(posts()).toHaveLength(1); expect(JSON.parse(String(posts()[0][1]?.body))).toEqual({ run_id: pdf.run_id, paper_id: pdf.paper_id, expected_pdf_sha256: pdf.pdf_sha256 });
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([path]) => path.endsWith("/sources"))).toHaveLength(1);
});

it("freezes the exact extraction after lost response and explicitly retries without preparing or switching the old source", async () => {
  let writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") { if (++writes === 1) throw new TypeError("lost response"); return { extraction: extracted, created: false } as never; }
    return (path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never;
  });
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  await userEvent.setup().selectOptions(screen.getByLabelText("추출할 로컬 PDF"), JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]));
  fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" })); const retry = await screen.findByRole("button", { name: "같은 PDF 추출 요청 다시 확인" });
  expect(posts()).toHaveLength(1); expect(screen.getByLabelText("추출할 로컬 PDF")).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "PDF·추출 이력 새로고침" })).toHaveProperty("disabled", true);
  fireEvent.click(retry); await screen.findByText(/동일 PDF의 보존 추출 기록을 확인했습니다/);
  expect(posts()).toHaveLength(2); expect(posts()[0][1]?.body).toBe(posts()[1][1]?.body);
  expect(screen.getByLabelText("저장 본문 출처")).toHaveProperty("value", JSON.stringify([legacy.run_id, legacy.paper_id]));
  expect(posts().every(([path]) => path.endsWith("/pdf-extractions"))).toBe(true);
});

it.each([
  ["topic", { ...extracted, topic_id: "other" }], ["paper", { ...extracted, paper_version_id: "other" }],
  ["PDF digest", { ...extracted, pdf: { ...extracted.pdf, sha256: "f".repeat(64) } }],
  ["source occurrence", { ...extracted, source: { ...extracted.source, run_id: "other" } }],
  ["execution", { ...extracted, execution: { ...extracted.execution, started: true } }],
  ["page range", { ...extracted, page_ranges: [{ ...extracted.page_ranges[0], end: 30 }] }],
])("does not promote a malformed or mismatching %s save acknowledgement", async (_name, result) => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => (init?.method === "POST" ? { extraction: result, created: true } : path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never);
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  fireEvent.change(screen.getByLabelText("추출할 로컬 PDF"), { target: { value: JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]) } });
  fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" }));
  await screen.findByRole("button", { name: "같은 PDF 추출 요청 다시 확인" });
  expect(screen.queryByRole("region", { name: "보존된 PDF 추출" })).toBeNull(); expect(posts()).toHaveLength(1);
});

it("requires explicit inventory refresh after a changed PDF conflict and keeps typed coordinates", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") throw new api.ApiError("review_pdf_source_changed", 409, "");
    return (path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never;
  });
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  fireEvent.change(screen.getByLabelText("시작 위치"), { target: { value: "100" } });
  fireEvent.change(screen.getByLabelText("추출할 로컬 PDF"), { target: { value: JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]) } });
  fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" })); await screen.findByText(/선택 당시와 PDF가 달라졌습니다/);
  expect(screen.queryByRole("button", { name: "같은 PDF 추출 요청 다시 확인" })).toBeNull(); expect(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100");
  fireEvent.click(screen.getByRole("button", { name: "PDF·추출 이력 새로고침" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" })).toHaveProperty("disabled", false)); expect(posts()).toHaveLength(1);
});

it("distinguishes inventory failure from no PDF and keeps already saved extraction readable without a current PDF", async () => {
  let inventoryReads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/pdf-extractions")) { if (++inventoryReads === 1) throw new api.ApiError("forbidden", 403, ""); return { ...inventory, pdfs: [], extractions: [extracted] } as never; }
    return (path.endsWith("/sources") ? sources : path.endsWith("/extract-one") ? extracted : listing) as never;
  });
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  expect((await screen.findByRole("alert")).textContent).toContain("권한"); expect(screen.queryByText("이 논문에 연결된 로컬 PDF가 없습니다.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "PDF·추출 이력 새로고침" }));
  fireEvent.click(await screen.findByRole("button", { name: "PDF 추출 기록 열기 · extract-one" }));
  await screen.findByRole("region", { name: "보존된 PDF 추출" }); expect(api.fetchJson).toHaveBeenCalledWith(`${root}/pdf-extractions/extract-one`); expect(posts()).toHaveLength(0);
});

it("keeps old and new extraction sources distinct and previews only the explicitly chosen digest beyond the old source limit", async () => {
  const next = { ...extracted.source, source_chars: 30000, extraction_id: extracted.extraction_id };
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/preview")) { const body = JSON.parse(String(init?.body)); return { schema_version: "PAPER_REVIEW_PREVIEW_V1", source: next, window: { start: body.start, end: body.start + 3, chars: 3, text: "e\u0301🙂" }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT", state: "NOT_EXECUTED" } as never; }
    return (path.endsWith("/sources") ? { ...sources, sources: [legacy, next] } : listing) as never;
  });
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  const select = await screen.findByLabelText("저장 본문 출처"); expect(select).toHaveProperty("value", JSON.stringify([legacy.run_id, legacy.paper_id]));
  fireEvent.change(select, { target: { value: JSON.stringify([next.run_id, next.paper_id, next.extraction_id]) } });
  fireEvent.change(screen.getByLabelText("미리보기 시작 위치"), { target: { value: "29997" } }); fireEvent.click(screen.getByRole("button", { name: "이 위치 미리보기" }));
  await screen.findByDisplayValue("e\u0301🙂"); expect(JSON.parse(String(posts()[0][1]?.body)).expected_source_digest).toBe(next.source_digest);
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(`${root}/preview`);
});

it("ignores a late extraction acknowledgement after topic change and does not auto-read the new topic", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation((path, init) => init?.method === "POST" ? new Promise(done => { resolve = done; }) as never : Promise.resolve(path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never);
  const view = render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  fireEvent.change(screen.getByLabelText("추출할 로컬 PDF"), { target: { value: JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]) } }); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" }));
  view.rerender(<ReviewPreparation topicId="other" paperVersionId="other"/>); await act(async () => resolve({ extraction: extracted, created: true }));
  expect(screen.queryByRole("region", { name: "보존된 PDF 추출" })).toBeNull(); expect(screen.queryByText(/PDF 텍스트 추출을 보존했습니다/)).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([path]) => path.startsWith(root))).toBe(true);
});

it.each(["TRUNCATED", "UNKNOWN"])("shows %s extraction observations without calling it complete", async state => {
  const result = { ...extracted, source: { ...extracted.source, extraction_truncation: state }, provenance: { ...extracted.provenance, truncation: state, document_page_count: null, document_text_chars: null }, unvisited_page_numbers: null };
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? { ...inventory, extractions: [result] } : path.endsWith("/extract-one") ? result : listing) as never);
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction(); fireEvent.click(await screen.findByRole("button", { name: "PDF 추출 기록 열기 · extract-one" }));
  const detail = await screen.findByRole("region", { name: "보존된 PDF 추출" }); expect(within(detail).getByText(state === "TRUNCATED" ? /추출 잘림 확인/ : /추출 범위 미확인/)).toBeTruthy();
  expect(within(detail).getByText("방문하지 않은 페이지: 미확인")).toBeTruthy(); expect(posts()).toHaveLength(0);
});

it("keeps an unresolved range save and its replay body intact when independent PDF extraction finishes", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path === root && init?.method === "POST") throw new TypeError("range response lost");
    if (path.endsWith("/pdf-extractions") && init?.method === "POST") return { extraction: extracted, created: true } as never;
    return (path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never;
  });
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  fireEvent.change(screen.getByLabelText("시작 위치"), { target: { value: "100" } }); fireEvent.change(screen.getByLabelText("끝 위치 (포함하지 않음)"), { target: { value: "500" } });
  fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" })); await screen.findByRole("button", { name: "같은 고정 요청 다시 보내기" });
  fireEvent.change(screen.getByLabelText("추출할 로컬 PDF"), { target: { value: JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]) } }); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" }));
  await screen.findByRole("region", { name: "보존된 PDF 추출" }); expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100"); expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "500");
  fireEvent.click(screen.getByRole("button", { name: "같은 고정 요청 다시 보내기" })); await screen.findByRole("button", { name: "같은 고정 요청 다시 보내기" });
  const saves = posts().filter(([path]) => path === root); expect(saves).toHaveLength(2); expect(saves[0][1]?.body).toBe(saves[1][1]?.body);
  expect(JSON.parse(String(saves[1][1]?.body))).toMatchObject({ expected_source_digest: legacy.source_digest, start: 100, end: 500 });
});

it.each([
  ["review_pdf_engine_unavailable", /로컬 PDF 추출 엔진\(PyMuPDF\)이 필요합니다/],
  ["review_pdf_encrypted", /암호화된 PDF는 로컬 추출을 지원하지 않습니다/],
  ["review_pdf_page_limit_exceeded", /PDF가 페이지 수 상한을 넘습니다/],
])("explains the bounded local %s failure without an automatic retry", async (code, expected) => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") throw new api.ApiError(String(code), 422, "");
    return (path.endsWith("/sources") ? sources : path.endsWith("/pdf-extractions") ? inventory : listing) as never;
  });
  render(<ReviewPreparation topicId="topic" paperVersionId="version"/>); await openExtraction();
  fireEvent.change(screen.getByLabelText("추출할 로컬 PDF"), { target: { value: JSON.stringify([pdf.run_id, pdf.paper_id, pdf.pdf_sha256]) } }); fireEvent.click(screen.getByRole("button", { name: "선택한 PDF 전체 텍스트 추출" }));
  expect((await screen.findByRole("alert")).textContent).toMatch(expected); expect(posts()).toHaveLength(1); expect(screen.queryByRole("button", { name: "같은 PDF 추출 요청 다시 확인" })).toBeNull();
});
