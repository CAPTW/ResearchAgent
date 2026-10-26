import { useEffect, useState } from "react";
import { getDocumentCoverage, type DocumentCoverageRecord, type PdfExtractionRecord, type SourceRange } from "../../lib/reviewPreparation";
import { V2Button } from "../V2Primitives";
import { SavedResultCoverage, type GapPreparationControls } from "./ReviewSourceCoverage";
import { ReviewGapPreparation } from "./ReviewGapPreparation";

type Props = { root: string; record: PdfExtractionRecord; onOpenPreparation?: (id: string) => void; onOpenReceipt?: (id: string) => void; gapPreparation?: GapPreparationControls };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const integer = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const text = (value: unknown): value is string => typeof value === "string" && !!value;
const ranges = (values: SourceRange[]) => values.length ? values.map(value => `${value.start}–${value.end}`).join(", ") : "없음";
const reason = (value: string) => ({ TEXT_NOT_FOUND: "추출 텍스트에서 원래 입력을 찾을 수 없음", TEXT_AMBIGUOUS: "동일 텍스트가 여러 곳에 있어 위치를 확정할 수 없음", LEGACY_BINDING_UNAVAILABLE: "보존된 구간 연결 계약 없음" } as Record<string, string>)[value] ?? value;
function validRanges(value: unknown, start: number, end: number): value is SourceRange[] {
  return Array.isArray(value) && value.every((range, index) => object(range) && integer(range.start) && integer(range.end)
    && range.start >= start && range.start < range.end && range.end <= end && (!index || range.start >= value[index - 1].end));
}
function validGroup(value: unknown, chars: number, result: boolean, gaps: boolean): boolean {
  if (!object(value) || !integer(value.record_count) || !integer(value.covered_chars) || !validRanges(value.ranges, 0, chars)
    || !Array.isArray(value.records) || value.record_count !== value.records.length || value.covered_chars !== value.ranges.reduce((sum, range) => sum + range.end - range.start, 0)
    || (gaps && !validRanges(value.gaps, 0, chars))) return false;
  return value.records.every(record => object(record) && text(record.preparation_id) && integer(record.start) && integer(record.end)
    && record.start < record.end && record.end <= chars && object(record.source) && text(record.source.source_digest)
    && object(record.selection) && integer(record.selection.start) && integer(record.selection.end) && record.selection.start < record.selection.end
    && record.selection.chars === record.selection.end - record.selection.start && text(record.selection.sha256)
    && ["DIRECT_SOURCE_COORDINATES", "EXACT_UNIQUE_TEXT_MATCH"].includes(String(record.mapping))
    && (!result || (text(record.receipt_id) && ["BODY_GROUNDED", "OTHER_RETURNED", "UNVERIFIED_PROVIDER"].includes(String(record.class))
      && ["NATIVE_ANTHROPIC_V2", "VALIDATED_PREPARED_RESPONSE", "UNVERIFIED_ORIGIN"].includes(String(record.subtype)))));
}
function read(value: unknown, expected: PdfExtractionRecord): DocumentCoverageRecord {
  const invalid = () => { throw new Error("선택한 추출 기록의 범위 응답을 확인할 수 없습니다."); };
  if (!object(value) || value.schema_version !== "PAPER_REVIEW_DOCUMENT_COVERAGE_V1" || !object(value.extraction)
    || value.extraction.extraction_id !== expected.extraction_id || value.extraction.topic_id !== expected.topic_id || value.extraction.paper_version_id !== expected.paper_version_id
    || !object(value.extraction.pdf) || value.extraction.pdf.sha256 !== expected.pdf.sha256 || !object(value.extraction.source)
    || value.extraction.source.source_digest !== expected.source.source_digest || value.extraction.source.source_chars !== expected.source.source_chars
    || value.coordinate_space !== "NORMALIZED_EXTRACTION_CODEPOINTS" || value.normalization !== "XRML_WHITESPACE_V1" || value.full_document_review !== "NOT_ESTABLISHED" || value.scientific_support !== "NOT_ASSESSED"
    || value.alignment_claim !== "EXACT_NORMALIZED_TEXT_ONLY_NOT_PDF_IDENTITY" || !object(value.results)
    || value.results.gap_semantics !== "NO_VALIDATED_SAVED_RESULT" || value.results.attempt_history_included !== false
    || !validGroup(value.prepared, expected.source.source_chars, false, true)
    || !validGroup(value.results.validated_returned, expected.source.source_chars, true, true)
    || !validGroup(value.results.body_grounded, expected.source.source_chars, true, true)
    || !validGroup(value.results.other_returned, expected.source.source_chars, true, false)
    || !validGroup(value.results.unverified_provider, expected.source.source_chars, true, false)
    || JSON.stringify(value.no_text_page_numbers) !== JSON.stringify(expected.no_text_page_numbers)
    || JSON.stringify(value.unvisited_page_numbers) !== JSON.stringify(expected.unvisited_page_numbers)
    || !Array.isArray(value.pages) || value.pages.length !== expected.page_ranges.length || !object(value.unaligned)
    || !Array.isArray(value.unaligned.preparations) || !Array.isArray(value.unaligned.receipts)) return invalid();
  if (value.pages.some((page, index) => {
    const original = expected.page_ranges[index];
    return !object(page) || page.page_number !== original.page_number || page.start !== original.start || page.end !== original.end || page.chars !== original.chars
      || page.text_status !== "TEXT_EXTRACTED" || !integer(page.prepared_chars) || page.prepared_chars > original.chars
      || !integer(page.body_result_chars) || page.body_result_chars > original.chars || !validRanges(page.body_result_ranges, original.start, original.end)
      || page.body_result_chars !== page.body_result_ranges.reduce((sum, range) => sum + range.end - range.start, 0) || !validRanges(page.gaps, original.start, original.end);
  }) || [...value.unaligned.preparations, ...value.unaligned.receipts].some(item => !object(item) || !(text(item.preparation_id) || text(item.receipt_id))
    || !["TEXT_NOT_FOUND", "TEXT_AMBIGUOUS", "LEGACY_BINDING_UNAVAILABLE"].includes(String(item.reason)))) return invalid();
  return value as unknown as DocumentCoverageRecord;
}

function Coverage({ root, record, onOpenPreparation, onOpenReceipt, gapPreparation }: Props) {
  const [opened, setOpened] = useState(false), [revision, setRevision] = useState(0), [value, setValue] = useState<DocumentCoverageRecord | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    if (!opened) return;
    let cancelled = false; setLoading(true); setValue(null); setError("");
    void getDocumentCoverage(root, record.extraction_id).then(result => { const next = read(result, record); if (!cancelled) { setValue(next); setLoading(false); } }).catch(reason => {
      if (!cancelled) { setError(reason instanceof Error ? reason.message : String(reason)); setLoading(false); }
    });
    return () => { cancelled = true; };
  }, [root, record, opened, revision]);
  return <details className="review-preparation-record" onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>PDF 전체 추출 텍스트의 결과 범위</summary>{opened ? <section aria-label="PDF 추출 결과 범위">
    <p>이 추출에서 보존한 정규화 텍스트를 분모로 사용합니다. 원본 PDF의 모든 내용·이미지·수식 또는 모델의 전체 문서 검토율이 아닙니다.</p>
    <V2Button disabled={loading || !!gapPreparation?.busy} onClick={() => setRevision(previous => previous + 1)}>PDF 결과 범위 새로고침</V2Button>
    {loading ? <p role="status">PDF 결과 범위 확인 중</p> : error ? <p role="alert">PDF 결과 범위 조회 오류: {error}. 현재 범위를 0으로 표시하지 않았습니다.</p> : value ? <>
      <p>보존된 추출 텍스트 {record.source.source_chars.toLocaleString("ko-KR")}자 · Unicode 코드 포인트, 끝 위치 제외</p>
      <p>전체 문서 검토 미확인 · 과학적 뒷받침 미평가</p><p>다른 출처의 과거 입력은 원래 텍스트와 유일하게 일치하는 구간만 연결합니다. 같은 PDF라는 입증이 아니며 원래 입력 좌표·해시는 변경하지 않습니다.</p>
      <h4>준비한 입력 범위</h4><p>준비 기록 {value.prepared.record_count}건 · 중복 제외 {value.prepared.covered_chars}자</p><p>준비 범위: {ranges(value.prepared.ranges)}</p><p>준비되지 않은 추출 범위: {ranges(value.prepared.gaps)}</p>
      {value.prepared.records.length ? <details><summary>원래 준비 기록과 대응 좌표</summary><ul className="review-preparation-list">{value.prepared.records.map(item => <li key={item.preparation_id}>
        <p>추출 좌표: {item.start}–{item.end} · 원래 입력: {item.selection.start}–{item.selection.end}</p><p>{item.mapping === "EXACT_UNIQUE_TEXT_MATCH" ? "원래 입력 텍스트의 유일 일치" : "같은 출처의 좌표"}</p><p>원래 출처: {item.source.source_digest}</p>
        {onOpenPreparation ? <V2Button aria-label={`대응 준비 기록 열기 · ${item.preparation_id}`} onClick={() => onOpenPreparation(item.preparation_id)}>대응 준비 기록 열기</V2Button> : null}
      </li>)}</ul></details> : null}
      <SavedResultCoverage value={value.results} onOpenReceipt={onOpenReceipt}/>
      {gapPreparation ? <ReviewGapPreparation root={root} coverage={value} controls={gapPreparation}
        refresh={async () => read(await getDocumentCoverage(root, record.extraction_id), record)} onOpenPreparation={onOpenPreparation}/> : null}
      <details><summary>페이지별 보존 텍스트와 결과 범위</summary><ul className="review-preparation-list">{value.pages.map(page => <li key={page.page_number}>
        <p>{page.page_number}페이지 · 텍스트 {page.start}–{page.end} · {page.chars}자</p><p>준비 {page.prepared_chars}자 · 저장된 본문 결과 {page.body_result_chars}자</p><p>저장된 본문 결과가 없는 페이지 내 범위: {ranges(page.gaps)}</p>
      </li>)}</ul><p>방문했으나 보존 텍스트 없음: {value.no_text_page_numbers.join(", ") || "없음"} · 미방문: {value.unvisited_page_numbers === null ? "미확인" : value.unvisited_page_numbers.join(", ") || "없음"}</p><p>보존 텍스트가 없거나 방문하지 않은 페이지는 검토 완료나 0자 문서로 판정하지 않습니다.</p></details>
      <section aria-label="대응하지 못한 과거 입력"><h4>대응하지 못한 과거 입력</h4><p>준비 {value.unaligned.preparations.length}건 · 입력·결과 {value.unaligned.receipts.length}건. 위 합계에 포함하지 않았습니다.</p>
        <ul>{value.unaligned.preparations.map(item => <li key={item.preparation_id}>{reason(item.reason)} · {item.preparation_id}{onOpenPreparation && item.preparation_id ? <V2Button onClick={() => onOpenPreparation(item.preparation_id!)}>대응 불가 준비 기록 열기</V2Button> : null}</li>)}
          {value.unaligned.receipts.map(item => <li key={item.receipt_id}>{reason(item.reason)} · {item.receipt_id}{onOpenReceipt && item.receipt_id ? <V2Button onClick={() => onOpenReceipt(item.receipt_id!)}>대응 불가 입력·결과 열기</V2Button> : null}</li>)}</ul>
      </section>
    </> : null}
  </section> : null}</details>;
}
export function ReviewDocumentCoverage(props: Props) { return <Coverage key={JSON.stringify([props.root, props.record.extraction_id])} {...props}/>; }
