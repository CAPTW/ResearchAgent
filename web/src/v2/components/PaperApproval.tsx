import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { getPaperApproval, readPaperApproval, registerPaperApproval, type PaperApprovalPreview, type PaperApprovalRequest } from "../../lib/paperApproval";
import { readCodexPaperApproved } from "../../lib/reviewCodexExecution";
import type { TransportDetail } from "../../lib/reviewTransport";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";

type Props = { root: string; selected: TransportDetail; disabled?: boolean; onBusy: (value: boolean) => void;
  onPending: (value: boolean) => void; onUpdated: (value: TransportDetail) => void };
type Pending = { body: PaperApprovalRequest; frozen: TransportDetail; authorizationId: string };
export function PaperApproval(props: Props) { return <Approval key={JSON.stringify([props.root, props.selected.contract.execution_id])} {...props}/>; }
function Approval({ root, selected, disabled = false, onBusy, onPending, onUpdated }: Props) {
  const { preferences } = useOptionalAppearance();
  const [preview, setPreview] = useState<PaperApprovalPreview | null>(null), [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const alive = useRef(true), locked = useRef(false), latest = useRef(selected), callbacks = useRef({ onBusy, onPending });
  latest.current = selected; callbacks.current = { onBusy, onPending };
  useEffect(() => { alive.current = true; return () => { alive.current = false; callbacks.current.onBusy(false); callbacks.current.onPending(false); }; }, []);
  useEffect(() => { setPreview(null); }, [selected.current.sequence]);
  function working(value: boolean) { setBusy(value); callbacks.current.onBusy(value); }
  function remember(value: Pending | null) { setPending(value); callbacks.current.onPending(value !== null); }
  const current = preview?.observed_sequence === selected.current.sequence ? preview : null;
  const mayRegister = !disabled && !busy && !pending && current?.approval_allowed && current.offer && selected.current.state === "READY";
  async function inspect() {
    if (disabled || locked.current) return;
    locked.current = true; working(true); setPreview(null); setError(""); setNotice("");
    try {
      const response = await getPaperApproval(root, selected.contract.execution_id);
      if (!alive.current) return;
      const value = readPaperApproval(response, latest.current);
      if (!value) throw new Error(); setPreview(value);
    } catch { if (alive.current) setError("승인 등록 조건을 확인하지 못했습니다. 인증된 사용자·주제 쓰기 권한과 최신 요청 상태를 확인한 뒤 다시 조회하세요."); }
    finally { locked.current = false; if (alive.current) working(false); }
  }
  async function submit() {
    if (disabled || locked.current || !pending && !mayRegister) return;
    const command: Pending = pending ?? { frozen: selected, authorizationId: current!.offer!.authorization_id,
      body: { request_id: crypto.randomUUID(), expected_sequence: selected.current.sequence,
        expected_contract_sha256: current!.contract_sha256, expected_authority_sha256: current!.offer!.record_sha256 } };
    locked.current = true; working(true); remember(command); setError(""); setNotice("");
    try {
      const response = await registerPaperApproval(root, command.frozen.contract.execution_id, command.body);
      if (!alive.current) return;
      const value = readCodexPaperApproved(response, command.frozen, command.body, command.authorizationId);
      if (!value || value.current.sequence < latest.current.current.sequence) throw new Error();
      remember(null); setPreview(null); onUpdated(value);
      setNotice("논문 승인 등록 결과를 확인했습니다. 새 권한 발급이나 모델 실행은 시작하지 않았습니다. 실행하려면 서버 허용 조건을 별도로 확인하세요.");
    } catch (reason) {
      if (alive.current) {
        const rejected = reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && ![408, 429].includes(reason.status);
        if (rejected) { remember(null); setPreview(null); }
        setError(rejected ? "승인 등록이 거절되었습니다. 원본 변경·인증·유효 기간과 최신 이력을 확인한 뒤 조건을 다시 조회하세요."
          : "승인 등록 응답을 확인하지 못했습니다. 같은 요청으로 저장 결과를 다시 확인하세요. 실행은 자동으로 시작하지 않습니다.");
      }
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  const offer = current?.offer;
  const reasons: Record<string, string> = { paper_approval_disabled: "서버 승인 소스가 연결되지 않았습니다.",
    paper_approval_source_unavailable: "서버의 승인 원본을 조회할 수 없습니다.", paper_approval_not_ready: "새 승인 등록이 가능한 요청 상태가 아닙니다.",
    paper_review_authority_expired: "승인 유효 기간이 지났습니다." };
  return <section className="review-preparation-record" aria-label="논문 서버 승인 등록" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <h4>논문 서버 승인 등록</h4>
    <p>독립적으로 발급된 서버 승인 기록을 현재 요청에 등록합니다. 조회·등록은 로그인, 새 승인 발급, 모델 실행, 논문 전체 검토가 아닙니다.</p>
    <V2Button disabled={disabled || busy} onClick={() => void inspect()}>선택한 논문 요청의 승인 등록 조건 확인</V2Button>
    {busy ? <p role="status">승인 등록 상태 확인 중</p> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {current && !current.approval_allowed ? <p role="status">승인 등록 불가: {reasons[current.reason ?? ""] ?? "현재 승인 원본과 요청 조건을 사용할 수 없습니다."} 기존 이력 조회는 가능합니다.</p> : null}
    {offer ? <>
      <p>발급 주체: {offer.issuer_id} · 정책: {offer.policy_version}</p>
      <p>{offer.input_origin === "FIXTURE_SYNTHETIC" ? "합성 시험 자료" : "Provider 전달 대상 자료"} · 요청 모델: {selected.contract.prepared.model}</p>
      <p>승인 만료: {formatTimestamp(offer.expires_at, preferences.timeFormat, "ko-KR")}</p>
      <p>허용 시도: {offer.max_attempts}회 · 예약 출력 토큰: {offer.reserved_output_tokens}. 실제 과금·모델 동작의 보증은 아닙니다.</p>
      <details><summary>고정 승인 식별자와 원본 해시</summary><p>{offer.authorization_id}</p><p>{offer.record_sha256}</p></details>
      <V2Button disabled={!mayRegister} onClick={() => void submit()}>확인한 논문 승인 기록 등록</V2Button>
    </> : null}
    {pending ? <><p>응답 확인이 끝나지 않은 등록 요청을 보존했습니다. 화면을 떠난 뒤에는 저장된 승인 이력부터 확인하세요.</p>
      <p>최초 요청이 저장되지 않았다면 같은 요청으로 원래 승인 기록을 등록할 수 있습니다. 새 권한을 발급하거나 실행을 시작하지 않습니다.</p>
      <details><summary>다시 확인할 승인 원본</summary><p>{pending.authorizationId}</p><p>{pending.body.expected_authority_sha256}</p></details>
      <V2Button disabled={disabled || busy} onClick={() => void submit()}>같은 논문 승인 등록 요청 다시 확인</V2Button></> : null}
  </section>;
}
