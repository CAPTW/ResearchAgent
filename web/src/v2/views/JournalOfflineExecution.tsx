import { useEffect, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import type { ExecutionDetail, OfflineAuthority, OfflineCapability } from "../../lib/journalExecutions";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { JournalAuthorityRecord, type AuthorityWithdrawal, type AuthorityWithdrawalResult } from "./JournalAuthorityRecord";

type Status = OfflineCapability & { authorities: OfflineAuthority[]; authorities_has_more?: boolean };
type Approval = { request_id: string; expected_sequence: number };
type Pending = { action: "approval"; body: Approval } | { action: "dispatch"; body: Approval & { authorization_id: string; acknowledge_unknown: boolean } };
type Props = { url: string; saved: ExecutionDetail; onUpdated: (execution: ExecutionDetail) => void; onBusy: (busy: boolean) => void; disabled?: boolean };

export function JournalOfflineExecution(props: Props) {
  return <Workspace key={props.url} {...props}/>;
}

function Workspace({ url, saved, onUpdated, onBusy, disabled = false }: Props) {
  const { preferences } = useOptionalAppearance();
  const [status, setStatus] = useState<Status | null>(null), [revision, refresh] = useState(0);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [readError, setReadError] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [pending, setPending] = useState<Pending | null>(null), [acknowledged, acknowledge] = useState(false);
  const alive = useRef(true), locked = useRef(false), busyCallback = useRef(onBusy);
  busyCallback.current = onBusy;
  useEffect(() => { alive.current = true; return () => { alive.current = false; busyCallback.current(false); }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setReadError("");
    void fetchJson<Status>(`${url}/offline`).then(value => {
      if (!cancelled) { setStatus(value); setLoading(false); }
    }, e => { if (!cancelled) { setReadError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [url, revision]);
  useEffect(() => { acknowledge(false); }, [saved.current.sequence]);
  const available = status?.authorities.find(item => item.status === "AVAILABLE");
  const canAttempt = ["READY", "FAILED", "INTERRUPTED"].includes(saved.current.state) && saved.current.attempt < saved.contract.specification.max_attempts;
  const blocked = disabled || loading || !!readError || busy || pending !== null;
  function working(value: boolean) { setBusy(value); busyCallback.current(value); }
  async function submit(action: Pending["action"]) {
    if (locked.current || disabled || loading || readError || !status?.enabled) return;
    if (!pending && (!canAttempt || (action === "dispatch" && (!available || (saved.current.outcome_unknown && !acknowledged))))) return;
    const request: Pending = pending ?? (action === "approval"
      ? { action, body: { request_id: crypto.randomUUID(), expected_sequence: saved.current.sequence } }
      : { action, body: { request_id: crypto.randomUUID(), expected_sequence: saved.current.sequence, authorization_id: available!.record.grant.authorization_id, acknowledge_unknown: acknowledged } });
    locked.current = true; working(true); setPending(request); setError(""); setNotice("");
    try {
      if (request.action === "approval") {
        const result = await fetchJson<{ authority: OfflineAuthority; created: boolean }>(`${url}/offline-approvals`, { method: "POST", body: JSON.stringify(request.body) });
        if (alive.current) {
          setStatus(previous => previous ? { ...previous, authorities: [result.authority, ...previous.authorities.filter(item => item.record_digest !== result.authority.record_digest)] } : previous);
          setNotice("승인만 기록했습니다. 아래 실행 버튼을 눌러야 합성 시도가 시작됩니다."); setPending(null);
        }
      } else {
        const result = await fetchJson<{ execution: ExecutionDetail }>(`${url}/offline-dispatch`, { method: "POST", body: JSON.stringify(request.body) });
        if (alive.current) { setPending(null); onUpdated(result.execution); setNotice("실행 요청의 저장 상태를 확인했습니다. 이력 조회는 새 실행을 시작하지 않습니다."); }
      }
    } catch (e) {
      if (alive.current) {
        const rejected = e instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(e.status);
        if (rejected) { setPending(null); refresh(value => value + 1); }
        setError(rejected ? `요청이 거절되었습니다: ${String(e)}. 실행 이력과 승인 목록을 확인하세요.`
          : `응답을 확인하지 못했습니다: ${String(e)}. 같은 요청을 다시 보내세요. 화면을 떠난 뒤에는 이력과 승인 목록부터 확인하세요.`);
      }
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  async function withdraw(body: AuthorityWithdrawal): Promise<AuthorityWithdrawalResult> {
    if (locked.current || blocked) throw new Error("진행 중인 요청과 현재 상태를 먼저 확인하세요.");
    locked.current = true; working(true);
    try {
      const result = await fetchJson<AuthorityWithdrawalResult & { execution: ExecutionDetail }>(`${url}/authority/revoke`, { method: "POST", body: JSON.stringify(body) });
      if (alive.current) {
        setStatus(previous => previous ? { ...previous, authorities: previous.authorities.map(item => item.record_digest === body.expected_record_digest ? { ...item, status: "REVOKED", revocation: result.revocation } : item) } : previous);
        onUpdated(result.execution);
      }
      return result;
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  return <section aria-label="오프라인 합성 실행" className="local-journal-editor">
    <h5>오프라인 합성 승인·실행</h5>
    <p>외부 전송 없이 고정 근거와 앱 연결을 시험합니다. 실제 AI 모델·논문 전체 검토·과학적 검증을 수행하지 않습니다.</p>
    <button type="button" disabled={busy || loading || disabled} onClick={() => refresh(value => value + 1)}>오프라인 승인 목록 새로고침</button>
    {loading ? <p role="status">오프라인 승인 확인 중</p> : null}
    {readError ? <p role="alert">승인 조회 오류: {readError}. 확인할 때까지 실행할 수 없습니다.</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {!loading && !readError && status ? <>
      {!status.enabled ? <p>이 환경에서는 오프라인 합성 실행을 사용할 수 없습니다. 저장된 승인·결과 이력만 확인할 수 있습니다.</p> : <>
        <p>승인은 이 사용자·주제·고정 요청·합성 도구에만 적용되며 5분 동안 유효합니다. 새 승인도 요청의 총 시도 한도를 늘리지 않습니다.</p>
        <button type="button" disabled={blocked || !canAttempt} onClick={() => void submit("approval")}>오프라인 합성 실행 승인 기록</button>
        {saved.current.outcome_unknown ? <label><input type="checkbox" disabled={blocked} checked={acknowledged} onChange={e => acknowledge(e.target.checked)}/>이전 시도의 결과가 불명확함을 확인하고 새 합성 시도를 요청합니다</label> : null}
        <button type="button" disabled={blocked || !canAttempt || !available || (saved.current.outcome_unknown && !acknowledged)} onClick={() => void submit("dispatch")}>승인한 오프라인 합성 실행</button>
        {pending ? <button type="button" disabled={busy || disabled} onClick={() => void submit(pending.action)}>{pending.action === "approval" ? "같은 오프라인 승인 요청 다시 보내기" : "같은 오프라인 실행 요청 다시 보내기"}</button> : null}
        {!canAttempt ? <p>현재 실행 상태 또는 시도 한도로 인해 새 시도를 시작할 수 없습니다. 저장된 실행 이력을 확인하세요.</p> : null}
      </>}
      {!status.authorities.length ? <p>저장된 오프라인 승인이 없습니다.</p> : <ul>{status.authorities.map(item => <li key={item.record_digest}>
        <p>{item.status === "AVAILABLE" ? "조회 시 사용 가능" : item.status === "EXPIRED" ? "기한 만료" : "철회됨"} · 시작 허용 기한: {formatTimestamp(item.record.grant.expires_at, preferences.timeFormat, "ko-KR")}</p>
        <p className="local-report-id">{item.record.grant.authorization_id}</p>
        <JournalAuthorityRecord url={`${url}/authority?authorization_id=${encodeURIComponent(item.record.grant.authorization_id)}&expected_record_digest=${item.record_digest}`}
          disabled={blocked} knownRevocation={item.revocation ?? undefined}
          withdrawal={{ authorization_id: item.record.grant.authorization_id, record_digest: item.record_digest, submit: withdraw }}/>
      </li>)}</ul>}
      {status.authorities_has_more ? <p>최근 승인 25개를 표시합니다. 실행에 사용된 이전 승인은 해당 실행 사건의 허용 기록에서 확인할 수 있습니다.</p> : null}
    </> : null}
  </section>;
}
