import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  createOfflineReview, getOfflineReview, getPreparation, getPreparationSources, listPreparations, preparationRoot, savePreparation,
  type PreparationList, type PreparationRequest, type PreparationSource, type PreparationSources, type ReviewPreparationRecord,
  type OfflineReviewCapability, type OfflineReviewRequest, type OfflineReviewResult, type SourceRange,
} from "../../lib/reviewPreparation";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { ReviewSourcePreview } from "./ReviewSourcePreview";
import { ReviewExecution } from "./ReviewExecution";
import { ReviewTransport } from "./ReviewTransport";
import { ReviewSourceCoverage, type GapPreparationControls } from "./ReviewSourceCoverage";
import { SavedReviewResearchContext } from "./SavedReviewResearchContext";
import { PaperPdfExtraction } from "./PaperPdfExtraction";
import { ReviewDocumentPlan } from "./ReviewDocumentPlan";
import "./ReviewPreparation.css";

type Props = { topicId: string; paperVersionId: string; onOpenReceipt?: (receiptId: string) => void };
const sourceKey = (value: Pick<PreparationSource, "run_id" | "paper_id" | "extraction_id">) => JSON.stringify(value.extraction_id ? [value.run_id, value.paper_id, value.extraction_id] : [value.run_id, value.paper_id]);
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const truncation = (value: string) => value === "TRUNCATED" ? "잘림 확인" : value === "NOT_OBSERVED" ? "잘림 관측 없음" : "미확인";
const integer = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null;
const unavailableReason = (reason: string) => ({
  review_preparation_not_found: "연결된 저장 본문을 찾을 수 없습니다.",
  review_preparation_source_changed: "보존된 추출 기록과 현재 파일이 일치하지 않습니다.",
  review_preparation_source_invalid: "저장 본문의 형식·인코딩 또는 안전한 파일 경로를 확인할 수 없습니다.",
  review_preparation_source_too_large: "저장 본문이 지원하는 크기 한도를 초과합니다. 일부만 잘라서 읽지 않았습니다.",
  review_preparation_source_unsupported: "이 파일 형식은 구간 준비를 지원하지 않습니다. 추출된 텍스트가 필요합니다.",
} as Record<string, string>)[reason] ?? "이 저장 본문을 현재 사용할 수 없습니다.";

function OfflineReview({ root, id, onOpenReceipt }: { root: string; id: string; onOpenReceipt?: Props["onOpenReceipt"] }) {
  const { preferences } = useOptionalAppearance();
  const [capability, setCapability] = useState<OfflineReviewCapability | null>(null), [readError, setReadError] = useState("");
  const [loading, setLoading] = useState(true), [revision, setRevision] = useState(0), [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<OfflineReviewRequest | null>(null), [error, setError] = useState("");
  const [saved, setSaved] = useState<OfflineReviewResult | null>(null);
  const alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setReadError("");
    void getOfflineReview(root, id).then(value => {
      if (!cancelled) { setCapability(value); setSaved(null); setLoading(false); }
    }, reason => { if (!cancelled) { setReadError(message(reason)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [root, id, revision]);
  const result = saved ?? capability?.latest;
  async function run() {
    if (locked.current || (!pending && (loading || !!readError || !capability?.enabled))) return;
    const body = pending ?? { request_id: crypto.randomUUID(), expected_preparation_digest: capability!.preparation_digest };
    locked.current = true; setSaving(true); setPending(body); setError("");
    try {
      const value = await createOfflineReview(root, id, body);
      if (alive.current) { setSaved(value); setPending(null); onOpenReceipt?.(value.receipt_id); }
    } catch (reason) {
      if (alive.current) {
        setError(message(reason));
        if (reason instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(reason.status)) {
          setPending(null); setCapability(null); setRevision(value => value + 1);
        }
      }
    } finally { locked.current = false; if (alive.current) setSaving(false); }
  }
  return <section aria-label="준비 구간 합성 검토" className="review-preparation-record">
    <h4>개발용 합성 검토</h4>
    <p>고정한 구간으로 앱 연결을 시험합니다. 실제 AI 모델·외부 전송·논문 전체 검토를 수행하지 않습니다. 연구 품질·신규성·신뢰도는 미평가입니다.</p>
    <V2Button disabled={loading || saving || pending !== null} onClick={() => setRevision(value => value + 1)}>합성 검토 상태 새로고침</V2Button>
    {loading ? <p role="status">합성 검토 상태 확인 중</p> : readError ? <p role="alert">합성 검토 상태 조회 오류: {readError}</p> : capability ? <>
      {capability.enabled ? <V2Button disabled={saving || pending !== null} busy={saving} onClick={() => void run()}>이 준비 구간으로 합성 검토 결과 만들기</V2Button> : <p>이 환경에서는 합성 검토 실행이 비활성화되어 있습니다. 저장된 입력·결과 이력은 확인할 수 있습니다.</p>}
    </> : null}
    {error ? <p role="alert">합성 검토 오류: {error}</p> : null}
    {pending && !saving ? <><p>응답을 확인하지 못했습니다. 같은 요청을 다시 보내세요. 화면을 떠난 뒤에는 저장된 결과부터 확인하세요.</p><V2Button onClick={() => void run()}>같은 합성 검토 요청 다시 보내기</V2Button></> : null}
    {result ? <div><p>합성 입력·결과 저장됨 · {formatTimestamp(result.created_at, preferences.timeFormat, "ko-KR")}</p><p>실행 기록: {result.execution_id}</p><p>입력 기록: {result.receipt_id}</p>
      <p>보고서 입력 선택과 사람의 채택은 별도입니다.</p>{onOpenReceipt ? <V2Button onClick={() => onOpenReceipt(result.receipt_id)}>저장된 합성 입력·결과 열기</V2Button> : null}</div> : null}
  </section>;
}

function Stored({ root, id, onOpenReceipt, onOpenPreparation, gapPreparation }: { root: string; id: string; onOpenReceipt?: Props["onOpenReceipt"]; onOpenPreparation: (id: string) => void; gapPreparation: GapPreparationControls }) {
  const { preferences } = useOptionalAppearance();
  const [record, setRecord] = useState<ReviewPreparationRecord | null>(null), [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    let cancelled = false; setRecord(null); setError("");
    void getPreparation(root, id).then(value => { if (!cancelled) setRecord(value); }, reason => { if (!cancelled) setError(message(reason)); });
    return () => { cancelled = true; };
  }, [root, id, revision]);
  useEffect(() => { if (record) heading.current?.focus({ preventScroll: true }); }, [record]);
  function download() {
    if (!record) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: "application/json;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `${record.preparation_id}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  return <section className="review-preparation-record" aria-label="보존된 본문 구간 준비">
    <h4 tabIndex={-1} ref={heading}>보존된 본문 구간</h4>
    {error ? <><p role="alert">준비 기록 조회 오류: {error}</p><V2Button onClick={() => setRevision(value => value + 1)}>준비 기록 다시 읽기</V2Button></> : !record ? <p role="status">준비 기록 확인 중</p> : null}
    {record ? <>
      <p>{record.state === "PREPARED_NOT_EXECUTED" && record.execution.started === false ? "준비만 저장됨 · 모델 실행 안 함 · 전체 문서 검토 미확인" : "실행 상태 미확인 · 원문 전체 검토를 의미하지 않습니다."}</p>
      <p>저장 시각: {formatTimestamp(record.created_at, preferences.timeFormat, "ko-KR")}</p>
      <SavedReviewResearchContext value={record.context}/>
      {record.schema_version === "PAPER_REVIEW_PREPARATION_V3" ? <section aria-label="연결된 전문 분석 구간">
        <p>전문 계획의 {record.analysis_unit.unit_index + 1}/{record.analysis_unit.unit_count} 구간 · {record.analysis_unit.label}</p>
        <p>고정 모델: {record.analysis_unit.budget.provider_name} · {record.analysis_unit.budget.model}</p>
        <p>계획: {record.analysis_unit.plan_id}. 위 전문 계획에서 앞뒤 구간의 준비·결과를 함께 확인할 수 있습니다.</p>
        <details><summary>분석 대상과 구분한 주변 문맥</summary><p>문맥은 연결을 돕지만 이 요청에서 분석한 본문 범위·인용 근거에 포함하지 않습니다.</p>
          <pre>{record.analysis_unit.context_before || "앞 문맥 없음"}</pre><pre>{record.analysis_unit.context_after || "뒤 문맥 없음"}</pre>
        </details>
      </section> : null}
      <p>저장된 구간: {record.selection.start}–{record.selection.end} · {record.selection.chars}자 (Unicode 코드 포인트)</p>
      <p>아래는 저장 시점의 정규화된 본문에서 고정한 선택 구간입니다. 좌표는 공백 정규화(XRML_WHITESPACE_V1) 후 본문 기준이며 원본 파일의 문자·바이트 위치와 다를 수 있습니다. 실제 모델에 전달되거나 검토된 결과가 아닙니다.</p>
      <pre tabIndex={0} aria-label="보존된 준비 본문">{record.selection.text}</pre>
      <details><summary>보존된 출처와 식별자</summary>
        <p>준비 기록: {record.preparation_id}</p><p>Run: {record.source.run_id} · 논문: {record.source.paper_id}</p>
        <p>당시 저장 본문: {record.source.source_chars}자 · 추출 잘림: {truncation(record.source.extraction_truncation)}</p>
        <p>출처 관측 SHA-256: {record.source.source_digest}</p><p>선택 구간 SHA-256: {record.selection.sha256}</p>
        <p>파일 참조: {record.source.artifact_ref || "미확인"}</p>
        <p>파일 SHA-256: {record.source.artifact_sha256 || "미확인"} · {record.source.artifact_bytes === null ? "크기 미확인" : `${record.source.artifact_bytes}바이트`}</p>
      </details>
      <V2Button onClick={download}>준비 기록 JSON 내려받기</V2Button>
      <ReviewSourceCoverage root={root} id={id} onOpenReceipt={onOpenReceipt} onOpenPreparation={onOpenPreparation} gapPreparation={gapPreparation}/>
      {record.schema_version !== "PAPER_REVIEW_PREPARATION_V3" ? <><OfflineReview root={root} id={id} onOpenReceipt={onOpenReceipt}/>
      <ReviewExecution root={root} id={id} onOpenReceipt={onOpenReceipt}/></> : <p>이 전문 구간은 고정한 모델 예산을 사용합니다. 아래 Native V2 실행에서 전송 내용과 승인 상태를 확인하세요.</p>}
      <ReviewTransport root={root} id={id} onOpenReceipt={onOpenReceipt}/>
    </> : null}
  </section>;
}

function History({ root, selected, onSelect }: { root: string; selected: string; onSelect: (id: string) => void }) {
  const { preferences } = useOptionalAppearance();
  const [listing, setListing] = useState<PreparationList | null>(null), [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true), [error, setError] = useState(""), [revision, setRevision] = useState(0);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    void listPreparations(root, page).then(value => { if (!cancelled) { setListing(value); setLoading(false); } }, reason => { if (!cancelled) { setError(message(reason)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [root, page, revision]);
  return <section aria-label="본문 구간 준비 이력">
    <h4>저장된 준비 이력</h4><p>현재 본문을 읽을 수 없어도 이미 고정한 구간은 이력에서 확인할 수 있습니다.</p>
    <V2Button disabled={loading} onClick={() => setRevision(value => value + 1)}>준비 이력 새로고침</V2Button>
    {loading ? <p role="status">준비 이력 확인 중</p> : error ? <p role="alert">준비 이력 조회 오류: {error}</p> : listing ? <>
      {listing.items.length ? <ul className="review-preparation-list">{listing.items.map(item => <li key={item.preparation_id}>
        <p>{formatTimestamp(item.created_at, preferences.timeFormat, "ko-KR")} · {item.selection.start}–{item.selection.end} · {item.selection.chars}자</p>
        <V2Button aria-label={`준비 기록 열기 · ${item.preparation_id}`} aria-pressed={selected === item.preparation_id} onClick={() => onSelect(item.preparation_id)}>준비 기록 열기</V2Button>
        <small title={item.preparation_id}>{item.preparation_id.length > 22 ? `${item.preparation_id.slice(0, 20)}…` : item.preparation_id}</small>
      </li>)}</ul> : <p>이 페이지에 저장된 준비 기록이 없습니다.</p>}
      <nav aria-label="준비 이력 페이지"><V2Button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>이전 준비 기록 페이지</V2Button><span>{page} 페이지</span><V2Button disabled={!listing.hasMore} onClick={() => setPage(value => value + 1)}>다음 준비 기록 페이지</V2Button></nav>
    </> : null}
  </section>;
}

function Workspace({ root, topicId, paperVersionId, onOpenReceipt }: Props & { root: string }) {
  const id = useId(), alive = useRef(true), locked = useRef(false), initialized = useRef(false);
  const refreshBaseline = useRef<{ key: string; digest: string } | null>(null);
  const [sources, setSources] = useState<PreparationSources | null>(null), [key, setKey] = useState("");
  const [sourceLoading, setSourceLoading] = useState(true), [sourceError, setSourceError] = useState(""), [sourceRevision, setSourceRevision] = useState(0);
  const [start, setStart] = useState("0"), [end, setEnd] = useState("");
  const [pending, setPending] = useState<PreparationRequest | null>(null), [saving, setSaving] = useState(false), [needsRefresh, setNeedsRefresh] = useState(false);
  const [saveError, setSaveError] = useState(""), [notice, setNotice] = useState("");
  const [historyRevision, setHistoryRevision] = useState(0), [selected, setSelected] = useState(""), [detailRevision, setDetailRevision] = useState(0);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [gapBusy, setGapBusy] = useState(false);
  const [planBusy, setPlanBusy] = useState(false);
  const rangeInput = useRef<HTMLInputElement>(null);
  const [sourceChanged, setSourceChanged] = useState(false), [sourceUnavailable, setSourceUnavailable] = useState(false);
  function clearSourceNotice() { setSourceChanged(false); setSourceUnavailable(false); }
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setSourceLoading(true); setSourceError("");
    void getPreparationSources(root).then(value => {
      if (cancelled) return;
      setSources(value); setSourceLoading(false); setNeedsRefresh(false);
      const previous = refreshBaseline.current;
      const refreshed = previous ? value.sources.find(item => sourceKey(item) === previous.key) : undefined;
      if (previous) setSourceUnavailable(!refreshed);
      if (refreshed && previous && refreshed.source_digest !== previous.digest) setSourceChanged(true);
      if (!initialized.current && value.sources.length) {
        initialized.current = true; setKey(sourceKey(value.sources[0])); setEnd(String(Math.min(6000, value.sources[0].source_chars, value.limits.max_selected_chars)));
      }
    }, reason => { if (!cancelled) { setSourceError(message(reason)); setSourceLoading(false); } });
    return () => { cancelled = true; };
  }, [root, sourceRevision]);
  const selectedSource = sources?.sources.find(value => sourceKey(value) === key);
  const first = integer(start), last = integer(end), limit = sources?.limits.max_selected_chars ?? 12000;
  const validRange = !!selectedSource && first !== null && last !== null && first < last && last <= selectedSource.source_chars && last - first <= limit;
  const fieldsDisabled = saving || pending !== null || sourceLoading || !!sourceError || needsRefresh || previewBusy || gapBusy || planBusy || !sources?.sources.length;
  const ready = validRange && !fieldsDisabled;
  const gapDisabledReason = saving || pending !== null ? "저장 요청이 진행 중이거나 응답이 미확정입니다. 같은 저장 요청을 먼저 확인하세요."
    : previewBusy ? "현재 본문 미리보기를 마치거나 취소한 뒤 다른 구간을 여세요." : "";
  function useGap(source: PreparationSource, range: SourceRange): void | string {
    if (locked.current || saving || pending !== null || previewBusy || gapBusy || planBusy) return "진행 중인 요청을 먼저 마치거나 확인하세요. 기존 입력은 유지됩니다.";
    const match = sources?.sources.find(value => sourceKey(value) === sourceKey(source) && value.source_digest === source.source_digest)
      ?? (!source.extraction_id ? sources?.sources.find(value => value.run_id === source.run_id && value.paper_id === source.paper_id && value.source_digest === source.source_digest && value.source_chars === source.source_chars) : undefined);
    if (sourceLoading || sourceError || needsRefresh || !match || match.source_digest !== source.source_digest || match.source_chars !== source.source_chars) return "현재 출처 목록과 미리보기 출처가 일치하지 않습니다. 저장 본문 출처를 새로고침하세요. 기존 입력은 유지됩니다.";
    if (!Number.isSafeInteger(range.start) || !Number.isSafeInteger(range.end) || range.start < 0 || range.end <= range.start || range.end > match.source_chars || range.end - range.start > limit) return "선택한 범위가 유효하지 않습니다. 기존 입력은 유지됩니다.";
    setKey(sourceKey(match)); setStart(String(range.start)); setEnd(String(range.end)); setSaveError(""); clearSourceNotice();
    setNotice("미리본 본문의 선택 구간으로 입력 출처·범위를 바꿨습니다. 아직 저장하거나 모델을 실행하지 않았습니다.");
    rangeInput.current?.focus();
  }
  function choose(value: string) {
    setKey(value); setSaveError(""); setNotice(""); clearSourceNotice();
    refreshBaseline.current = null;
    const next = sources?.sources.find(item => sourceKey(item) === value);
    if (next) { setStart("0"); setEnd(String(Math.min(6000, next.source_chars, limit))); }
  }
  async function save(event?: FormEvent) {
    event?.preventDefault();
    if (locked.current || (!pending && !ready)) return;
    const body = pending ?? { request_id: crypto.randomUUID(), run_id: selectedSource!.run_id, paper_id: selectedSource!.paper_id, expected_source_digest: selectedSource!.source_digest, start: first!, end: last! };
    locked.current = true; setSaving(true); setPending(body); setSaveError(""); setNotice("");
    try {
      const record = await savePreparation(root, body);
      if (alive.current) {
        setPending(null); setSelected(record.preparation_id); setDetailRevision(value => value + 1); setHistoryRevision(value => value + 1);
        clearSourceNotice();
        setNotice("구간을 고정했습니다. 모델을 실행하지 않았으며 저장된 기록을 다시 읽습니다.");
      }
    } catch (error) {
      if (alive.current) {
        setSaveError(message(error));
        if (error instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(error.status)) {
          setPending(null);
          if (error.status === 409 || error.status === 404) setNeedsRefresh(true);
        }
      }
    } finally { locked.current = false; if (alive.current) setSaving(false); }
  }
  return <div className="review-preparation-body">
    <p>저장 본문을 전문·절별 계획 또는 수동 발췌 구간으로 고정합니다. 이 단계는 모델 실행·외부 전송·검토 결과 생성을 하지 않습니다.</p>
    <ReviewDocumentPlan root={root} source={sourceLoading || sourceError || needsRefresh ? undefined : selectedSource}
      disabled={saving || pending !== null || previewBusy || gapBusy} onBusyChange={setPlanBusy} onOpenReceipt={onOpenReceipt}
      onOpenPreparation={value => { setSelected(value); setDetailRevision(count => count + 1); setHistoryRevision(count => count + 1); }}/>
    <PaperPdfExtraction root={root} topicId={topicId} paperVersionId={paperVersionId} onOpenReceipt={onOpenReceipt}
      onOpenPreparation={value => { setSelected(value); setDetailRevision(count => count + 1); setHistoryRevision(count => count + 1); }}
      gapPreparation={{ busy: gapBusy || planBusy, disabledReason: planBusy ? "전문 계획 요청을 먼저 확인하세요." : gapDisabledReason, selectionLimit: limit, onBusyChange: setGapBusy, onUseRange: useGap }}/>
    <V2Button disabled={sourceLoading || saving || pending !== null || previewBusy || gapBusy || planBusy} onClick={() => {
      refreshBaseline.current = selectedSource ? { key, digest: selectedSource.source_digest } : refreshBaseline.current;
      setSourceRevision(value => value + 1);
    }}>저장 본문 출처 새로고침</V2Button>
    {sourceUnavailable ? <p role="status">선택했던 출처를 현재 사용할 수 없습니다. 입력 위치를 유지했습니다. 아래 이유를 확인하거나 다른 출처를 직접 선택하세요.</p>
      : sourceChanged ? <p role="status">출처가 변경되어 숫자 위치만 유지했습니다. 본문을 미리보고 범위를 다시 확인하세요.</p> : null}
    {sourceLoading ? <p role="status">저장 본문 출처 확인 중</p> : sourceError ? <p role="alert">저장 본문 출처 조회 오류: {sourceError}</p> : !sources?.sources.length ? <p>준비할 수 있는 저장 본문이 없습니다. 기존 준비 이력은 아래에서 확인하세요.</p> : null}
    {!sourceLoading && !sourceError && !!sources?.unavailable_sources?.length ? <section aria-label="사용할 수 없는 저장 본문 출처">
      <h4>사용할 수 없는 저장 본문 출처</h4>
      <p>아래 출처는 선택할 수 없습니다. 사용 가능한 다른 출처는 계속 준비할 수 있으며, 이미 고정한 기록은 변경되지 않습니다.</p>
      <ul className="review-preparation-list">{sources.unavailable_sources.map(value => <li key={value.diagnostic_id}>
        <p>Run: {value.run_id}</p><p>{unavailableReason(value.reason)}</p>
        {value.extraction_id ? <p>PDF 추출 기록: {value.extraction_id}</p> : null}
      </li>)}</ul>
      <p>파일·연결을 확인한 후 저장 본문 출처를 새로고침하세요. 변경·손상된 파일은 기존 본문 추출·연결 경로에서 다시 준비해야 합니다.</p>
    </section> : null}
    {sourceError.includes("source_changed") ? <p>보존된 추출 출처와 현재 파일이 일치하지 않습니다. 새로고침 후에도 같으면 기존 본문 추출·연결 경로에서 출처를 다시 생성해 연결해야 합니다. 과거 준비 이력은 그대로 확인할 수 있습니다.</p> : null}
    {sources?.sources.length || initialized.current ? <>
      <fieldset disabled={fieldsDisabled}><legend>고정할 입력 출처</legend>
        <label htmlFor={`${id}-source`}>저장 본문 출처</label>
        <select id={`${id}-source`} value={selectedSource ? key : ""} onChange={event => choose(event.target.value)}>
          {!selectedSource ? <option value="">출처를 다시 선택하세요</option> : null}
          {sources?.sources.map(value => <option key={sourceKey(value)} value={sourceKey(value)}>{value.run_id} · {value.paper_id} · {value.source_chars}자{value.extraction_id ? ` · PDF 추출 ${value.extraction_id}` : ""}</option>)}
        </select>
        {selectedSource ? <p>저장 본문 {selectedSource.source_chars}자 · 추출 잘림: {truncation(selectedSource.extraction_truncation)}. 전체 문서 확보·검토 여부와는 별개입니다.</p> : null}
      </fieldset>
      {selectedSource && !sourceLoading && !sourceError && !saving && pending === null ? <ReviewSourcePreview
        key={JSON.stringify([root, key, selectedSource.source_digest, sourceRevision])} root={root} source={selectedSource}
        disabled={needsRefresh || gapBusy || planBusy} selectionLimit={limit} onBusyChange={setPreviewBusy} onSourceInvalid={() => setNeedsRefresh(true)}
        onUseRange={range => { setStart(String(range.start)); setEnd(String(range.end)); setSaveError(""); setNotice(""); }}/>
        : null}
      <form aria-label="본문 구간 고정" onSubmit={event => void save(event)}>
      <fieldset disabled={fieldsDisabled}><legend>고정할 입력 범위</legend>
        <p id={`${id}-range-note`}>위치는 0부터 시작하며 끝 위치는 포함하지 않습니다. Unicode 코드 포인트 기준으로 결합 문자도 따로 셉니다. NUL·연속 공백을 공백 하나로 정리하고 양끝 공백을 제거한 본문(XRML_WHITESPACE_V1)의 위치이며 원본 파일의 문자·바이트 위치와 다를 수 있습니다. 결합 문자는 합치지 않습니다. 이 수동 발췌 양식의 기존 계약은 최대 {limit.toLocaleString("ko-KR")}자입니다. 전문·긴 구간은 위 모델 예산 계획을 사용하세요.</p>
        <div className="review-preparation-range"><label htmlFor={`${id}-start`}>시작 위치<input ref={rangeInput} id={`${id}-start`} type="number" min="0" step="1" value={start} aria-describedby={`${id}-range-note`} onChange={event => setStart(event.target.value)}/></label>
          <label htmlFor={`${id}-end`}>끝 위치 (포함하지 않음)<input id={`${id}-end`} type="number" min="1" step="1" value={end} aria-describedby={`${id}-range-note`} onChange={event => setEnd(event.target.value)}/></label></div>
      </fieldset>
      {!validRange ? <p>시작보다 큰 끝 위치를 저장 본문 길이 안에서 지정하세요. 선택 길이는 {limit.toLocaleString("ko-KR")}자 이하여야 합니다.</p> : <p>고정할 길이: {last! - first!}자. 저장 성공 후 고정한 본문을 보존 기록에서 다시 읽습니다.</p>}
      <V2Button type="submit" disabled={!ready} busy={saving}>선택 구간 고정</V2Button>
    </form></> : null}
    {saveError ? <p role="alert">구간 고정 오류: {saveError}. 입력 위치는 유지됩니다.</p> : null}
    {needsRefresh ? <p>출처를 다시 확인해야 합니다. 저장 본문 출처 새로고침 후 유지된 위치가 올바른지 확인하세요. 출처 변경 오류가 계속되면 기존 본문 추출·연결 경로에서 출처를 다시 생성해 연결해야 합니다.</p> : null}
    {pending && !saving ? <div><p>저장 응답을 확인하지 못했습니다. 입력을 잠시 유지하며 재시도는 같은 요청을 보냅니다. 이 화면을 나갔다 돌아오면 새로 고정하기 전에 저장된 준비 이력을 확인하세요.</p><V2Button onClick={() => void save()}>같은 고정 요청 다시 보내기</V2Button></div> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <History key={historyRevision} root={root} selected={selected} onSelect={value => { setSelected(value); setDetailRevision(count => count + 1); }}/>
    {selected ? <Stored key={`${selected}:${detailRevision}`} root={root} id={selected} onOpenReceipt={onOpenReceipt} onOpenPreparation={value => { setSelected(value); setDetailRevision(count => count + 1); }} gapPreparation={{ busy: gapBusy || planBusy, disabledReason: planBusy ? "전문 계획 요청을 먼저 확인하세요." : gapDisabledReason, selectionLimit: limit, onBusyChange: setGapBusy, onUseRange: useGap }}/> : null}
  </div>;
}

function Panel({ topicId, paperVersionId, onOpenReceipt }: Props) {
  const [opened, setOpened] = useState(false);
  return <details className="review-preparation" onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>본문 구간 검토 준비</summary>{opened ? <Workspace root={preparationRoot(topicId, paperVersionId)} topicId={topicId} paperVersionId={paperVersionId} onOpenReceipt={onOpenReceipt}/> : null}</details>;
}
export function ReviewPreparation(props: Props) { return <Panel key={JSON.stringify([props.topicId, props.paperVersionId])} {...props}/>; }
