import { useEffect, useRef, useState } from "react";
import { getReviewInput, getReviewInputs, type ReviewInputDetail, type ReviewInputList, type ReviewInputScope } from "../paperBankApi";
import type { FrozenReviewInput, JournalReviewInput, ReviewInputSelection } from "../../lib/localSynthesis";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { SavedReviewResearchContext } from "./SavedReviewResearchContext";
import { EvidenceText } from "./EvidenceText";
import { EvidenceLocator, PaperBrief } from "./PaperBrief";
import "./ReviewInputHistory.css";

type Props = { topicId: string; paperVersionId: string; selectedReportReceiptId?: string; onSelectForReport?: (selection: ReviewInputSelection) => void; reportSelectionDisabledReason?: string; openReceipt?: { receiptId: string; revision: number } };
const prose = { whiteSpace: "pre-wrap" as const, overflowWrap: "anywhere" as const };
const origins: Record<string, string> = {
  FIXTURE_SYNTHETIC: "합성 실행 · 실제 AI 본문 검토가 아닙니다.",
  PROVIDER_RESPONSE: "Provider 응답 · 실제 모델·연구 품질은 독립적으로 검증되지 않았습니다.",
  MANUAL_IMPORT_UNVERIFIED: "수동 반입 · 실제 전달 범위와 모델 실행은 미확인입니다.",
  LOCAL_APP_CAPTURE: "앱에 보존된 응답의 사본 · 새 모델 호출이 아니며 과학적 검증은 미완료입니다.",
  FIXTURE_SYNTHETIC_CAPTURE: "앱에 보존된 합성 응답의 사본 · 실제 AI 실행의 증거가 아닙니다.",
};
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const preparedPacket = (packet: Record<string, unknown>) => packet.packet_version === "ai-review-packet-v3" || packet.packet_version === "ai-review-packet-v4" || packet.packet_version === "ai-review-packet-v5";
const savedResearchContext = (preparation: unknown) => object(preparation) && ["REVIEW_PREPARATION_BINDING_V2", "REVIEW_PREPARATION_BINDING_V3"].includes(String(preparation.schema_version)) ? preparation.context : undefined;
function Scope({ value }: { value: { input_origin: string; input_scope: ReviewInputScope } }) {
  const scope = value.input_scope, length = scope.source?.preserved_text_chars, covered = scope.covered_stored_chars;
  const prepared = scope.schema_version === "AI_REVIEW_INPUT_SCOPE_V2";
  return <div className="review-input-scope">
    <p>{origins[value.input_origin] ?? "입력 출처 미확인"}</p>
    {count(length) && count(covered) && covered <= length ? <p>{prepared ? "보존된 선택 구간" : "저장 본문"} {length}자 중 위치 확인된 {covered}자</p> : <p>저장 본문 길이·처리 위치 미확인. 과거 기록에 없는 범위를 현재 자료로 다시 만들지 않습니다.</p>}
    {prepared ? <p>아래 범위는 보존된 선택 구간 내부 위치 0부터 계산합니다. 원래 정규화 본문의 좌표와 구분하며, 선택하지 않은 전체 본문은 이 입력에 보존되지 않습니다.</p> : null}
    <p>전체 문서 길이: 미확인 · 전체 문서 검토: 확인되지 않음</p>
    <p>위치 확인 문자 수는 겹치는 구간을 중복 계산하지 않은 값입니다. 실제 전달·AI 검토 완료율이 아닙니다.</p>
    {scope.components?.length ? <details><summary>발췌별 저장 본문 위치</summary>
      <p>문자 위치는 0부터 시작하며 끝 위치는 포함하지 않습니다.</p>
      <ul>{scope.components.map(component => <li key={component.json_pointer}>
        <p>{component.json_pointer} · {component.chars}자</p>
        <p>{component.mapping === "exact" && count(component.source_start) && count(component.source_end)
          ? `${prepared ? "선택 구간 내부 위치" : "저장 본문 문자 위치"}: ${component.source_start}–${component.source_end}` : "저장 본문에서의 위치 미확인"}</p>
      </li>)}</ul>
    </details> : null}
  </div>;
}
const quantity = (value: unknown, unit: string) => count(value) ? `${value}${unit}` : "미확인";
const digest = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/i.test(value) ? value : "미확인";
const name = (value: unknown) => typeof value === "string" && value.trim() ? value : "미확인";
const truncation = (value: unknown) => value === "TRUNCATED" ? "잘림 확인" : value === "NOT_OBSERVED" ? "잘림 관측 없음" : "미확인";
function ExecutionProvenance({ value }: { value: unknown }) {
  if (value === undefined) return null;
  if (!object(value)) return <p>보존된 실행 요청·응답 출처를 해석할 수 없습니다.</p>;
  const summary = value.schema_version === "PREPARED_REVIEW_RESULT_SUMMARY_V1";
  const version = value.schema_version === "PREPARED_REVIEW_RESULT_V1" ? "V1" : value.schema_version === "PREPARED_REVIEW_RESULT_V2" ? "V2" : value.schema_version === "PREPARED_REVIEW_RESULT_V3" ? "V3" : value.schema_version === "PREPARED_REVIEW_RESULT_V4" ? "V4" : null;
  const full = version !== null;
  const requested = summary ? value.requested : value.contract;
  const reported = summary ? value.reported : value.response;
  if ((!summary && !full) || !object(requested) || !object(reported)
      || (full && (requested.schema_version !== `PREPARED_REVIEW_REQUEST_${version}`
        || !object(requested.provider_request) || requested.provider_request.schema_name !== `PREPARED_PAPER_REVIEW_DRAFT_${version === "V4" ? "V3" : version}`))) {
    return <p>보존된 실행 요청·응답 출처를 해석할 수 없습니다.</p>;
  }
  const legacyBodyResultMismatch = (version === "V1" || version === "V2")
    && object(requested.packet) && requested.packet.review_level === "full_text_grounded" && requested.packet.abstract === null
    && object(reported.payload) && reported.payload.review_level === "abstract_only";
  return <details style={prose} aria-label="보존된 실행 요청·응답 출처">
    <summary>보존된 실행 요청·응답 출처</summary>
    <p>요청 Provider: {name(requested.provider_name)}</p><p>요청 모델: {name(requested.model)}</p>
    <p>응답에 기록된 Provider: {name(reported.provider_name)}</p>
    <p>응답에 기록된 모델: {reported.model === "not_reported" ? "미보고 (실제 모델 미확인)" : name(reported.model)}</p>
    <p>응답 식별자: {name(reported.response_id)}</p>
    <p>요청 SHA-256: {digest(summary ? value.request_sha256 : requested.request_sha256)}</p>
    <p>계약 SHA-256: {digest(summary ? value.contract_sha256 : requested.contract_sha256)}</p>
    <p>응답 원문 SHA-256: {digest(summary ? value.raw_text_sha256 : reported.raw_text_sha256)}</p>
    <p>실제 모델 신원: {value.reported_model_identity === "NOT_VERIFIED" ? "독립 검증 안 됨" : "미확인"} · 실제 외부 실행: {value.independent_live_proof === "NOT_VERIFIED" ? "독립 검증 안 됨" : "미확인"}</p>
    <p>과학적 뒷받침: {value.scientific_support === "NOT_ASSESSED" ? "미평가" : "미확인"} · 전체 문서 검토: 확인되지 않음 · 사람의 채택: 확인되지 않음</p>
    <p>이 검토 응답 생성 시점의 기록입니다. 이후 저널 채택 기록과 구분합니다. 식별값 일치는 연구 품질의 증명이 아닙니다.</p>
    {legacyBodyResultMismatch ? <p>이 과거 기록은 본문 구간 입력에 초록 기반 결과 유형을 저장했습니다. 당시 계약은 입력과 결과 유형의 일치를 확인하지 않았습니다.</p> : null}
    {version === "V1" ? <p>보존된 V1 검사 계약입니다. 새 V2의 필수 서술·배열 항목 공백 검사를 이 과거 기록에 소급 적용하지 않습니다.</p>
      : version === "V2" ? <p>보존된 V2 검사 계약은 필수 서술과 배열 항목이 공백뿐인 응답을 거부합니다. 빈 항목 검사는 내용의 충실도나 과학적 타당성 평가가 아닙니다.</p>
      : version === "V3" ? <p>보존된 V3 검사 계약은 필수 서술과 배열 항목이 공백뿐인 응답을 거부합니다. 본문 구간에 근거한 검토와 제공된 근거 구간 인용 1개 이상을 요구합니다. 이 계약은 논문 전체 검토나 과학적 타당성 검증을 뜻하지 않습니다.</p> : null}
    {summary ? <p>이 저널 입력에는 요청·응답의 식별 요약만 포함됩니다. 전체 원문은 보존된 검토 기록에서 확인하세요.</p> : null}
  </details>;
}
function PreparationProvenance({ value }: { value: unknown }) {
  if (!object(value) || (value.schema_version !== "REVIEW_PREPARATION_BINDING_V1" && value.schema_version !== "REVIEW_PREPARATION_BINDING_V2" && value.schema_version !== "REVIEW_PREPARATION_BINDING_V3")) return <p>보존된 준비 구간 출처: 미확인</p>;
  const source = object(value.source) ? value.source : {}, selection = object(value.selection) ? value.selection : {};
  return <section aria-label="보존된 준비 구간 출처">
    <h5>고정한 준비 구간의 출처</h5>
    <p>준비 기록: {name(value.preparation_id)}</p>
    {count(selection.start) && count(selection.end) && count(selection.chars) ? <p>원래 정규화 본문 위치: {selection.start}–{selection.end} · 선택 {selection.chars}자</p> : <p>원래 정규화 본문 위치: 미확인</p>}
    <p>당시 출처 본문 길이: {quantity(source.source_chars, "자")} · 추출 잘림: {truncation(source.extraction_truncation)}</p>
    <p>이 길이는 당시 출처의 관측값이며 전체 본문을 이번 입력에 보존하거나 검토했다는 뜻은 아닙니다.</p>
    <details><summary>준비 구간 출처 식별자</summary>
      <p>정규화: {value.normalization === "XRML_WHITESPACE_V1" ? "공백 정규화 (XRML_WHITESPACE_V1)" : "미확인"} · 읽기: {value.decoding === "UTF8_STRICT" ? "UTF-8 엄격 검증" : value.decoding === "UTF8_SIG_STRICT" ? "UTF-8 BOM 허용·엄격 검증" : "미확인"}</p>
      <p>파일 참조: {name(source.artifact_ref)}</p><p>파일 SHA-256: {digest(source.artifact_sha256)} · {quantity(source.artifact_bytes, "바이트")}</p>
      <p>출처 관측 SHA-256: {digest(source.source_digest)}</p><p>준비 기록 SHA-256: {digest(value.preparation_sha256)}</p><p>선택 구간 SHA-256: {digest(selection.sha256)}</p>
    </details>
  </section>;
}
function pages(value: unknown) {
  if (!Array.isArray(value) || !value.every(page => count(page) && page > 0)) return "미확인";
  return value.length ? `${value.join(", ")} (${value.length}개)` : "기록 없음 (0개)";
}
function Extraction({ value }: { value: unknown }) {
  if (!object(value) || value.schema_version !== "REVIEW_EXTRACTION_V1") return <section className="review-input-extraction" aria-label="보존된 원문·추출 범위">
    <h5>보존된 원문·추출 범위</h5>
    <p>보존된 원문·추출 범위 미확인. 이 기록에는 읽을 수 있는 당시 원문 파일·추출 한도가 없습니다. 현재 파일에서 추정하지 않습니다.</p>
  </section>;
  const artifact = object(value.artifact) ? value.artifact : {};
  const selection = object(value.selection) ? value.selection : {};
  const pdf = object(value.pdf_extraction) && value.pdf_extraction.schema_version === "PDF_EXTRACTION_PROVENANCE_V1" ? value.pdf_extraction : null;
  const sourcePdf = pdf && object(pdf.source_pdf) ? pdf.source_pdf : {};
  const extractor = pdf && object(pdf.extractor) ? pdf.extractor : {};
  const outputs = pdf && object(pdf.outputs) ? pdf.outputs : {};
  const kind = artifact.kind === "text" ? "텍스트" : artifact.kind === "pages" ? "페이지 JSON" : artifact.kind === "pdf_extract" ? "PDF 직접 추출" : "종류 미확인";
  return <section className="review-input-extraction" aria-label="보존된 원문·추출 범위">
    <h5>보존된 원문·추출 범위</h5>
    <p>정규화 후 선택: {quantity(selection.chars, "자")} · 선택 한도: {quantity(selection.max_chars, "자")} · 선택 전 텍스트: {quantity(selection.available_chars, "자")}</p>
    <p>선택 범위 잘림: {truncation(selection.truncation)}</p>
    <p>잘림 관측 없음도 논문 전체 확보나 AI 전체 검토를 뜻하지 않습니다. 이 값은 당시 파일·추출 과정의 기록이며 모델에 전달된 범위는 아래 보존된 입력에서 확인하세요.</p>
    <details><summary>원문·추출 기록 상세</summary>
      <p>당시 읽은 파일: {kind} · {quantity(artifact.bytes, "바이트")}</p>
      <p>파일 SHA-256: {digest(artifact.sha256)}</p>
      <p>선택 텍스트 SHA-256: {digest(selection.sha256)}</p>
      <p>정규화: {value.normalization === "XRML_WHITESPACE_V1" ? "공백 정규화 (XRML_WHITESPACE_V1)" : "미확인"}</p>
      <p>읽기 방식: {value.decoding === "UTF8_ERRORS_IGNORE" ? "UTF-8 (해석할 수 없는 UTF-8 바이트는 무시)" : value.decoding === "PDF_ENGINE" ? "PDF 추출 엔진" : "미확인"}</p>
      {pdf ? <>
        <h6>보존된 PDF 추출 기록</h6>
        <p>원본 PDF: {quantity(sourcePdf.bytes, "바이트")} · SHA-256: {digest(sourcePdf.sha256)}</p>
        <p>추출 도구: {name(extractor.name)} · 버전: {name(extractor.version)}</p>
        <p>PDF 정규화: {extractor.normalization === "XRML_WHITESPACE_V1" ? "공백 정규화 (XRML_WHITESPACE_V1)" : "미확인"}</p>
        <p>PDF 전체 페이지 수: {quantity(pdf.document_page_count, "페이지")}</p>
        <p>추출기가 방문한 페이지: {pages(pdf.visited_page_numbers)}</p>
        <p>텍스트가 보존된 페이지: {pages(pdf.retained_page_numbers)}</p>
        <p>방문하거나 텍스트를 보존한 페이지라도 전체 내용이 추출·검토됐다는 뜻은 아닙니다.</p>
        <p>PDF 추출 잘림: {truncation(pdf.truncation)} · 추출 한도: {quantity(pdf.max_chars, "자")}</p>
        <p>추출기가 기록한 문서 텍스트 길이: {quantity(pdf.document_text_chars, "자")}</p>
        <p>추출 텍스트: {quantity(outputs.text_bytes, "바이트")} · SHA-256: {digest(outputs.text_sha256)}</p>
        <p>페이지 JSON: {quantity(outputs.pages_bytes, "바이트")} · SHA-256: {digest(outputs.pages_sha256)}</p>
      </> : <p>PDF 추출 기록: 미확인. 읽은 파일만으로 원본 PDF의 범위를 추정하지 않습니다.</p>}
    </details>
  </section>;
}
function safeScope(value: unknown): ReviewInputScope {
  return object(value) && typeof value.schema_version === "string" ? value as ReviewInputScope : { schema_version: "UNKNOWN" };
}
function componentText(packet: Record<string, unknown>, pointer: string): string | null {
  let value: unknown = packet;
  if (!pointer.startsWith("/")) return null;
  for (const segment of pointer.slice(1).split("/")) {
    const key = segment.replace(/~1/g, "/").replace(/~0/g, "~");
    if ((!object(value) && !Array.isArray(value)) || !Object.hasOwn(value, key)) return null;
    value = (value as Record<string, unknown>)[key];
  }
  return typeof value === "string" ? value : null;
}
export function PreservedReviewContext({ receiptId, inputOrigin, scope, extraction, preparation, result, status, validation, archive = false, showBrief = true }: { receiptId: string; inputOrigin: string; scope: unknown; extraction: unknown; preparation?: unknown; result: Record<string, unknown>; status?: string; validation?: Record<string, unknown>; archive?: boolean; showBrief?: boolean }) {
  return <section className="review-input-history review-input-history-body" aria-label="고정한 과거 검토 문맥">
    {showBrief ? <PaperBrief result={result} scope={scope} extraction={extraction} preparation={preparation} inputOrigin={inputOrigin}/> : null}
    <p>고정한 검토 기록: {receiptId}</p>
    <p>{archive ? "아래 범위는 반입 파일에 보존된 당시 검토 요청의 범위입니다." : "아래 범위는 당시 검토 요청 전체의 보존 범위입니다. 이번 보고서에서 선택한 입력 구간과 구분합니다."}</p>
    <Scope value={{ input_origin: inputOrigin, input_scope: safeScope(scope) }}/>
    <SavedReviewResearchContext value={savedResearchContext(preparation)}/>
    {preparation || safeScope(scope).schema_version === "AI_REVIEW_INPUT_SCOPE_V2" ? <PreparationProvenance value={preparation}/> : <Extraction value={extraction}/>}
    <h5>과거 검토 결과 · 원문 사실과 구분되는 해석</h5>
    <p>당시 저장된 해석·제안입니다. 인용할 원문 사실이나 독립적으로 검증된 과학적 결론으로 취급하지 않습니다.</p>
    {inputOrigin === "FIXTURE_SYNTHETIC" ? <p>합성 연결 시험의 작성 결과입니다. 연구 품질·신규성·신뢰도는 미평가입니다.</p> : null}
    <p>당시 검토 결과 상태: {status || "미확인"}. 이 저장 상태는 사람의 저널 채택이나 과학적 타당성 검증을 뜻하지 않습니다.</p>
    <ExecutionProvenance value={validation?.prepared_execution}/>
    <details><summary>당시 형식·근거 연결 검사 기록</summary>{validation ? <pre style={prose}>{JSON.stringify(validation,null,2)}</pre> : <p>검사 기록 미확인</p>}</details>
    <details><summary>과거 검토 결과 원문(JSON)</summary><pre style={prose}>{JSON.stringify(result, null, 2)}</pre></details>
  </section>;
}
export function FrozenReviewInputView({ value }: { value: FrozenReviewInput }) {
  const { preferences } = useOptionalAppearance();
  const receipt = value.receipt;
  return <section aria-label="고정한 검토 입력 기록">
    <p>기록 시각: {formatTimestamp(receipt.recorded_at, preferences.timeFormat, "ko-KR")} · Run: {receipt.run_id} · 논문: {receipt.paper_id}</p>
    <details open><summary>선택한 검토 입력 발췌 {value.selected_component_ids.length}개</summary>
      {value.selected_component_ids.map(pointer => <details key={pointer}><summary>{pointer}</summary><EvidenceText text={componentText(receipt.packet, pointer) ?? "보존된 구간 텍스트를 확인할 수 없습니다."}/></details>)}
    </details>
    <PreservedReviewContext receiptId={receipt.receipt_id} inputOrigin={receipt.input_origin} scope={receipt.packet.input_scope} extraction={receipt.packet.source_extraction} preparation={receipt.packet.source_preparation} result={receipt.draft} status={receipt.status} validation={receipt.validation}/>
    <details><summary>고정한 검토 기록 원문(JSON)</summary><pre style={prose}>{JSON.stringify(receipt, null, 2)}</pre></details>
  </section>;
}
export function JournalReviewInputContext({ value }: { value: JournalReviewInput }) {
  return <section><p className="journal-preserved-text">이번 보고서의 선택 구간: {value.selected_component_ids.join(", ")}</p><PreservedReviewContext receiptId={value.receipt_id} inputOrigin={value.input_origin} scope={value.input_scope} extraction={value.source_extraction} preparation={value.source_preparation} result={value.review_result} status={value.status} validation={value.validation}/></section>;
}
function reportComponents(packet: Record<string, unknown>): string[] {
  if (!object(packet.input_scope) || !((packet.packet_version === "ai-review-packet-v2" && packet.input_scope.schema_version === "AI_REVIEW_INPUT_SCOPE_V1") || (preparedPacket(packet) && packet.input_scope.schema_version === "AI_REVIEW_INPUT_SCOPE_V2")) || !Array.isArray(packet.input_scope.components)) return [];
  return [...new Set(packet.input_scope.components.filter(object).filter(component => typeof component.json_pointer === "string" && component.json_pointer.startsWith("/") && count(component.chars) && component.chars > 0).map(component => component.json_pointer as string))].sort();
}
function Receipt({ topicId, paperVersionId, receiptId, onSelectForReport, selectedReportReceiptId, reportSelectionDisabledReason, openReceipt }: Props & { receiptId: string }) {
  const { preferences } = useOptionalAppearance();
  const [value, setValue] = useState<ReviewInputDetail | null>(null), [error, setError] = useState("");
  const [loading, setLoading] = useState(true), [revision, setRevision] = useState(0), [rawOpen, setRawOpen] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError(""); setValue(null);
    void getReviewInput(topicId, paperVersionId, receiptId).then(result => {
      if (!cancelled) { setValue(result); setLoading(false); }
    }, reason => { if (!cancelled) { setError(String(reason)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [topicId, paperVersionId, receiptId, revision]);
  useEffect(() => { if (value) heading.current?.focus({ preventScroll: !openReceipt }); }, [value, openReceipt]);
  const excerpts = Array.isArray(value?.packet.selected_text_excerpts) ? value.packet.selected_text_excerpts.filter(object) : [];
  const spans = Array.isArray(value?.packet.evidence_spans) ? value.packet.evidence_spans.filter(object) : [];
  const componentIds = value ? reportComponents(value.packet) : [];
  return <div className="review-input-receipt">
    <V2Button type="button" disabled={loading} onClick={() => setRevision(current => current + 1)}>선택 기록 다시 확인</V2Button>
    {loading ? <p role="status">선택한 검토 입력·결과 확인 중</p> : null}
    {error ? <p role="alert">검토 입력·결과 조회 오류: {error}</p> : null}
    {value ? <section aria-label="선택한 검토 입력·결과">
      <h4 ref={heading} tabIndex={-1}>보존된 검토 입력과 결과</h4>
      <PaperBrief result={value.draft} scope={value.input_scope} extraction={value.packet.source_extraction} preparation={value.packet.source_preparation} inputOrigin={value.input_origin}/>
      {onSelectForReport ? <div>
        <V2Button type="button" disabled={!!reportSelectionDisabledReason || !componentIds.length} aria-pressed={selectedReportReceiptId === value.receipt_id} onClick={() => onSelectForReport({ receipt_id: value.receipt_id, component_ids: componentIds })}>보고서 입력에 이 기록 선택</V2Button>
        <p>{reportSelectionDisabledReason || (!componentIds.length ? "등록된 V2/V3/V4 입력 구간이 없어 보고서 입력으로 선택할 수 없습니다. 과거 기록 조회는 가능합니다." : `등록된 비어 있지 않은 입력 구간 ${componentIds.length}개와 당시 검토 결과를 선택합니다. 논문도 함께 선택되며 아직 고정·실행되지 않습니다.`)}</p>
      </div> : null}
      <p>저장 시각: {formatTimestamp(value.recorded_at, preferences.timeFormat, "ko-KR")}</p>
      <Scope value={value}/>
      <SavedReviewResearchContext value={savedResearchContext(value.packet.source_preparation)}/>
      {preparedPacket(value.packet) ? <PreparationProvenance value={value.packet.source_preparation}/> : <Extraction value={value.packet.source_extraction}/>}
      {preparedPacket(value.packet) ? <p>고정한 준비 구간과 출처 정보를 보존했습니다. 선택하지 않은 원래 본문을 현재 파일에서 추가하지 않습니다.</p> : <p>선택 발췌의 12,000자 기본 한도는 별도로 포함된 초록·근거 발췌·기존 검토와 메타데이터까지 제한하지 않습니다. 아래 전체 입력에서 함께 보존된 문맥을 확인할 수 있습니다.</p>}
      <p>형식·근거 연결 검사: {value.validation_status}. 검사 상태는 내용의 과학적 타당성이나 전세계 신규성의 증명이 아닙니다.</p>
      <ExecutionProvenance value={value.validation?.prepared_execution}/>
      {value.validation ? <details><summary>당시 형식·근거 연결 검사 기록</summary><pre tabIndex={0} style={prose}>{JSON.stringify(value.validation, null, 2)}</pre></details> : null}
      {!preparedPacket(value.packet) || excerpts.length ? <><h5>선택된 텍스트 발췌</h5>
      {excerpts.length ? excerpts.map((excerpt, index) => <details key={index}>
        <summary>발췌 {index + 1} · 출처: {typeof excerpt.source === "string" ? excerpt.source : "미확인"}{count(excerpt.char_count) ? ` · ${excerpt.char_count}자` : ""}</summary>
        <pre style={prose} aria-label={`선택 발췌 ${index + 1}`}>{typeof excerpt.text === "string" ? excerpt.text : "텍스트 미확인"}</pre>
      </details>) : <p>보존된 선택 발췌 없음</p>}</> : null}
      <h5>{preparedPacket(value.packet) ? "검토 입력에 보존된 고정 본문" : "함께 보존된 근거 발췌"}</h5>
      {spans.length ? spans.map((span, index) => <details key={index}><summary>{preparedPacket(value.packet) ? "준비 구간" : "근거"} {index + 1} · {typeof span.span_id === "string" ? span.span_id : "식별자 미확인"}</summary><EvidenceLocator span={span} scope={value.input_scope}/><EvidenceText text={typeof span.text === "string" ? span.text : "텍스트 미확인"}/></details>) : <p>보존된 근거 발췌 없음</p>}
      <details onToggle={event => setRawOpen(event.currentTarget.open)}><summary>보존된 입력 전체(JSON)</summary>
        {rawOpen ? <pre tabIndex={0} style={prose} aria-label="보존된 검토 입력 JSON">{JSON.stringify(value.packet, null, 2)}</pre> : null}
      </details>
      <h5>저장된 검토 결과</h5>
      <p>저장된 작성 결과를 표시합니다. 모델의 해석·제안은 원문에서 확인된 사실과 구분해 검토해야 합니다.</p>
      {value.input_origin === "FIXTURE_SYNTHETIC" ? <p>합성 연결 시험의 작성 결과입니다. 연구 품질·신규성·신뢰도는 미평가입니다.</p> : null}
      <details><summary>결과 원문과 기록 식별자</summary>
        <p>입력 기록: {value.receipt_id}</p><p>Run: {value.run_id} · 논문: {value.paper_id}</p>
        <p>기록된 출처 식별값 (source_text_sha256): {value.source_text_sha256 || "미확인"}</p>
        <p>이 식별값은 과거 정규화된 일부 텍스트나 메타데이터에서 계산됐을 수 있으며, 원문 전체 바이트의 검증을 뜻하지 않습니다.</p>
        <p>입력 묶음 SHA-256: {value.input_scope.packet_payload_sha256 ?? "미확인"}</p>
        <pre tabIndex={0} style={prose} aria-label="보존된 검토 결과 JSON">{JSON.stringify(value.draft, null, 2)}</pre>
      </details>
    </section> : null}
  </div>;
}
function History(props: Props) {
  const { preferences } = useOptionalAppearance();
  const [list, setList] = useState<ReviewInputList | null>(null), [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true), [error, setError] = useState(""), [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState(props.openReceipt?.receiptId ?? "");
  const openedRequest = useRef(props.openReceipt);
  useEffect(() => {
    if (props.openReceipt && props.openReceipt !== openedRequest.current) { setSelected(props.openReceipt.receiptId); setPage(1); setRevision(value => value + 1); }
    openedRequest.current = props.openReceipt;
  }, [props.openReceipt]);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    void getReviewInputs(props.topicId, props.paperVersionId, page).then(result => {
      if (!cancelled) { setList(result); setLoading(false); }
    }, reason => { if (!cancelled) { setError(String(reason)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [props.topicId, props.paperVersionId, page, revision]);
  return <div className="review-input-history-body">
    <p>이 논문 버전에 연결된 당시 입력과 결과입니다. 조회는 연구 실행이나 외부 전송을 시작하지 않습니다.</p>
    <V2Button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>검토 입력 이력 새로고침</V2Button>
    {loading ? <p role="status">검토 입력 이력 확인 중</p> : null}
    {error ? <p role="alert">검토 입력 이력 조회 오류: {error}</p> : null}
    {!loading && !error && list ? <>
      {list.items.length ? <ul className="review-input-records">{list.items.map(item => <li key={item.receipt_id} data-selected={selected === item.receipt_id}>
        <p>{origins[item.input_origin] ?? "입력 출처 미확인"}</p>
        <p>{formatTimestamp(item.recorded_at, preferences.timeFormat, "ko-KR")} · {item.review_level === "full_text_grounded" ? "본문 근거 연결 기록" : item.review_level === "abstract_only" ? "초록 기반 기록" : "검토 수준 미확인"}</p>
        <div className="review-input-record-actions"><V2Button type="button" aria-label={`입력·결과 열기 · ${item.receipt_id}`} aria-pressed={selected === item.receipt_id} onClick={() => setSelected(item.receipt_id)}>입력·결과 열기</V2Button>
          <small title={item.receipt_id}>{item.receipt_id.length > 20 ? `${item.receipt_id.slice(0, 18)}…` : item.receipt_id}</small></div>
      </li>)}</ul> : <p>보존된 검토 입력 기록이 없습니다. 기록이 없다는 사실만으로 과거 모델 실행 여부를 판단할 수 없습니다.</p>}
      <nav aria-label="검토 입력 이력 페이지"><V2Button type="button" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>이전 검토 입력 페이지</V2Button><span> {page} 페이지 </span><V2Button type="button" disabled={!list.hasMore} onClick={() => setPage(value => value + 1)}>다음 검토 입력 페이지</V2Button></nav>
    </> : null}
    {selected ? <Receipt key={`${selected}:${props.openReceipt?.revision ?? 0}`} {...props} receiptId={selected}/> : null}
  </div>;
}
function Panel(props: Props) {
  const [opened, setOpened] = useState(!!props.openReceipt), panel = useRef<HTMLDetailsElement>(null);
  useEffect(() => { if (props.openReceipt) { setOpened(true); if (panel.current) panel.current.open = true; } }, [props.openReceipt]);
  return <details ref={panel} className="review-input-history" onToggle={event => { if (event.currentTarget.open) setOpened(true); }}>
    <summary>검토 입력·결과 이력</summary>{opened ? <History {...props}/> : null}
  </details>;
}
export function ReviewInputHistory(props: Props) { return <Panel key={JSON.stringify([props.topicId, props.paperVersionId])} {...props}/>; }
