import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { createPdfExtraction, getPdfExtraction, getPdfExtractions, type LocalPdf, type PdfExtractionInventory, type PdfExtractionRecord, type PdfExtractionRequest } from "../../lib/reviewPreparation";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { ReviewDocumentCoverage } from "./ReviewDocumentCoverage";
import type { GapPreparationControls } from "./ReviewSourceCoverage";
import { PaperPdfAttachment } from "./PaperPdfAttachment";

type Scope = { root: string; topicId: string; paperVersionId: string };
type Props = Scope & { onOpenPreparation?: (id: string) => void; onOpenReceipt?: (id: string) => void; gapPreparation?: GapPreparationControls };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && !!value.trim();
const count = (value: unknown, minimum = 0): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const numbers = (value: unknown): value is number[] => Array.isArray(value) && value.every((number, index) => count(number, 1) && (!index || number > value[index - 1]));
const pages = (value: number[] | null) => value === null ? "미확인" : value.length ? value.join(", ") : "없음";
const key = (value: LocalPdf) => JSON.stringify([value.run_id, value.paper_id, value.pdf_sha256, ...(value.attachment_id ? [value.attachment_id] : [])]);
const failure = (reason: unknown) => reason instanceof ApiError && reason.status === 403 ? "이 주제에 대한 읽기·수정 권한을 확인하세요. 입력과 보존 이력은 자동으로 변경하지 않았습니다." : reason instanceof Error ? reason.message : String(reason);
const reasonLabel = (reason: string) => ({
  review_pdf_source_missing: "연결된 로컬 PDF를 찾을 수 없습니다.", review_pdf_source_invalid: "PDF 파일·경로를 안전하게 확인할 수 없습니다.",
  review_pdf_source_too_large: "PDF가 로컬 추출 크기 한도를 넘습니다.", review_pdf_source_changed: "선택 당시와 PDF가 달라졌습니다. 목록을 다시 확인하세요.",
  review_pdf_extraction_failed: "PDF 텍스트 추출에 실패했습니다. OCR은 수행하지 않습니다.", review_pdf_extraction_conflict: "동일 추출의 저장 상태를 확인할 수 없습니다. 보존 이력을 다시 읽으세요.",
  review_pdf_extraction_limit_exceeded: "추출 이력 조회 한도를 넘었습니다. 일부 목록을 전체인 것처럼 표시하지 않았습니다.",
  review_pdf_extraction_integrity_invalid: "보존한 추출 파일·기록의 무결성을 확인할 수 없습니다.",
  review_pdf_engine_unavailable: "로컬 PDF 추출 엔진(PyMuPDF)이 필요합니다. 자동 설치하거나 외부 서비스를 호출하지 않았습니다.",
  review_pdf_encrypted: "암호화된 PDF는 로컬 추출을 지원하지 않습니다.", review_pdf_page_limit_exceeded: "PDF가 페이지 수 상한을 넘습니다. 일부 페이지만 처리하지 않았습니다.",
} as Record<string, string>)[reason] ?? reason;

export function readPdfExtraction(value: unknown, scope: Pick<Scope, "topicId" | "paperVersionId">): PdfExtractionRecord {
  const invalid = () => { throw new Error("요청한 논문의 보존 추출 기록·좌표·출처를 확인할 수 없습니다."); };
  if (!object(value) || !["PAPER_PDF_EXTRACTION_V1", "PAPER_PDF_EXTRACTION_V2"].includes(String(value.schema_version)) || !text(value.extraction_id)
    || value.topic_id !== scope.topicId || value.paper_version_id !== scope.paperVersionId || !text(value.created_by)
    || !text(value.created_at) || !Number.isFinite(Date.parse(value.created_at)) || value.policy_version !== "LOCAL_PDF_TEXT_V1"
    || !object(value.pdf) || !hash(value.pdf.sha256) || !count(value.pdf.bytes, 1) || !text(value.pdf.artifact_ref)
    || (value.schema_version === "PAPER_PDF_EXTRACTION_V2" ? !/^pdfa_[a-f0-9]{64}$/.test(String(value.pdf.attachment_id)) || value.pdf.artifact_ref !== `run:attachments/pdf/${value.pdf.sha256}.pdf` : value.pdf.attachment_id !== undefined)
    || !object(value.source) || !text(value.source.run_id) || !text(value.source.paper_id)
    || !hash(value.source.source_digest) || !count(value.source.source_chars, 1) || !text(value.source.source_kind)
    || !hash(value.source.artifact_sha256) || !count(value.source.artifact_bytes, 1) || !text(value.source.artifact_ref)
    || !object(value.execution) || value.execution.started !== false || value.execution.full_document_review !== "NOT_ESTABLISHED"
    || value.scientific_support !== "NOT_ASSESSED" || !object(value.provenance)) return invalid();
  const p = value.provenance, source = value.source;
  if (p.schema_version !== "PDF_EXTRACTION_PROVENANCE_V1" || !object(p.source_pdf) || p.source_pdf.sha256 !== value.pdf.sha256 || p.source_pdf.bytes !== value.pdf.bytes
    || !object(p.extractor) || !text(p.extractor.name) || !(p.extractor.version === null || text(p.extractor.version)) || p.extractor.normalization !== "XRML_WHITESPACE_V1"
    || !count(p.max_chars, 1) || !(p.document_page_count === null || count(p.document_page_count, 1))
    || !(p.document_text_chars === null || count(p.document_text_chars, 1)) || !numbers(p.visited_page_numbers) || !numbers(p.retained_page_numbers)
    || !["TRUNCATED", "NOT_OBSERVED", "UNKNOWN"].includes(String(p.truncation)) || source.extraction_truncation !== p.truncation
    || !object(p.outputs) || p.outputs.text_sha256 !== source.artifact_sha256 || p.outputs.text_bytes !== source.artifact_bytes
    || !hash(p.outputs.pages_sha256) || !count(p.outputs.pages_bytes, 1) || !numbers(value.no_text_page_numbers)
    || !(value.unvisited_page_numbers === null || numbers(value.unvisited_page_numbers)) || !Array.isArray(value.page_ranges)) return invalid();
  const ranges = value.page_ranges, retained = p.retained_page_numbers, visited = p.visited_page_numbers;
  if (!ranges.length || ranges.length !== retained.length || ranges.some((range, index) => !object(range)
    || range.page_number !== retained[index] || !count(range.start) || !count(range.end, 1) || range.start >= range.end
    || range.end > (source.source_chars as number) || range.chars !== range.end - range.start
    || (index > 0 && range.start < ranges[index - 1].end))
    || value.no_text_page_numbers.some(page => !visited.includes(page) || retained.includes(page))
    || (p.document_page_count === null && value.unvisited_page_numbers !== null)
    || (p.document_page_count !== null && (value.unvisited_page_numbers === null || [...p.visited_page_numbers, ...retained, ...value.unvisited_page_numbers].some(page => page > (p.document_page_count as number))))) return invalid();
  return value as unknown as PdfExtractionRecord;
}

function readInventory(value: unknown, scope: Scope): PdfExtractionInventory {
  if (!object(value) || value.schema_version !== "PAPER_PDF_EXTRACTION_INVENTORY_V1" || value.state !== "NOT_EXECUTED"
    || !Array.isArray(value.pdfs) || !Array.isArray(value.extractions) || !Array.isArray(value.unavailable) || !object(value.limits)
    || ![value.limits.max_pdf_bytes, value.limits.max_source_chars, value.limits.max_artifact_bytes, value.limits.max_document_pages].every(item => count(item, 1))
    || value.pdfs.some(pdf => !object(pdf) || !text(pdf.run_id) || !text(pdf.paper_id) || !hash(pdf.pdf_sha256) || !count(pdf.pdf_bytes, 1) || (pdf.attachment_id !== undefined && !/^pdfa_[a-f0-9]{64}$/.test(String(pdf.attachment_id))))
    || value.unavailable.some(item => !object(item) || !text(item.diagnostic_id) || !text(item.run_id) || !text(item.reason))) throw new Error("PDF 목록 응답을 확인할 수 없습니다.");
  const extractions = value.extractions.map(item => readPdfExtraction(item, scope));
  if (new Set(extractions.map(item => item.extraction_id)).size !== extractions.length || new Set((value.pdfs as LocalPdf[]).map(key)).size !== value.pdfs.length) throw new Error("PDF 목록에 중복 식별자가 있습니다.");
  return { ...value, extractions } as unknown as PdfExtractionInventory;
}

function ExtractionDetails({ record, ...props }: Props & { record: PdfExtractionRecord }) {
  const { preferences } = useOptionalAppearance(), p = record.provenance;
  return <section className="review-preparation-record" aria-label="보존된 PDF 추출">
    <h4>보존된 PDF 텍스트 추출</h4>
    <p>추출 시각: {formatTimestamp(record.created_at, preferences.timeFormat, "ko-KR")} · 저장 사용자: {record.created_by}</p>
    <p>보존된 정규화 텍스트 {record.source.source_chars.toLocaleString("ko-KR")}자 · PDF {record.pdf.bytes.toLocaleString("ko-KR")}바이트</p>
    <p>추출 엔진: {p.extractor.name} · 버전: {p.extractor.version ?? "미확인"}</p>
    <p>{p.truncation === "TRUNCATED" ? "추출 잘림 확인 · 일부 텍스트만 보존됐습니다." : p.truncation === "UNKNOWN" ? "추출 범위 미확인 · 엔진이 전체 문서 추출 여부를 입증하지 않았습니다." : "추출 잘림 관측 없음 · 모든 내용의 정확한 추출이나 모델 검토를 뜻하지 않습니다."}</p>
    <p>문서 페이지 수: {p.document_page_count ?? "미확인"} · 방문 페이지: {pages(p.visited_page_numbers)} · 텍스트 보존 페이지: {pages(p.retained_page_numbers)}</p>
    <p>방문했으나 보존 텍스트가 없는 페이지: {pages(record.no_text_page_numbers)}. 빈 페이지로 판정한 것이 아니며 이미지·수식·표의 내용은 추출되지 않았을 수 있습니다.</p>
    <p>방문하지 않은 페이지: {pages(record.unvisited_page_numbers)}</p>
    <p>모델 실행 안 함 · OCR 안 함 · 전체 문서 검토 미확인 · 과학적 뒷받침 미평가</p>
    <details><summary>추출 원본·텍스트 식별자</summary><p>추출 기록: {record.extraction_id}</p><p>Run: {record.source.run_id} · 논문: {record.source.paper_id}</p><p>원본 PDF SHA-256: {record.pdf.sha256}</p><p>보존 텍스트 SHA-256: {record.source.artifact_sha256}</p><p>출처 관측 SHA-256: {record.source.source_digest}</p><p>원본 참조: {record.pdf.artifact_ref}</p><p>추출 텍스트 참조: {record.source.artifact_ref}</p></details>
    <p>추출 텍스트를 준비하려면 저장 본문 출처를 새로고침한 뒤 이 추출 ID를 직접 선택하세요. 기존 입력 범위는 자동으로 바뀌지 않습니다.</p>
    <ReviewDocumentCoverage root={props.root} record={record} onOpenPreparation={props.onOpenPreparation} onOpenReceipt={props.onOpenReceipt} gapPreparation={props.gapPreparation}/>
  </section>;
}

function Workspace(props: Props) {
  const { preferences } = useOptionalAppearance(), id = useId(), alive = useRef(true), busyRef = useRef(false);
  const [listing, setListing] = useState<PdfExtractionInventory | null>(null), [fresh, setFresh] = useState(false), [loading, setLoading] = useState(true);
  const [chosen, setChosen] = useState(""), [readError, setReadError] = useState(""), [revision, setRevision] = useState(0);
  const [pending, setPending] = useState<{ body: PdfExtractionRequest; pdf: LocalPdf } | null>(null), [saving, setSaving] = useState(false), [saveError, setSaveError] = useState("");
  const [record, setRecord] = useState<PdfExtractionRecord | null>(null), [recordError, setRecordError] = useState(""), [recordLoading, setRecordLoading] = useState(false), [notice, setNotice] = useState("");
  const readSequence = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; readSequence.current += 1; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setFresh(false); setReadError("");
    void getPdfExtractions(props.root).then(value => { const next = readInventory(value, props); if (!cancelled) { setListing(next); setFresh(true); setLoading(false); } }).catch(error => { if (!cancelled) { setReadError(failure(error)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [props.root, props.topicId, props.paperVersionId, revision]);
  const selected = listing?.pdfs.find(pdf => key(pdf) === chosen);
  async function save() {
    if (props.gapPreparation?.busy || busyRef.current || (!pending && (!fresh || !selected))) return;
    const command = pending ?? { body: { run_id: selected!.run_id, paper_id: selected!.paper_id, expected_pdf_sha256: selected!.pdf_sha256, ...(selected!.attachment_id ? { attachment_id: selected!.attachment_id } : {}) }, pdf: selected! };
    busyRef.current = true; setSaving(true); setPending(command); setSaveError(""); setNotice(""); readSequence.current += 1; setRecordLoading(false);
    try {
      const value = await createPdfExtraction(props.root, command.body);
      if (!object(value) || typeof value.created !== "boolean") throw new Error("PDF 추출 저장 응답을 확인할 수 없습니다.");
      const saved = readPdfExtraction(value.extraction, props);
      if (saved.source.run_id !== command.body.run_id || saved.source.paper_id !== command.body.paper_id || saved.pdf.sha256 !== command.body.expected_pdf_sha256 || saved.pdf.bytes !== command.pdf.pdf_bytes || saved.pdf.attachment_id !== command.body.attachment_id) throw new Error("선택한 PDF와 추출 저장 응답이 다릅니다.");
      if (!alive.current) return;
      setPending(null); setRecord(saved); setRecordError(""); setNotice(value.created ? "PDF 텍스트 추출을 보존했습니다. 모델을 실행하거나 준비 구간을 바꾸지 않았습니다." : "동일 PDF의 보존 추출 기록을 확인했습니다. 모델을 실행하지 않았습니다.");
      setListing(current => current ? { ...current, extractions: [saved, ...current.extractions.filter(item => item.extraction_id !== saved.extraction_id)] } : current);
    } catch (error) {
      if (alive.current) { setSaveError(reasonLabel(failure(error))); if (error instanceof ApiError && [400, 401, 403, 404, 409, 413, 422].includes(error.status)) { setPending(null); setFresh(false); } }
    } finally { busyRef.current = false; if (alive.current) setSaving(false); }
  }
  async function read(id: string) {
    if (props.gapPreparation?.busy || saving || pending) return;
    const sequence = ++readSequence.current; setRecord(null); setRecordError(""); setRecordLoading(true); setNotice("");
    try { const value = readPdfExtraction(await getPdfExtraction(props.root, id), props); if (value.extraction_id !== id) throw new Error("선택한 추출 기록과 응답이 다릅니다."); if (alive.current && sequence === readSequence.current) setRecord(value); }
    catch (error) { if (alive.current && sequence === readSequence.current) setRecordError(failure(error)); }
    finally { if (alive.current && sequence === readSequence.current) setRecordLoading(false); }
  }
  return <section aria-label="로컬 PDF 추출">
    <p>이미 연결된 로컬 PDF의 텍스트를 추출해 별도 이력으로 보존합니다. 파일을 외부로 보내거나 모델·OCR을 실행하지 않습니다.</p>
    <PaperPdfAttachment root={props.root} topicId={props.topicId} paperVersionId={props.paperVersionId} disabled={!!props.gapPreparation?.busy || saving || pending !== null} onAttached={() => setRevision(value => value + 1)}/>
    <V2Button disabled={!!props.gapPreparation?.busy || loading || saving || pending !== null} onClick={() => setRevision(value => value + 1)}>PDF·추출 이력 새로고침</V2Button>
    {loading ? <p role="status">PDF·추출 이력 확인 중</p> : readError ? <p role="alert">PDF 목록 조회 오류: {reasonLabel(readError)}</p> : null}
    {listing ? <>
      <p>한도: PDF {listing.limits.max_pdf_bytes.toLocaleString("ko-KR")}바이트 · {listing.limits.max_document_pages.toLocaleString("ko-KR")}페이지 · 보존 텍스트 최대 {listing.limits.max_source_chars.toLocaleString("ko-KR")}자. 추출 잘림이나 엔진 범위를 확인하지 못하면 일부·미확인 상태를 표시합니다.</p>
      {!fresh && !loading ? <p>이전 목록을 유지하고 있습니다. 새 추출 전 현재 PDF 목록을 다시 확인하세요.</p> : null}
      {listing.pdfs.length ? <><label htmlFor={id}>추출할 로컬 PDF</label><select id={id} value={selected ? chosen : ""} disabled={!!props.gapPreparation?.busy || !fresh || loading || saving || pending !== null} onChange={event => { setChosen(event.target.value); setSaveError(""); setNotice(""); }}>
        <option value="">PDF를 직접 선택하세요</option>{listing.pdfs.map(pdf => <option key={key(pdf)} value={key(pdf)}>{pdf.run_id} · {pdf.paper_id} · {pdf.pdf_bytes.toLocaleString("ko-KR")}바이트 · {pdf.attachment_id ? "사용자 첨부" : "수집 원본"} · {pdf.pdf_sha256.slice(0, 12)}</option>)}</select>
        {selected ? <p>선택한 PDF SHA-256: {selected.pdf_sha256}</p> : null}
        <V2Button disabled={!!props.gapPreparation?.busy || !fresh || !selected || saving || pending !== null} busy={saving} onClick={() => void save()}>선택한 PDF 전체 텍스트 추출</V2Button>
      </> : fresh ? <p>이 논문에 연결된 로컬 PDF가 없습니다.</p> : null}
      {listing.unavailable.length ? <section aria-label="PDF를 사용할 수 없는 출처 관측"><h4>PDF를 사용할 수 없는 출처 관측</h4><p>한 논문에는 여러 출처 관측이 연결될 수 있습니다. 각 관측을 별도로 확인하므로 사용 가능한 PDF와 아래 진단이 함께 표시될 수 있습니다.</p><ul>{listing.unavailable.map(item => <li key={item.diagnostic_id}>Run: {item.run_id} · {reasonLabel(item.reason)}</li>)}</ul></section> : null}
      <h4>보존된 PDF 추출 이력</h4>{listing.extractions.length ? <ul className="review-preparation-list">{listing.extractions.map(item => <li key={item.extraction_id}>
        <p>{formatTimestamp(item.created_at, preferences.timeFormat, "ko-KR")} · {item.source.source_chars.toLocaleString("ko-KR")}자</p><V2Button disabled={!!props.gapPreparation?.busy || saving || pending !== null} aria-label={`PDF 추출 기록 열기 · ${item.extraction_id}`} onClick={() => void read(item.extraction_id)}>PDF 추출 기록 열기</V2Button>
      </li>)}</ul> : fresh ? <p>보존된 PDF 추출 기록이 없습니다. 위 PDF를 선택해 명시적으로 추출할 수 있습니다.</p> : null}
    </> : null}
    {saveError ? <p role="alert">PDF 추출 오류: {saveError}</p> : null}
    {pending && !saving ? <><p>추출 저장 응답을 확인하지 못했습니다. 같은 PDF·해시로 다시 확인하며 자동 재시도하지 않습니다.</p><V2Button onClick={() => void save()}>같은 PDF 추출 요청 다시 확인</V2Button></> : null}
    {notice ? <p role="status">{notice}</p> : null}{recordLoading ? <p role="status">보존된 추출 기록 확인 중</p> : recordError ? <p role="alert">추출 기록 조회 오류: {recordError}</p> : null}
    {record ? <ExtractionDetails {...props} record={record}/> : null}
  </section>;
}
function Panel(props: Props) { const [opened, setOpened] = useState(false); return <details className="review-preparation-record" onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>PDF 전체 텍스트 추출·범위 확인</summary>{opened ? <Workspace {...props}/> : null}</details>; }
export function PaperPdfExtraction(props: Props) { return <Panel key={JSON.stringify([props.root, props.topicId, props.paperVersionId])} {...props}/>; }
