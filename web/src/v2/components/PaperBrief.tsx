import { useId } from "react";
import "./PaperBrief.css";

type Props = {
  result: Record<string, unknown>;
  scope?: unknown;
  extraction?: unknown;
  preparation?: unknown;
  evidenceSpans?: unknown;
  inputOrigin?: string;
};
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const prose = { whiteSpace: "pre-wrap" as const, overflowWrap: "anywhere" as const };
const fields = [
  ["contribution_summary", "기여 요약"], ["method_summary", "방법 요약"], ["evidence_summary", "근거 요약"],
  ["limitations", "한계"], ["relevance_to_topic", "주제 관련성"], ["follow_up_questions", "후속 질문"],
] as const;
const additional = [["novelty_signal", "새로움에 대한 기록"], ["practical_implications", "활용 제안"], ["unsupported_claims", "근거가 부족한 주장"]] as const;

function Value({ value }: { value: unknown }) {
  if (typeof value === "string") return <p style={prose}>{!value.trim() ? "기록 없음" : value === "NOT_ASSESSED" ? "미평가" : value}</p>;
  if (Array.isArray(value) && value.every(item => typeof item === "string")) return value.length
    ? <ul>{value.map((item, index) => <li key={index} style={prose}>{item.trim() ? item : "기록 없음"}</li>)}</ul> : <p>기록 없음</p>;
  return <p>{value === undefined || value === null ? "기록 없음" : "기록 형식 미확인"}</p>;
}

function InputScope({ scope, extraction, preparation }: Pick<Props, "scope" | "extraction" | "preparation">) {
  const knownScope = object(scope) && ["AI_REVIEW_INPUT_SCOPE_V1", "AI_REVIEW_INPUT_SCOPE_V2"].includes(String(scope.schema_version)) ? scope : null;
  const source = knownScope && object(knownScope.source) ? knownScope.source : {};
  const chars = source.preserved_text_chars, covered = knownScope?.covered_stored_chars;
  const prepared = object(preparation) && ["REVIEW_PREPARATION_BINDING_V1", "REVIEW_PREPARATION_BINDING_V2", "REVIEW_PREPARATION_BINDING_V3"].includes(String(preparation.schema_version)) ? preparation : null;
  const original = prepared && object(prepared.selection) ? prepared.selection : {};
  const observed = object(extraction) && extraction.schema_version === "REVIEW_EXTRACTION_V1" ? extraction : null;
  const selection = observed && object(observed.selection) ? observed.selection : {};
  const pdf = observed && object(observed.pdf_extraction) && observed.pdf_extraction.schema_version === "PDF_EXTRACTION_PROVENANCE_V1" ? observed.pdf_extraction : null;
  const state = prepared && object(prepared.source) ? prepared.source.extraction_truncation : selection.truncation;
  const truncated = state === "TRUNCATED" || pdf?.truncation === "TRUNCATED";
  const notObserved = state === "NOT_OBSERVED" && (!pdf || pdf.truncation === "NOT_OBSERVED");
  return <div className="paper-brief-scope" aria-label="브리프 입력 범위">
    <p>{count(chars) && count(covered) && covered <= chars
      ? `보존 입력: ${knownScope?.schema_version === "AI_REVIEW_INPUT_SCOPE_V2" ? "선택 구간" : "저장 텍스트"} ${chars}자 · 위치 연결 ${covered}자`
      : "보존 입력: 범위 기록 미확인"}. 실제 모델이 읽은 분량이나 전체 논문 검토 완료율을 뜻하지 않습니다.</p>
    {count(original.start) && count(original.end) && original.end > original.start
      ? <p>선택한 원래 본문 좌표: {original.start}–{original.end} (정규화 본문, 끝 위치 제외)</p> : null}
    <p>선택·추출 범위: {truncated ? "잘림 기록 있음" : notObserved ? "잘림 관측 없음 · 전체 확보는 미확인" : "잘림 여부 미확인"}</p>
  </div>;
}

/** Locations describe the preserved quotation only; they do not bind a model sentence to evidence. */
export function EvidenceLocator({ span, scope }: { span: Record<string, unknown>; scope?: unknown }) {
  const prepared = object(scope) && scope.schema_version === "AI_REVIEW_INPUT_SCOPE_V2";
  const page = count(span.page_number) && span.page_number > 0 ? span.page_number : null;
  const range = count(span.char_start) && count(span.char_end) && span.char_end > span.char_start;
  return <div className="paper-brief-locator">
    {page !== null ? <p>기록된 PDF 페이지: {page}</p> : null}
    {range ? <p>{prepared ? "선택 구간 내부 문자 위치" : "기록된 문자 위치"}: {String(span.char_start)}–{String(span.char_end)} (끝 위치 제외)</p> : null}
    {typeof span.source_path === "string" && span.source_path.trim() ? <p>기록된 파일 참조: {span.source_path}</p> : null}
    {object(span.locator) ? <p>보존된 위치 기록: <code>{JSON.stringify(span.locator)}</code></p> : null}
    {page === null && !range && !object(span.locator) ? <p>발췌 위치 기록 없음</p> : null}
  </div>;
}

export function PaperBrief({ result, scope, extraction, preparation, evidenceSpans, inputOrigin }: Props) {
  const id = useId();
  const spans = Array.isArray(evidenceSpans) ? evidenceSpans.filter(object) : [];
  const cited = Array.isArray(result.cited_evidence_span_ids) ? result.cited_evidence_span_ids : [];
  return <section className="paper-brief" aria-labelledby={`${id}-title`}>
    <h5 id={`${id}-title`}>논문 브리프 · 저장된 모델 해석</h5>
    {inputOrigin === "FIXTURE_SYNTHETIC" || inputOrigin === "FIXTURE_SYNTHETIC_CAPTURE" ? <p>합성 시험 기록이며 실제 모델 실행 결과가 아닙니다.</p> : null}
    <p>보존된 해석·질문을 그대로 읽습니다. 빈 항목은 기록 없음이며, 원문에서 찾지 못했거나 해당 없음으로 판정하지 않습니다.</p>
    <InputScope scope={scope} extraction={extraction} preparation={preparation}/>
    <div className="paper-brief-fields">{fields.map(([field, label]) => <section key={field} aria-labelledby={`${id}-${field}`}><h6 id={`${id}-${field}`}>{label}</h6><Value value={result[field]}/></section>)}</div>
    <details><summary>추가 해석과 근거가 부족한 주장</summary><div className="paper-brief-fields">{additional.map(([field, label]) => <section key={field} aria-labelledby={`${id}-${field}`}><h6 id={`${id}-${field}`}>{label}</h6><Value value={result[field]}/></section>)}</div></details>
    {evidenceSpans !== undefined ? <section aria-label="분석의 고정 근거">
      <h6>당시 보존된 원문 발췌</h6><p>응답 전체의 인용 기록입니다. 문장별 연결이나 과학적 뒷받침은 확인되지 않았습니다.</p>
      {spans.length ? spans.map((span, index) => <details key={index}><summary>근거 {index + 1} · {cited.includes(span.span_id) ? "응답에서 인용함" : "입력에 포함됨"}</summary>
        <p>{typeof span.span_id === "string" ? span.span_id : "식별자 미확인"}</p><EvidenceLocator span={span} scope={scope}/>
        <pre tabIndex={0} style={prose} aria-label={`보존된 원문 발췌 ${index + 1}`}>{typeof span.text === "string" && span.text.trim() ? span.text : "발췌 기록 없음"}</pre>
      </details>) : <p>보존된 원문 발췌 기록 없음</p>}
    </section> : null}
  </section>;
}
