import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import { executionStates as states, executionReasons as reasons, isOfflineJournal, type ExecutionControl, type ExecutionDetail } from "../../lib/journalExecutions";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { JournalAuthorityRecord, type AuthorityWithdrawal, type AuthorityWithdrawalResult } from "./JournalAuthorityRecord";
import { JournalExecutionEvidence } from "./JournalExecutionEvidence";
import { JournalOfflineExecution } from "./JournalOfflineExecution";
import { JournalAdapterManifest } from "./JournalAdapterManifest";
import { JournalCodexPreview } from "./JournalCodexPreview";
import { JournalCodexDispatch } from "./JournalCodexDispatch";
import { JournalApproval } from "./JournalApproval";

type Props = { url: string; onChanged: () => void };
export function JournalExecutionHistory(props: Props) { return <History key={props.url} {...props}/>; }
function History({ url, onChanged }: Props) {
  const { preferences } = useOptionalAppearance();
  const [saved, setSaved] = useState<ExecutionDetail | null>(null), [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [offlineBusy, setOfflineBusy] = useState(false);
  const [codexBusy, setCodexBusy] = useState(false);
  const [codexPending, setCodexPending] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(false), [approvalPending, setApprovalPending] = useState(false);
  const [readError, setReadError] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [pending, setPending] = useState<ExecutionControl | null>(null);
  const [withdrawn, setWithdrawn] = useState<Record<string, AuthorityWithdrawalResult["revocation"]>>({});
  const alive = useRef(true), locked = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); heading.current?.scrollIntoView({ block: "start" }); }, [url]);
  useEffect(() => {
    let cancelled = false; setLoading(true); setReadError("");
    void fetchJson<ExecutionDetail>(url).then(value => {
      if (!cancelled) { setSaved(value); setLoading(false); setPending(null); setError(""); setNotice(""); }
    }, e => { if (!cancelled) { setReadError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [url, revision]);
  const current = saved?.current;
  const active = current?.state === "LEASED" || current?.state === "RUNNING";
  const synthetic = saved ? isOfflineJournal(saved.contract.specification.provider_name, saved.contract.specification.model) : false;
  const codex = saved?.contract.specification.provider_name === "codex_cli";
  const changed = useRef(onChanged); changed.current = onChanged;
  // Uncertain approval/dispatch responses block new execution, not observation
  // or safety actions. Cancellation/recovery/revocation keep server guards.
  useEffect(() => {
    if ((!synthetic && !codex) || !active || loading || busy || offlineBusy || codexBusy || approvalBusy || pending || readError) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void fetchJson<ExecutionDetail>(url).then(value => {
        if (!cancelled) {
          setSaved(value);
          if (value.current.sequence !== current?.sequence) changed.current();
        }
      }, e => { if (!cancelled) setReadError(String(e)); });
    }, 1000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [synthetic, codex, active, loading, busy, offlineBusy, codexBusy, approvalBusy, pending, readError, url, saved, current?.sequence]);
  async function withdrawAuthority(body: AuthorityWithdrawal): Promise<AuthorityWithdrawalResult> {
    if (locked.current || offlineBusy || codexBusy || approvalBusy || loading || readError) throw new Error("실행 상태를 확인한 뒤 다시 요청하세요.");
    locked.current = true; setBusy(true);
    try {
      const result = await fetchJson<AuthorityWithdrawalResult & { execution: ExecutionDetail }>(`${url}/authority/revoke`, { method: "POST", body: JSON.stringify(body) });
      if (alive.current) {
        setSaved(result.execution); setPending(null); setError(""); setNotice("");
        setWithdrawn(previous => ({ ...previous, [body.expected_record_digest]: result.revocation }));
        onChanged();
      }
      return result;
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  async function submit(action: ExecutionControl["action"]) {
    if (locked.current || offlineBusy || codexBusy || approvalBusy || !saved || loading || readError) return;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    const body = pending ?? { request_id: crypto.randomUUID(), action, expected_sequence: saved.current.sequence };
    setPending(body);
    try {
      const result = await fetchJson<{ execution: ExecutionDetail; applied: boolean }>(`${url}/control`, { method: "POST", body: JSON.stringify(body) });
      if (alive.current) {
        setSaved(result.execution); setPending(null);
        setNotice(result.applied ? "요청한 상태 변경을 기록했습니다." : "이미 처리한 요청입니다. 현재 저장 상태를 표시합니다.");
        onChanged();
      }
    } catch (e) {
      if (alive.current) {
        const message = String(e);
        setError(message.includes("journal_execution_not_expired") ? "서버 기준으로 결과 접수 기한이 아직 남아 있습니다. 기한 이후 이력을 새로고침하고 다시 확인하세요."
          : message.includes("journal_execution_conflict") || message.includes("journal_execution_request_conflict") ? "저장 상태가 바뀌어 요청을 적용하지 않았습니다. 이력을 새로고침하고 현재 상태를 확인하세요."
          : `상태 변경 응답을 확인하지 못했습니다: ${message}. 같은 요청을 다시 보내거나 이력을 새로고침하세요.`);
      }
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  const stamp = (value: string | null) => value ? formatTimestamp(value, preferences.timeFormat, "ko-KR") : "기록 없음";
  const controllable = current && current.state !== "SUCCEEDED" && current.state !== "CANCELLED";
  return <section aria-label="저널 실행 이력과 제어" className="local-journal">
    <h4 ref={heading} tabIndex={-1} style={{ scrollMarginBlockStart: 80 }}>실행 이력과 상태 관리</h4>
    <button type="button" disabled={loading || busy || offlineBusy || codexBusy || approvalBusy} onClick={() => setRevision(v => v + 1)}>실행 이력 새로고침</button>
    {loading ? <p role="status">실행 이력 확인 중</p> : readError ? <p role="alert">실행 이력 조회 오류: {readError}. 현재 상태를 확인하지 못해 조작할 수 없습니다.</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {saved && codex ? <JournalApproval url={url} saved={saved} disabled={loading || !!readError || busy || offlineBusy || codexBusy || codexPending || pending !== null}
      onBusy={setApprovalBusy} onPending={setApprovalPending} onUpdated={value => { setSaved(value); onChanged(); }}/> : null}
    {saved && codex ? <JournalCodexDispatch url={url} saved={saved} disabled={loading || !!readError || busy || offlineBusy || approvalBusy || approvalPending || pending !== null}
      onBusy={setCodexBusy} onPending={setCodexPending} onUpdated={value => { setSaved(value); onChanged(); }}/> : null}
    {!loading && !readError && saved && current ? <>
      <p>최근 저장 상태: {states[current.state]} · 시도 {current.attempt} / 최대 {saved.contract.specification.max_attempts}회</p>
      {current.state === "READY" ? <p>아직 호출을 시작하지 않은 입력 준비 기록입니다. 도구 사용 가능 여부·실행 허용·금액 예산이 확인되었다는 뜻은 아닙니다.</p> : null}
      {current.outcome_unknown ? <p>외부 호출의 완료 여부는 확인되지 않았습니다. 자동으로 재시도하지 않습니다.</p> : null}
      {["provider_incomplete", "provider_failed", "provider_cancelled"].includes(current.reason || "") ? <p>도구가 성공하지 못한 종료 상태를 보고했습니다. 비용이 발생하지 않았다는 뜻은 아니며 자동으로 다시 실행하지 않습니다.</p> : null}
      {active ? <p>결과 접수 기한: {stamp(current.deadline)}. 기한 확인은 서버 시간으로 만료 여부를 판단해 호출 전 실패 또는 호출 후 응답 확인 불가를 기록합니다. 새 호출은 시작하지 않습니다.</p> : null}
      {controllable ? <p>취소하면 이 요청의 새 시도와 결과 저장이 닫힙니다. 이미 시작된 외부 호출의 중단·비용 취소를 보장하지 않습니다.</p> : null}
      {active ? <button type="button" disabled={busy || offlineBusy || codexBusy || approvalBusy || pending !== null} onClick={() => void submit("recover")}>접수 기한 만료 확인</button> : null}
      {controllable ? <button type="button" disabled={busy || offlineBusy || codexBusy || approvalBusy || pending !== null} onClick={() => void submit("cancel")}>이 요청의 결과 저장 취소</button> : null}
      {pending ? <button type="button" disabled={busy || offlineBusy || codexBusy || approvalBusy} onClick={() => void submit(pending.action)}>같은 상태 변경 요청 다시 보내기</button> : null}
      <p>이력 조회·기한 확인·결과 저장 취소는 생성 실행이나 재시도를 하지 않습니다.</p>
      {synthetic ? <JournalOfflineExecution url={url} saved={saved} disabled={busy || pending !== null}
        onBusy={setOfflineBusy} onUpdated={value => { setSaved(value); onChanged(); }}/> : null}
      <JournalExecutionEvidence key={saved.contract.execution_id} saved={saved}/>
      {!busy && !offlineBusy && !codexBusy && !codexPending && !approvalBusy && !approvalPending && pending === null ? <JournalCodexPreview url={url} saved={saved}/> : null}
      <ol aria-label="실행 사건 이력">
        <li><strong>입력 준비</strong> · {stamp(saved.contract.created_at)}<p>요청 기록 사용자: {saved.contract.actor_id}</p></li>
        {saved.events.map(event => <li key={event.sequence}>
          <strong>{states[event.state]}</strong> · 시도 {event.attempt} · {stamp(event.at)}
          {event.reason ? <p>{reasons[event.reason] || "상세 사유를 확인할 수 없습니다."}</p> : null}
          {event.control ? <p>{event.control.action === "cancel" ? "결과 저장 취소" : "기한 만료 확인"} 요청 사용자: {event.control.actor_id}</p>
            : event.dispatch ? <p>실행 요청 사용자: {event.dispatch.actor_id}</p>
            : event.authority_revocation ? <p>실행 허용 철회 사용자: {event.authority_revocation.actor_id}</p> : <p>실행 처리 기록 · 사용자 조작 정보 없음</p>}
          {event.authority_revocation ? <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>철회 사유: {event.authority_revocation.reason}</p> : null}
          {event.dispatch ? <details><summary>실행 허용·예약 기록</summary>
            <p>이 시도의 출력 토큰 예약 상한: {event.dispatch.reserved_output_tokens}</p>
            <p>이 요청에 허용된 최대 시도: {event.dispatch.grant.max_attempts}회 · 누적 출력 토큰 예약 한도: {event.dispatch.grant.max_reserved_output_tokens}</p>
            <p>호출 시작 허용 기한: {stamp(event.dispatch.grant.expires_at)}</p>
            <p>호출 전 실패에도 예약량은 반환되지 않습니다. 예약 상한은 실제 토큰 사용량·요금·모델 실행 증명이 아닙니다.</p>
            <p className="local-report-id">허용 기록: {event.dispatch.grant.authorization_id}</p>
            {event.dispatch.authority ? <JournalAuthorityRecord key={`${url}:${event.dispatch.authority.record_digest}`}
              disabled={busy || offlineBusy || codexBusy || approvalBusy || pending !== null} withdrawal={{ ...event.dispatch.authority, submit: withdrawAuthority }}
              knownRevocation={withdrawn[event.dispatch.authority.record_digest]}
              url={`${url}/authority?authorization_id=${encodeURIComponent(event.dispatch.authority.authorization_id)}&expected_record_digest=${event.dispatch.authority.record_digest}`}/> : null}
          </details> : null}
          {event.outcome_unknown ? <p>이 시점의 외부 완료 여부: 확인되지 않음</p> : null}
          {event.adapter_manifest ? <JournalAdapterManifest value={event.adapter_manifest}/> : null}
        </li>)}
      </ol>
      <details><summary>고정된 실행 요청 정보</summary>
        <p>요청 도구·모델: {saved.contract.specification.provider_name} · {saved.contract.specification.model}</p>
        <p>요청 출력 토큰 상한: {saved.contract.specification.max_output_tokens}</p>
        <p>실제 도구 호출·모델 사용을 증명하는 정보는 아닙니다.</p>
        <p className="local-report-id">실행: {saved.contract.execution_id}</p>
        <p className="local-report-id">입력: {saved.contract.specification.expected_input_digest}</p>
        <p className="local-report-id">근거 패킷: {saved.contract.packet.packet_id}</p>
      </details>
    </> : null}
  </section>;
}
