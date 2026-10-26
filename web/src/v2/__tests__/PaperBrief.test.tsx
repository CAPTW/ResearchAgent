import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { EvidenceLocator, PaperBrief } from "../components/PaperBrief";

const scope = { schema_version: "AI_REVIEW_INPUT_SCOPE_V2", source: { preserved_text_chars: 12 }, covered_stored_chars: 12 };
const preparation = { schema_version: "REVIEW_PREPARATION_BINDING_V2", source: { extraction_truncation: "UNKNOWN" }, selection: { start: 28000, end: 28012, chars: 12 } };
const result = { contribution_summary: "Saved e\u0301 interpretation", method_summary: "A method with a condition", evidence_summary: "Reported result, not independently checked", limitations: ["Selected input only"], relevance_to_topic: "A possible connection", follow_up_questions: ["Does it generalize?"], unsupported_claims: ["Unverified claim"], cited_evidence_span_ids: ["span-one"] };

it("keeps frozen interpretation and source separate, preserves exact Unicode, and performs no IO", () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("unexpected IO"));
  const packet = { result, scope, preparation, evidenceSpans: [{ span_id: "span-one", text: "Original e\u0301 quote", char_start: 0, char_end: 12, source_path: "run:raw/fixture.txt" }] };
  const before = JSON.stringify(packet);
  try {
    render(<PaperBrief {...packet}/>);
    expect(screen.getByRole("heading", { name: "논문 브리프 · 저장된 모델 해석" })).toBeTruthy();
    expect(screen.getByText("Saved e\u0301 interpretation").textContent).toBe(result.contribution_summary);
    const evidence = screen.getByRole("region", { name: "분석의 고정 근거" });
    expect(within(evidence).getByText(/문장별 연결이나 과학적 뒷받침은 확인되지 않았습니다/)).toBeTruthy();
    const summary = within(evidence).getByText("근거 1 · 응답에서 인용함");
    summary.focus(); expect(document.activeElement).toBe(summary);
    // jsdom lacks the native summary Enter default action; exercise its click
    // activation here and verify keyboard expansion in the browser separately.
    fireEvent.click(summary);
    expect(summary.closest("details")?.open).toBe(true);
    expect(within(evidence).getByLabelText("보존된 원문 발췌 1").textContent).toBe("Original e\u0301 quote");
    expect(within(evidence).getByText("선택 구간 내부 문자 위치: 0–12 (끝 위치 제외)")).toBeTruthy();
    expect(screen.getByText("선택한 원래 본문 좌표: 28000–28012 (정규화 본문, 끝 위치 제외)")).toBeTruthy();
    expect(JSON.stringify(packet)).toBe(before);
    expect(fetch).not.toHaveBeenCalled();
  } finally { fetch.mockRestore(); }
});

it("does not turn absent, blank or empty values into not-found or inapplicable claims", () => {
  render(<PaperBrief result={{ method_summary: " \n", limitations: [], follow_up_questions: ["", "Preserved question"], novelty_signal: "NOT_ASSESSED", evidence_summary: { unexpected: "value" } }}/>);
  expect(within(screen.getByRole("region", { name: "기여 요약" })).getByText("기록 없음")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "방법 요약" })).getByText("기록 없음")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "한계" })).getByText("기록 없음")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "근거 요약" })).getByText("기록 형식 미확인")).toBeTruthy();
  expect(screen.getByText(/원문에서 찾지 못했거나 해당 없음으로 판정하지 않습니다/)).toBeTruthy();
  fireEvent.click(screen.getByText("추가 해석과 근거가 부족한 주장"));
  expect(screen.getByText("미평가")).toBeTruthy();
  expect(screen.getByText("보존 입력: 범위 기록 미확인. 실제 모델이 읽은 분량이나 전체 논문 검토 완료율을 뜻하지 않습니다.")).toBeTruthy();
  expect(screen.queryByText(/0자/)).toBeNull();
});

it("reports explicit PDF truncation even when the selected text itself was not truncated", () => {
  const extraction = { schema_version: "REVIEW_EXTRACTION_V1", selection: { truncation: "NOT_OBSERVED" }, pdf_extraction: { schema_version: "PDF_EXTRACTION_PROVENANCE_V1", truncation: "TRUNCATED" } };
  const view = render(<PaperBrief result={result} scope={scope} extraction={extraction}/>);
  expect(screen.getByText("선택·추출 범위: 잘림 기록 있음")).toBeTruthy();
  view.rerender(<PaperBrief result={result} extraction={{ ...extraction, pdf_extraction: null }}/>);
  expect(screen.getByText("선택·추출 범위: 잘림 관측 없음 · 전체 확보는 미확인")).toBeTruthy();
  view.rerender(<PaperBrief result={result} extraction={{ ...extraction, schema_version: "UNRECOGNIZED" }}/>);
  expect(screen.getByText("선택·추출 범위: 잘림 여부 미확인")).toBeTruthy();
});

it("keeps invalid scope counts unknown and exposes existing locators without inventing a page or link", () => {
  render(<PaperBrief result={result} scope={{ ...scope, covered_stored_chars: 13 }} evidenceSpans={[{ span_id: "legacy", text: "<script>not executed</script>", locator: { page: 2 }, source_path: "https://example.invalid/paper" }, { span_id: "unknown", text: "Another quote", page_number: -1, char_start: 12, char_end: 0 }]}/>);
  expect(screen.getByText(/^보존 입력: 범위 기록 미확인/)).toBeTruthy();
  expect(screen.getByText('{"page":2}')).toBeTruthy();
  expect(screen.queryByText(/기록된 PDF 페이지:/)).toBeNull();
  expect(screen.getByText("발췌 위치 기록 없음")).toBeTruthy();
  expect(screen.getByText("<script>not executed</script>")).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull();
  expect(document.querySelector("script")).toBeNull();
});

it("renders supplied page and source coordinates with distinct labels", () => {
  render(<EvidenceLocator span={{ page_number: 4, char_start: 30, char_end: 90, source_path: "run:raw/example.pdf" }}/>);
  expect(screen.getByText("기록된 PDF 페이지: 4")).toBeTruthy();
  expect(screen.getByText("기록된 문자 위치: 30–90 (끝 위치 제외)")).toBeTruthy();
  expect(screen.getByText("기록된 파일 참조: run:raw/example.pdf")).toBeTruthy();
});
