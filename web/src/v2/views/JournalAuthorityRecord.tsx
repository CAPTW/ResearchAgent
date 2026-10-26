import { useEffect, useId, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";

type Revocation = { actor_id: string; at: string; reason: string };
export type AuthorityWithdrawal = { authorization_id: string; expected_record_digest: string; request_id: string; reason: string };
export type AuthorityWithdrawalResult = { revocation: Revocation; applied: boolean };
type Source = {
  record: { registered_by: string; registered_at: string; decision_ref: string };
  revocation: Revocation | null;
};

export function JournalAuthorityRecord({ url, withdrawal, disabled = false, knownRevocation }: {
  url: string; disabled?: boolean; knownRevocation?: Revocation;
  withdrawal?: { authorization_id: string; record_digest: string; submit: (body: AuthorityWithdrawal) => Promise<AuthorityWithdrawalResult> };
}) {
  const { preferences } = useOptionalAppearance();
  const [revision, refresh] = useState(0), [saved, setSaved] = useState<Source | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [reason, setReason] = useState(""), [pending, setPending] = useState<AuthorityWithdrawal | null>(null);
  const [busy, setBusy] = useState(false), [writeError, setWriteError] = useState(""), [notice, setNotice] = useState("");
  const reasonId = useId(), alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { if (knownRevocation) { setPending(null); setWriteError(""); } }, [knownRevocation]);
  useEffect(() => {
    if (!revision) return;
    let cancelled = false;
    setLoading(true); setSaved(null); setError("");
    void fetchJson<Source>(url).then(value => {
      if (!cancelled) {
        setSaved(value); setLoading(false);
        if (value.revocation) { setPending(null); setWriteError(""); }
      }
    }, e => { if (!cancelled) { setError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [url, revision]);
  async function withdraw() {
    if (locked.current || disabled || loading || !saved || saved.revocation || knownRevocation || !withdrawal || !reason.trim()) return;
    locked.current = true; setBusy(true); setWriteError(""); setNotice("");
    const body = pending ?? { authorization_id: withdrawal.authorization_id, expected_record_digest: withdrawal.record_digest,
      request_id: crypto.randomUUID(), reason };
    setPending(body);
    try {
      const result = await withdrawal.submit(body);
      if (alive.current) {
        setSaved({ ...saved, revocation: result.revocation }); setPending(null);
        setNotice(result.applied ? "실행 허용 철회를 기록했습니다." : "이미 처리한 철회 요청입니다.");
      }
    } catch (e) {
      if (alive.current) setWriteError(`철회 응답을 확인하지 못했습니다: ${String(e)}. 같은 요청을 다시 보내거나 허용 출처를 새로고침하세요.`);
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  const stamp = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");
  const revoked = saved?.revocation ?? knownRevocation;
  return <section aria-label="저장된 실행 허용 출처">
    <button type="button" disabled={loading || busy || disabled} onClick={() => { setNotice(""); refresh(value => value + 1); }}>{revision ? "허용 출처 새로고침" : "허용 출처 확인"}</button>
    {loading ? <p role="status">허용 출처 확인 중</p> : null}
    {error ? <p role="alert">허용 출처 조회 오류: {error}. 현재 철회 여부를 확인하지 못했습니다.</p> : null}
    {writeError ? <p role="alert">{writeError}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {saved ? <>
      <p>기록 등록자: {saved.record.registered_by} · {stamp(saved.record.registered_at)}</p>
      <p className="local-report-id">승인 출처 식별자: {saved.record.decision_ref}</p>
      {revoked ? <>
        <p>철회 기록: {revoked.actor_id} · {stamp(revoked.at)}</p>
        <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>철회 사유: {revoked.reason}</p>
      </> : <p>조회한 기록에 철회는 없습니다. 현재 호출 가능 여부를 보장하지 않습니다.</p>}
      <p>저장된 출처 기록이며 별도의 승인 진위·실제 모델 실행 증명은 아닙니다. 조회는 실행을 시작하거나 허용하지 않습니다.</p>
      {!revoked && withdrawal ? <form onSubmit={e => { e.preventDefault(); void withdraw(); }}>
        <p>철회하면 이 허용 기록을 다시 사용할 수 없습니다. 진행 중인 해당 시도의 결과 저장을 막으며, 이미 시작된 외부 호출의 중단이나 비용 반환은 보장하지 않습니다. 저장된 성공 결과와 다른 허용 기록은 유지됩니다.</p>
        <label htmlFor={reasonId}>실행 허용 철회 사유</label>
        <textarea id={reasonId} value={reason} maxLength={2000} required disabled={busy || disabled || pending !== null}
          style={{ width: "100%", boxSizing: "border-box" }} onChange={e => setReason(e.target.value)}/>
        <button type="submit" disabled={busy || disabled || !reason.trim()}>{pending ? "같은 철회 요청 다시 보내기" : "이 실행 허용 철회"}</button>
        {busy ? <p role="status">철회 결과 확인 중</p> : null}
      </form> : null}
    </> : null}
  </section>;
}
