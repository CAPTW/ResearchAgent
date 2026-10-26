import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import type { DecisionHistory, DecisionInput, InsightDecision } from "../../lib/temporalAnalysis";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";

export type ReviewHistory = Omit<DecisionHistory, "items"> & {items: Array<Omit<InsightDecision, "insight_record_id"|"topic_id">>};

export function ReviewDecisionPanel({ loadHistory, saveDecision, eligible, explanation, acceptedLabel="후속 검토에 채택", ineligibleReason="정책 기준을 충족한 분석 기록만 후속 검토에 채택할 수 있습니다.", conflictHint="기록이 변경되었거나 채택할 수 없는 상태입니다. 이력을 새로고침하고 판단을 다시 확인하세요." }: {
 loadHistory: (page:number)=>Promise<ReviewHistory>;
 saveDecision: (body:DecisionInput)=>Promise<unknown>;
 eligible:boolean;
 explanation:string;
 acceptedLabel?:string;
 ineligibleReason?:string;
 conflictHint?:string;
}) {
 const labels = { accepted: acceptedLabel, held: "보류", rejected: "검토 대상에서 제외" };
 const id = useId();
 const { preferences } = useOptionalAppearance();
 const [open, setOpen] = useState(false);
 const [page, setPage] = useState(1);
 const [revision, setRevision] = useState(0);
 const [history, setHistory] = useState<ReviewHistory | null>(null);
 const [loading, setLoading] = useState(false);
 const [loadError, setLoadError] = useState("");
 const [error, setError] = useState("");
 const [message, setMessage] = useState("");
 const [state, setState] = useState<DecisionInput["state"]>("held");
 const [note, setNote] = useState("");
 const [busy, setBusy] = useState(false);
 const [conflict, setConflict] = useState(false);
 const attempt = useRef<DecisionInput | null>(null);
 const locked = useRef(false);
 const alive = useRef(true);
 useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
 useEffect(() => {
  if (!open) return;
  let cancelled = false;
  setLoading(true); setLoadError(""); setHistory(null);
  void loadHistory(page).then(data => {
   if (!cancelled) { setHistory(data); setLoading(false); setConflict(false); }
  }, e => { if (!cancelled) { setLoadError(e instanceof Error ? e.message : "이력 요청 실패"); setLoading(false); } });
  return () => { cancelled = true; };
 }, [open, loadHistory, page, revision]);
 const refresh = () => { attempt.current = null; setError(""); setPage(1); setRevision(v => v + 1); };
 const save = async () => {
  if (locked.current || loading || !history || conflict || (state === "accepted" && !eligible)) return;
  locked.current = true; setBusy(true); setError(""); setMessage("");
  // Keep the same request key after a lost response; retry must not duplicate history.
  const body = attempt.current ?? { request_id: crypto.randomUUID(), state, note, expected_previous_id: history.latest_id };
  attempt.current = body;
  try {
   await saveDecision(body);
   if (alive.current) { attempt.current = null; setMessage("판단을 저장했습니다."); setPage(1); setRevision(v => v + 1); }
  } catch (e) {
   if (alive.current) {
    const stale = e instanceof ApiError && e.status === 409;
    setConflict(stale);
    setError(stale ? conflictHint : `저장 실패: ${e instanceof Error ? e.message : "요청 실패"}. 같은 내용으로 다시 저장할 수 있습니다.`);
   }
  } finally { if (alive.current) { locked.current = false; setBusy(false); } }
 };
 return <details className="insight-decision" onToggle={e => setOpen(e.currentTarget.open)}>
  <summary>검토 판단과 이력</summary>
  {open ? <div>
   <p>{explanation}</p>
   <button type="button" onClick={refresh} disabled={busy || loading}>판단 이력 새로고침</button>
   {loading ? <p role="status">판단 이력 불러오는 중</p> : null}
   {loadError ? <p role="alert">판단 이력 오류: {loadError}</p> : null}
   {history ? <>
    <p>{history.total ? `${history.total}개 판단 기록` : "아직 사용자 판단이 없습니다."}</p>
    <ol aria-label="판단 이력">{history.items.map(d => <li key={d.decision_id}>
     <strong>{labels[d.state]}</strong> · <time dateTime={d.created_at}>{formatTimestamp(d.created_at, preferences.timeFormat, "ko-KR")}</time>
     <p className="decision-note">{d.note || "사유를 입력하지 않았습니다."}</p>
     <small>판단자: {d.actor_id} · {d.decision_id}</small>
    </li>)}</ol>
    <div><button type="button" disabled={busy || page <= 1} onClick={() => setPage(p => p - 1)}>이전 판단</button>
     <span> {page} / {Math.max(1, Math.ceil(history.total / history.limit))} </span>
     <button type="button" disabled={busy || page * history.limit >= history.total} onClick={() => setPage(p => p + 1)}>다음 판단</button></div>
   </> : null}
   <label htmlFor={`${id}-state`}>검토 판단</label>
   <select id={`${id}-state`} value={state} disabled={busy} onChange={e => { setState(e.target.value as DecisionInput["state"]); attempt.current = null; }}>
    {Object.entries(labels).map(([value, label]) => <option key={value} value={value} disabled={value === "accepted" && !eligible}>{label}</option>)}
   </select>
   {!eligible ? <p>{ineligibleReason}</p> : null}
   <label htmlFor={`${id}-note`}>판단 사유</label>
   <textarea id={`${id}-note`} value={note} maxLength={2000} disabled={busy} onChange={e => { setNote(e.target.value); attempt.current = null; }} />
   <button type="button" disabled={busy || loading || !history || conflict || (state === "accepted" && !eligible)} onClick={() => void save()}>판단 저장</button>
   {error ? <p role="alert">{error}</p> : null}
   {message ? <p role="status">{message}</p> : null}
  </div> : null}
 </details>;
}
