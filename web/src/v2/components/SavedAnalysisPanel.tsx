import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { draftInput, SavedAnalysisDraftBoundary, useAnalysisDraft } from "../SavedAnalysisDrafts";
import { getAnalyses, getAnalysis, importAnalysis, saveAnalysisReview, type AnalysisList, type AnalysisState, type SavedAnalysis } from "../savedAnalyses";
import { PreservedReviewContext } from "./ReviewInputHistory";
import { SavedAnalysisJournal } from "./SavedAnalysisJournal";
import { CapturedAnalysisSave } from "./CapturedAnalysisSave";
import { PaperBrief } from "./PaperBrief";
import "./SavedAnalysisPanel.css";

const labels = { needs_review: "검토 필요", reviewed: "직접 확인함", rejected: "사용 보류" };
const prose = { whiteSpace: "pre-wrap" as const, overflowWrap: "anywhere" as const };
function Review({ topicId, record, onSaved }: { topicId: string; record: SavedAnalysis; onSaved: (value: SavedAnalysis) => void }) {
  const latest = record.latest_decision;
  const initial = { state: latest?.state ?? "needs_review" as AnalysisState, note: latest?.note ?? "" };
  const { value, get, update } = useAnalysisDraft(JSON.stringify([topicId, record.analysis_id]), { ...initial, base: latest?.decision_id ?? null, baseline: draftInput(initial), busy: false, attempt: null, error: "", message: "", conflict: false });
  const { preferences } = useOptionalAppearance();
  const time = (stamp: string) => formatTimestamp(stamp, preferences.timeFormat, "ko-KR");
  const stale = value.base !== (latest?.decision_id ?? null);
  const save = async () => {
    const current = get(); if (current.busy || current.conflict || (stale && !current.attempt)) return;
    const command = current.attempt ?? { request_id: crypto.randomUUID(), expected_previous_id: current.base, state: current.state, note: current.note };
    update(old => ({ ...old, busy: true, attempt: command, error: "", message: "" }));
    try {
      const saved = await saveAnalysisReview(topicId, record.analysis_id, command);
      const acknowledged = saved.decisions.find(entry => entry.command.request_id === command.request_id);
      if (!acknowledged) throw new Error("review acknowledgement missing");
      // A replay can return history with a newer review. Do not silently rebase
      // the old form onto that newer decision or claim its note was our save.
      update(old => ({ ...old, busy: false, attempt: null, base: acknowledged.decision_id, baseline: draftInput(old), error: "", message: "검토를 새 이력으로 저장했습니다." }));
      onSaved(saved);
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const invalid = error instanceof ApiError && error.status === 422;
      update(old => ({ ...old, busy: false, conflict, attempt: invalid ? null : old.attempt, error: conflict ? "다른 검토가 먼저 저장되었습니다. 이력을 새로고침하고 비교한 뒤 입력을 이어가세요." : invalid ? "검토 입력이 거부되어 저장되지 않았습니다. 내용을 수정한 뒤 다시 저장하세요." : "저장 결과를 확인하지 못했습니다. 입력을 유지한 채 같은 검토 저장을 재시도하세요." }));
    }
  };
  return <section aria-label="분석 검토 편집">
    <h4>사람의 검토</h4>
    <p>현재 사람의 상태: {latest ? labels[latest.state] : "아직 검토 기록 없음"}. 직접 확인 표시는 과학적 검증이나 저널 채택을 뜻하지 않습니다.</p>
    {stale ? <p role="status">편집 기준과 최신 이력이 다릅니다. 메모는 보존됩니다.</p> : null}
    <label>분석 검토 상태<select aria-label="분석 검토 상태" value={value.state} disabled={value.busy || !!value.attempt} onChange={event => update(old => ({ ...old, state: event.target.value as AnalysisState }))}>
      {Object.entries(labels).map(([state, label]) => <option key={state} value={state}>{label}</option>)}
    </select></label>
    <label>분석 검토 메모<textarea aria-label="분석 검토 메모" maxLength={8000} rows={5} value={value.note} disabled={value.busy || !!value.attempt} onChange={event => update(old => ({ ...old, note: event.target.value }))}/></label>
    <V2Button type="button" disabled={value.busy || value.conflict || (stale && !value.attempt)} onClick={() => void save()}>{value.attempt ? "같은 검토 저장 재시도" : "분석 검토 저장"}</V2Button>
    {(stale || value.conflict) && !value.busy ? <V2Button type="button" onClick={() => update(old => ({ ...old, base: latest?.decision_id ?? null, attempt: null, conflict: false, error: "", message: "입력을 유지하고 최신 이력을 기준으로 바꿨습니다. 저장은 별도로 실행하세요." }))}>최신 이력 기준으로 입력 이어가기</V2Button> : null}
    {value.error ? <p role="alert">{value.error}</p> : null}{value.message ? <p role="status">{value.message}</p> : null}
    <details><summary>사람의 검토 이력 ({record.decisions.length})</summary>{record.decisions.length ? <ol>{record.decisions.map(entry => <li key={entry.decision_id}><p>{labels[entry.state]} · {time(entry.created_at)} · {entry.actor_id}</p><p style={prose}>{entry.note || "메모 없음"}</p></li>)}</ol> : <p>저장된 검토가 없습니다.</p>}</details>
  </section>;
}
function Reader({ topicId, record, onSaved }: { topicId: string; record: SavedAnalysis; onSaved: (value: SavedAnalysis) => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const { preferences } = useOptionalAppearance();
  useEffect(() => { heading.current?.focus(); }, []);
  const receipt = record.receipt, packet = receipt.packet;
  const appCapture = record.bundle.schema_version === "SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1"
    && (receipt.input_origin === "LOCAL_APP_CAPTURE" || receipt.input_origin === "FIXTURE_SYNTHETIC_CAPTURE");
  return <section className="review-input-receipt" aria-label="저장된 분석 상세">
    <h4 tabIndex={-1} ref={heading}>보존된 분석과 근거</h4>
    <p>원래 주제: {receipt.topic_id} · 논문: {receipt.paper_id} · 실행 식별자: {receipt.run_id}</p>
    {appCapture ? <><p>{receipt.input_origin === "FIXTURE_SYNTHETIC_CAPTURE" ? "합성 실행의 앱 보존 사본입니다. 실제 Provider 실행이나 AI 연구 검토의 증거가 아닙니다." : "앱 서버가 보존한 공개 발췌 응답의 사본입니다. 수동 반입 파일과 구분하며, 실제 모델 신원이나 과학적 타당성의 독립적 증명은 아닙니다."}</p><p>요청 모델: {record.bundle.plan.contract.model} · 원래 실행: {record.bundle.plan.grant?.trial_id ?? "미확인"}</p><p>현재 주제에 사본을 보관했습니다. 원래 실행을 재시도하거나 논문 목록의 실행 이력으로 새로 등록하지 않습니다.</p></>
      : <p>현재 주제에 보관한 외부 응답입니다. 논문 목록의 실행 이력으로 새로 등록하지 않습니다. 파일 반입은 실제 호출의 독립적 증명이 아닙니다.</p>}
    <p>캡처 시각 ({appCapture ? "앱 보존 기록" : "반입 파일이 제공한 값"}): {formatTimestamp(record.bundle.captured_at, preferences.timeFormat, "ko-KR")} · 앱 저장 시각: {formatTimestamp(record.saved_at, preferences.timeFormat, "ko-KR")}</p>
    <PaperBrief result={receipt.draft} scope={packet.input_scope} extraction={packet.source_extraction} preparation={packet.source_preparation} evidenceSpans={packet.evidence_spans} inputOrigin={receipt.input_origin}/>
    <details><summary>상세 검토 범위·출처와 결과 원문</summary><PreservedReviewContext archive showBrief={false} receiptId={receipt.receipt_id} inputOrigin={receipt.input_origin} scope={packet.input_scope} extraction={packet.source_extraction} preparation={packet.source_preparation} result={receipt.draft} status={receipt.status} validation={receipt.validation}/></details>
    <Review topicId={topicId} record={record} onSaved={onSaved}/>
    <SavedAnalysisJournal topicId={topicId} record={record}/>
    <details><summary>원본 결합·해시와 {appCapture ? "앱 보존 묶음" : "반입 묶음"}</summary>{appCapture ? <p>실행 결합 SHA-256: {record.bundle.binding_sha256}</p> : null}<p>요청 SHA-256: {record.bundle.request_body_sha256}</p><p>응답 SHA-256: {record.bundle.response_sha256}</p><pre style={prose}>{JSON.stringify(record.bundle, null, 2)}</pre></details>
  </section>;
}
function History({ topicId }: { topicId: string }) {
  const [list, setList] = useState<AnalysisList | null>(null), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [record, setRecord] = useState<SavedAnalysis | null>(null), [selected, setSelected] = useState("");
  const [error, setError] = useState(""), [loading, setLoading] = useState(false), [importing, setImporting] = useState(false), [file, setFile] = useState<File | null>(null);
  const [captureSaving, setCaptureSaving] = useState(false);
  const captureLock = useRef(false);
  const generation = useRef(0), alive = useRef(true), fileInput = useRef<HTMLInputElement>(null);
  const activeId = useRef(selected); activeId.current = selected;
  const { preferences } = useOptionalAppearance();
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++; }; }, []);
  useEffect(() => {
    const epoch = ++generation.current; setLoading(true); setError("");
    void Promise.all([getAnalyses(topicId, page), selected ? getAnalysis(topicId, selected) : Promise.resolve(null)]).then(([rows, value]) => {
      if (alive.current && epoch === generation.current) { setList(rows); if (value) setRecord(value); setLoading(false); }
    }, () => { if (alive.current && epoch === generation.current) { setError("분석 이력을 불러오지 못했습니다. 새로고침으로 다시 확인하세요."); setLoading(false); } });
  }, [topicId, page, revision, selected]);
  const saved = (value: SavedAnalysis) => { if (alive.current) { generation.current++; if (activeId.current === value.analysis_id) setRecord(value); setRevision(old => old + 1); } };
  const captureBusy = (saving: boolean) => { captureLock.current = saving; if (alive.current) setCaptureSaving(saving); };
  const captured = (value: SavedAnalysis) => { if (alive.current && value.topic_id === topicId) { generation.current++; setRecord(value); setSelected(value.analysis_id); setPage(1); setRevision(old => old + 1); } };
  const importFile = async () => {
    if (!file || importing || captureLock.current) return;
    if (file.size > 2_000_000) { setError("반입 파일은 2 MB 이하여야 합니다."); return; }
    setImporting(true); setError("");
    try {
      const text = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error()); reader.readAsText(file); });
      const value = await importAnalysis(topicId, text);
      if (alive.current) { generation.current++; setRecord(value); setSelected(value.analysis_id); setFile(null); if (fileInput.current) fileInput.current.value = ""; setPage(1); setRevision(old => old + 1); }
    } catch { if (alive.current) setError("반입에 실패했거나 저장 결과를 확인하지 못했습니다. 올바른 구조화 응답 묶음인지 확인하고 같은 파일로 재시도하세요. 중복 저장되지 않습니다."); }
    finally { if (alive.current) setImporting(false); }
  };
  return <div className="review-input-history-body">
    <p>이전 구조화 응답을 근거와 함께 보관·검토합니다. 앱 보존 응답을 저장하거나 파일의 형식과 연결을 다시 검사하며 모델 호출은 실행하지 않습니다.</p>
    <CapturedAnalysisSave topicId={topicId} disabled={importing || loading} onSaving={captureBusy} onSaved={captured}/>
    <label>분석 응답 묶음 파일<input ref={fileInput} type="file" accept=".json,application/json" aria-label="분석 응답 묶음 파일" disabled={importing || captureSaving} onChange={event => { if (!captureLock.current) setFile(event.target.files?.[0] ?? null); }}/></label>
    <V2Button type="button" disabled={!file || importing || captureSaving} onClick={() => void importFile()}>구조화 응답 저장</V2Button>
    <V2Button type="button" disabled={loading || importing || captureSaving} onClick={() => { if (!captureLock.current) setRevision(old => old + 1); }}>분석 이력 새로고침</V2Button>
    {loading || importing ? <p role="status">분석 기록 확인 중</p> : null}{error ? <p role="alert">{error}</p> : null}
    {list ? <><ul className="review-input-records">{list.items.map(item => <li key={item.analysis_id}><p>{item.paper_id}</p><p>{item.model} · {formatTimestamp(item.saved_at, preferences.timeFormat, "ko-KR")}</p><p>{item.latest_decision ? labels[item.latest_decision.state] : "아직 검토 기록 없음"}</p><V2Button type="button" disabled={captureSaving || importing} aria-label={`분석 열기 · ${item.paper_id}`} onClick={() => { if (captureLock.current) return; setRecord(null); setSelected(item.analysis_id); setRevision(old => old + 1); }}>분석 열기</V2Button></li>)}</ul>{!list.total ? <p>저장된 분석이 없습니다.</p> : null}
      <nav aria-label="저장된 분석 페이지"><V2Button disabled={loading || captureSaving || importing || page <= 1} onClick={() => { if (!captureLock.current) setPage(old => old - 1); }}>이전 분석 페이지</V2Button><span>{page} 페이지</span><V2Button disabled={loading || captureSaving || importing || page * list.limit >= list.total} onClick={() => { if (!captureLock.current) setPage(old => old + 1); }}>다음 분석 페이지</V2Button></nav></> : null}
    {record ? <Reader key={record.analysis_id} topicId={topicId} record={record} onSaved={saved}/> : null}
  </div>;
}
function Panel({ topicId }: { topicId: string }) {
  const [open, setOpen] = useState(false);
  return <details className="review-input-history saved-analysis-panel" onToggle={event => { if (event.currentTarget.open) setOpen(true); }}><summary>저장된 분석 결과</summary>{open ? <History topicId={topicId}/> : null}</details>;
}
export function SavedAnalysisPanel({ topicId }: { topicId: string }) { return <SavedAnalysisDraftBoundary><Panel key={topicId} topicId={topicId}/></SavedAnalysisDraftBoundary>; }
