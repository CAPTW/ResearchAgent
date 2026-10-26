import { useEffect, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import type { ExecutionDetail } from "../../lib/journalExecutions";
import { readJournalApprovalPreview, readJournalApprovalResult, sameJournalValue,
  type JournalApprovalBody, type JournalApprovalOffer, type JournalApprovalPreview } from "../../lib/journalApproval";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { JournalAdapterManifest } from "./JournalAdapterManifest";

type Props = { url: string; saved: ExecutionDetail; disabled?: boolean; onUpdated: (value: ExecutionDetail) => void;
  onBusy: (value: boolean) => void; onPending: (value: boolean) => void };
type Pending = { body: JournalApprovalBody; offer: JournalApprovalOffer; frozen: ExecutionDetail };
function unavailable(reason: string | null): string {
  if (reason === "journal_approval_disabled") return "이 서버에서는 Journal 승인 등록이 비활성화되어 있습니다. 운영자가 승인 출처와 등록 사용자 범위를 별도로 연결해야 합니다.";
  if (reason === "journal_approval_source_unavailable") return "서버 승인 출처에서 현재 등록할 기록을 제공하지 않았습니다. 승인 출처를 확인한 뒤 다시 조회하세요.";
  if (reason === "journal_approval_not_ready") return "현재 실행 상태에서는 새로운 승인을 등록할 수 없습니다. 저장된 이력을 확인하세요.";
  if (reason === "journal_dispatch_authority_expired") return "승인 또는 증명의 유효 기간이 지났습니다. 승인 출처에서 새 기록을 준비해야 합니다.";
  if (reason === "journal_dispatch_budget_exhausted") return "승인된 시도 수 또는 출력 토큰 예약 한도가 남아 있지 않습니다.";
  return "현재 승인 후보를 등록할 수 없습니다. 서버 승인 출처와 실행 조건을 확인하세요.";
}
export function JournalApproval(props: Props) {
  const identity = [props.url, props.saved.contract.execution_id, props.saved.contract.actor_id,
    props.saved.contract.packet.packet_id, props.saved.contract.specification.expected_input_digest];
  return props.saved.contract.specification.provider_name === "codex_cli" ? <Workspace key={JSON.stringify(identity)} {...props}/> : null;
}
function Workspace({ url, saved, disabled = false, onUpdated, onBusy, onPending }: Props) {
  const { preferences } = useOptionalAppearance();
  const [shown, setShown] = useState<JournalApprovalPreview | null>(null), [pending, setPending] = useState<Pending | null>(null);
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const alive = useRef(true), locked = useRef(false), current = useRef(saved), callbacks = useRef({ onBusy, onPending, onUpdated });
  current.current = saved; callbacks.current = { onBusy, onPending, onUpdated };
  useEffect(() => { alive.current = true; return () => { alive.current = false; callbacks.current.onBusy(false); callbacks.current.onPending(false); }; }, []);
  const currentOffer = shown?.observed_sequence === saved.current.sequence ? shown : null;
  const canRegister = !disabled && !loading && !busy && !pending && currentOffer?.approval_allowed && currentOffer.offer;
  async function inspect() {
    if (locked.current || disabled) return;
    locked.current = true; setLoading(true); setShown(null); setError(""); setNotice("");
    try {
      const response = await fetchJson<unknown>(`${url}/codex-approval`);
      if (!alive.current) return;
      const value = readJournalApprovalPreview(response, current.current);
      if (!value) throw new Error("invalid approval preview");
      setShown(value);
    } catch { if (alive.current) setError("승인 후보를 확인하지 못했습니다. 로그인·조회 권한과 서버 승인 출처를 확인한 뒤 다시 조회하세요."); }
    finally { locked.current = false; if (alive.current) setLoading(false); }
  }
  async function approve() {
    if (locked.current || disabled || (!pending && !canRegister)) return;
    const request = pending ?? { frozen: saved, offer: currentOffer!.offer!, body: {
      authorization_id: currentOffer!.offer!.authorization_id, expected_sequence: saved.current.sequence,
      expected_contract_digest: currentOffer!.contract_digest, expected_record_digest: currentOffer!.offer!.record_digest,
      expected_record_sha256: currentOffer!.offer!.record_sha256 } };
    locked.current = true; setBusy(true); setPending(request); callbacks.current.onBusy(true); callbacks.current.onPending(true); setError(""); setNotice("");
    try {
      const response = await fetchJson<unknown>(`${url}/codex-approval`, { method: "POST", body: JSON.stringify(request.body) });
      if (!alive.current) return;
      const result = await readJournalApprovalResult(response, current.current, request.body, request.offer);
      if (!alive.current) return;
      if (!sameJournalValue(current.current.contract, request.frozen.contract) || !result
        || result.execution.current.sequence < current.current.current.sequence) throw new Error("invalid approval response");
      setPending(null); callbacks.current.onPending(false); setShown(null); callbacks.current.onUpdated(result.execution);
      setNotice(result.revoked ? "등록된 승인 기록을 확인했습니다. 이미 철회된 기록이며 새 실행을 허용하지 않습니다."
        : "승인 등록을 확인했습니다. 등록만으로 실행을 시작하지 않으며 결과를 채택하지 않습니다. 실행하려면 별도 실행 허용을 조회하고 직접 요청하세요.");
    } catch (failure) {
      if (alive.current) {
        const rejected = failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && ![408, 429].includes(failure.status);
        if (rejected) { setPending(null); callbacks.current.onPending(false); setShown(null); }
        setError(rejected ? "승인 등록 요청이 거절되었습니다. 이력을 새로고침하고 승인 후보를 다시 확인하세요."
          : "승인 등록 응답을 확인하지 못했습니다. 같은 요청을 다시 보내 저장된 등록 결과를 확인하세요. 새 승인이나 실행을 만들지 않습니다.");
      }
    } finally { locked.current = false; if (alive.current) { setBusy(false); callbacks.current.onBusy(false); } }
  }
  const stamp = (value: string | null) => value ? formatTimestamp(value, preferences.timeFormat, "ko-KR") : "미기록";
  const offer = currentOffer?.offer;
  return <section aria-label="Journal 승인 등록" className="local-journal-editor" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <h5>Journal 승인 등록</h5>
    <p>서버가 제공한 승인 후보를 확인하고 앱의 인증 사용자로 등록합니다. 이 화면은 외부 실행 권한이나 검증 증명을 만들어내지 않습니다. 등록·실행 시작·결과 채택은 각각 별도 작업입니다.</p>
    <p>합성 시험의 성공은 실제 계정·Provider 호출·과학적 검토의 증거가 아닙니다. 아래 발급 주체와 증명 제한을 확인하세요.</p>
    <button type="button" disabled={disabled || loading || busy} onClick={() => void inspect()}>Journal 승인 후보 확인</button>
    {loading ? <p role="status">승인 후보 확인 중</p> : null}
    {disabled ? <p>다른 요청의 응답 또는 실행 이력을 확인한 뒤 승인 등록을 계속할 수 있습니다.</p> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {shown && !currentOffer ? <p>실행 상태가 바뀌었습니다. 새 등록 전 승인 후보를 다시 확인하세요.</p> : null}
    {currentOffer && !currentOffer.approval_allowed ? <p role="status">{unavailable(currentOffer.reason)}</p> : null}
    {offer && currentOffer?.manifest ? <>
      <p className="local-report-id">승인 식별자: {offer.authorization_id}</p>
      <p>발급 주체: {offer.issuer_id} · 정책: {offer.policy_version}</p>
      <p>원본 등록 시각: {stamp(offer.registered_at)} · 승인 만료: {stamp(offer.expires_at)} · 증명 만료: {stamp(offer.proof_expires_at)}</p>
      <p>최대 시도: {offer.max_attempts}회 · 누적 출력 토큰 예약 한도: {offer.max_reserved_output_tokens}</p>
      <p>예약 한도는 실제 토큰 사용량·비용 상한이 아닙니다. 유효 여부는 등록 시 서버가 다시 확인합니다.</p>
      <p className="local-report-id">고정 요청 식별 지문: {currentOffer.contract_digest}</p>
      <p className="local-report-id">승인 원문 SHA-256: {offer.record_sha256}</p>
      <p className="local-report-id">승인 정규화 지문: {offer.record_digest}</p>
      <p className="local-report-id">전송 조건 지문: {offer.binding.command_shape_sha256}</p>
      <div aria-label="승인 증명 제한">{offer.proof_limitations.length ? <ul>{offer.proof_limitations.map((item, index) => <li key={index} style={{ whiteSpace: "pre-wrap" }}>{item}</li>)}</ul> : <p>출처가 별도 제한을 기록하지 않았습니다. 실제 모델 실행이나 연구 품질이 검증됐다는 뜻은 아닙니다.</p>}</div>
      <JournalAdapterManifest value={currentOffer.manifest} context="preview"/>
      <button type="button" disabled={!canRegister} onClick={() => void approve()}>확인한 Journal 승인 등록</button>
    </> : null}
    {pending ? <><p>응답 확인이 끝나지 않은 승인 등록 요청을 보존했습니다. 새로고침·상태 변경·승인 출처 비활성화 후에도 같은 내용을 재조회합니다. 화면을 떠나면 저장된 승인 이력부터 확인하세요.</p>
      <p>최초 요청이 저장되지 않았다면 같은 요청으로 원래 승인 기록을 등록할 수 있습니다. 새 권한을 발급하거나 실행을 시작하지 않습니다.</p>
      <p className="local-report-id">다시 확인할 승인: {pending.body.authorization_id}</p>
      <p className="local-report-id">다시 확인할 승인 원문 SHA-256: {pending.body.expected_record_sha256}</p>
      <button type="button" disabled={disabled || loading || busy} onClick={() => void approve()}>같은 Journal 승인 등록 다시 확인</button></> : null}
    {busy ? <p role="status">승인 등록 결과 확인 중</p> : null}
  </section>;
}
