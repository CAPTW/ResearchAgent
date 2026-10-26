import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { SavedCandidate } from "./JournalExchangePanel";
import { JournalExecutionHistory } from "./JournalExecutionHistory";
import { JournalExecutionPreparation } from "./JournalExecutionPreparation";
import { executionStates as states, executionReasons as reasons, isOfflineJournal, reportedJournalModel, type ExecutionState, type OfflineCapability } from "../../lib/journalExecutions";

type Item = {
  execution_id: string; created_at: string; requested_provider: string; requested_model: string;
  state: ExecutionState;
  attempt: number; reason: string | null; outcome_unknown: boolean; deadline: string | null;
  result_digest: string | null; title: string | null;
  provider_reported: null | { provider_name: string; model: string };
};
type Listing = { items: Item[]; total: number; page: number; limit: number };

function Workspace({ root, inputDigest }: { root: string; inputDigest?: string }) {
  const { preferences } = useOptionalAppearance();
  const [listing, setListing] = useState<Listing | null>(null), [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [listError, setListError] = useState(""), [error, setError] = useState(""), [selected, setSelected] = useState("");
  const [historyId, setHistoryId] = useState("");
  const [offline, setOffline] = useState<OfflineCapability | undefined>(), [capabilityError, setCapabilityError] = useState("");
  const alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setListError("");
    void fetchJson<Listing>(`${root}/journal-executions?page=${page}&limit=10`).then(data => {
      if (!cancelled) { setListing(data); setLoading(false); }
    }, e => { if (!cancelled) { setListError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [root, page, revision]);
  useEffect(() => {
    if (!inputDigest) return;
    let cancelled = false; setOffline(undefined); setCapabilityError("");
    void fetchJson<OfflineCapability>(`${root}/journal-offline`).then(value => {
      if (!cancelled) setOffline(value);
    }, e => { if (!cancelled) setCapabilityError(String(e)); });
    return () => { cancelled = true; };
  }, [root, inputDigest, revision]);
  async function preserve(item: Item) {
    if (locked.current || !item.result_digest) return;
    locked.current = true; setBusy(true); setError("");
    try {
      const result = await fetchJson<{ candidate: { candidate_id: string } }>(`${root}/journal-executions/${encodeURIComponent(item.execution_id)}/candidate`, {
        method: "POST", body: JSON.stringify({ expected_result_digest: item.result_digest }),
      });
      if (alive.current) setSelected(result.candidate.candidate_id);
    } catch (e) { if (alive.current) setError(`후보 보존 오류: ${String(e)}. 같은 결과로 다시 시도할 수 있습니다.`); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  const ready = !loading && !listError;
  return <section aria-label="저장된 저널 생성 결과">
    <p>이미 저장된 응답을 검토 후보로 보존할 수 있습니다. 이 동작은 새 생성·외부 호출·보고서 채택을 하지 않습니다.</p>
    <p>실제 AI 생성 실행에는 별도의 실행 허용·계정 연결이 필요합니다. 저장된 응답의 모델 이름은 어댑터가 보고한 값이며 실제 외부 실행의 독립적인 증명이 아닙니다.</p>
    {capabilityError ? <p role="alert">오프라인 실행 가능 여부 조회 오류: {capabilityError}. 목록 새로고침으로 다시 확인하세요.</p> : null}
    {inputDigest ? <JournalExecutionPreparation key={inputDigest} root={root} inputDigest={inputDigest} offline={offline} onPrepared={id => { setPage(1); setRevision(v => v + 1); setHistoryId(id); }}/>: null}
    <button type="button" disabled={loading || busy} onClick={() => setRevision(v => v + 1)}>저장된 생성 결과 새로고침</button>
    {loading ? <p role="status">저장된 실행 기록 확인 중</p> : listError ? <p role="alert">실행 기록 조회 오류: {listError}. 현재 목록을 확인하지 못했습니다.</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {ready && listing ? <>
      {!listing.total ? <p>저장된 실행 기록이 없습니다. 결과 파일은 별도의 파일 교환에서 가져올 수 있습니다.</p> : <p>저장된 실행 기록 {listing.total}개 · 표시된 상태는 마지막으로 저장된 기록입니다.</p>}
      <ul>{listing.items.map(item => <li key={item.execution_id}>
        <h4>{item.title || `${item.requested_provider} · ${item.requested_model}`}</h4>
        {isOfflineJournal(item.requested_provider, item.requested_model) ? <p>오프라인 합성 실행 기록 · 실제 AI 본문 검토가 아닙니다.</p> : null}
        <p>{states[item.state]} · 시도 {item.attempt} · {formatTimestamp(item.created_at, preferences.timeFormat, "ko-KR")}</p>
        {item.provider_reported ? <p>어댑터 보고 모델: {item.provider_reported.provider_name} · {reportedJournalModel(item.provider_reported.model)}</p> : null}
        {item.reason ? <p>{reasons[item.reason] || "상세 사유를 확인할 수 없습니다."}</p> : null}
        {item.outcome_unknown ? <p>외부 호출의 완료 여부는 확인되지 않았습니다. 자동으로 재시도하지 않습니다.</p> : null}
        {item.state === "RUNNING" || item.state === "LEASED" ? <p>현재도 실행 중이라는 보장이 아닙니다. 결과 접수 기한: {item.deadline ? formatTimestamp(item.deadline, preferences.timeFormat, "ko-KR") : "기록 없음"}</p> : null}
        <small className="local-report-id">{item.execution_id}</small>
        <button type="button" onClick={() => setHistoryId(item.execution_id)}>실행 이력·제어 열기</button>
        {item.state === "SUCCEEDED" && item.result_digest ? <button type="button" disabled={busy} onClick={() => void preserve(item)}>검토 후보로 보존·열기</button> : <p>성공한 응답이 저장되지 않아 후보로 보존할 수 없습니다.</p>}
      </li>)}</ul>
      <button type="button" disabled={busy || page === 1} onClick={() => setPage(v => v - 1)}>이전 실행 기록 페이지</button><span> {page} / {Math.max(1, Math.ceil(listing.total / listing.limit))} </span><button type="button" disabled={busy || page * listing.limit >= listing.total} onClick={() => setPage(v => v + 1)}>다음 실행 기록 페이지</button>
    </> : null}
    {historyId ? <JournalExecutionHistory key={historyId} url={`${root}/journal-executions/${encodeURIComponent(historyId)}`} onChanged={() => setRevision(v => v + 1)}/> : null}
    {selected ? <SavedCandidate key={selected} root={`${root}/journal-candidates`} candidateId={selected} showReview/> : null}
  </section>;
}

export function JournalExecutionResults({ topicId, jobId, inputDigest }: { topicId: string; jobId: string; inputDigest?: string }) {
  const [opened, setOpened] = useState(false);
  const root = `/api/topics/${encodeURIComponent(topicId)}/local-synthesis/${encodeURIComponent(jobId)}`;
  return <details className="local-journal journal-exchange" onToggle={e => { if (e.currentTarget.open) setOpened(true); }}><summary>저장된 생성 결과</summary>{opened ? <Workspace key={root} root={root} inputDigest={inputDigest}/> : null}</details>;
}
