import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import type { TransportDetail, TransportList } from "../../lib/reviewTransport";
import { dispatchCodexPaper, getCodexPaperPreview, getCodexPaperStatus, isCodexPaperV2, prepareCodexPaper,
  readCodexPaperDispatched, readCodexPaperPrepared, readCodexPaperPreview, readCodexPaperStatus,
  type CodexPaperDispatch, type CodexPaperPrepare, type CodexPaperPreview, type CodexPaperStatus } from "../../lib/reviewCodexExecution";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";

type Props = { root: string; preparationId: string; listing: TransportList | null; selected: TransportDetail | null;
  disabled?: boolean; onBusy: (value: boolean) => void; onPending?: (value: boolean) => void; onUpdated: (value: TransportDetail) => void };
type Pending = { kind: "prepare"; body: CodexPaperPrepare; shown: CodexPaperPreview }
  | { kind: "dispatch"; body: CodexPaperDispatch; frozen: TransportDetail };
const validity = { NOT_YET_VALID: "서버 승인 유효 기간 전", WITHIN_VALIDITY_WINDOW: "서버 승인 유효 기간 내", EXPIRED: "서버 승인 유효 기간 만료", REVOKED: "서버 승인 철회됨" };
const activeStates = ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED"];
function unavailable(reason: string | null) {
  if (reason === "paper_codex_disabled") return "이 서버에서는 Codex 논문 검토 연결이 비활성화되어 있습니다. 기존 기록과 허용된 로컬 복구는 계속 사용할 수 있습니다.";
  if (reason === "paper_codex_runtime_changed" || reason === "paper_codex_binding_changed") return "서버 실행 조건이 바뀌었습니다. 서버 운영자가 고정된 조건을 확인해야 합니다.";
  if (reason === "review_execution_not_approved" || reason === "paper_codex_authority_missing") return "이 요청에 현재 사용할 수 있는 서버 승인이 없습니다. 준비와 승인·실행은 별도 단계입니다.";
  if (reason === "paper_review_authority_revoked" || reason === "paper_review_authority_expired") return "서버 승인이 철회되었거나 유효 시간이 지났습니다. 이력 조회는 새 승인을 발급하지 않습니다.";
  return "현재 Codex 검토 실행 조건을 사용할 수 없습니다. 서버 조건과 저장 이력을 확인하세요.";
}
export function ReviewCodexExecution(props: Props) { return <Workspace key={JSON.stringify([props.root, props.preparationId])} {...props}/>; }
function Workspace({ root, preparationId, listing, selected, disabled = false, onBusy, onPending, onUpdated }: Props) {
  const { preferences } = useOptionalAppearance(), unknownId = useId();
  const [preview, setPreview] = useState<CodexPaperPreview | null>(null), [status, setStatus] = useState<CodexPaperStatus | null>(null);
  const [pending, setPending] = useState<Pending | null>(null), [busy, setBusy] = useState(false), [acknowledged, acknowledge] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const alive = useRef(true), locked = useRef(false), latest = useRef({ listing, selected }), callbacks = useRef({ onBusy, onPending });
  latest.current = { listing, selected }; callbacks.current = { onBusy, onPending };
  useEffect(() => { alive.current = true; return () => { alive.current = false; callbacks.current.onBusy(false); callbacks.current.onPending?.(false); }; }, []);
  useEffect(() => { setStatus(null); acknowledge(false); }, [selected?.contract.execution_id, selected?.current.sequence, listing?.preparation_digest]);
  const currentPreview = preview?.preparation_digest === listing?.preparation_digest ? preview : null;
  const currentStatus = status?.execution_id === selected?.contract.execution_id && status?.observed_sequence === selected?.current.sequence ? status : null;
  const unknown = !!listing?.outcome_unknown_pending || !!selected?.current.outcome_unknown;
  const active = !!listing?.items.some(item => activeStates.includes(item.current.state)) || !!selected && activeStates.includes(selected.current.state);
  const blocked = disabled || busy || pending !== null;
  const mayPrepare = !blocked && listing?.page === 1 && !active && currentPreview?.enabled && currentPreview.prepared && (!unknown || acknowledged);
  const mayDispatch = !blocked && selected?.current.state === "APPROVED" && currentStatus?.enabled && currentStatus.reason === null
    && currentStatus.authority?.validity === "WITHIN_VALIDITY_WINDOW" && currentStatus.authority.revoked_at === null;
  function working(value: boolean) { setBusy(value); callbacks.current.onBusy(value); }
  function remember(value: Pending | null) { setPending(value); callbacks.current.onPending?.(value !== null); }
  async function inspect(kind: "preview" | "dispatch") {
    if (locked.current || disabled || !listing || kind === "dispatch" && !selected) return;
    locked.current = true; working(true); setError(""); setNotice("");
    if (kind === "preview") { setPreview(null); acknowledge(false); } else setStatus(null);
    const identity = selected?.contract.execution_id;
    try {
      const response = kind === "preview" ? await getCodexPaperPreview(root) : await getCodexPaperStatus(root, identity!);
      if (!alive.current) return;
      if (kind === "preview") {
        const value = latest.current.listing && readCodexPaperPreview(response, preparationId, latest.current.listing);
        if (!value) throw new Error(); setPreview(value);
      } else {
        const current = latest.current.selected;
        if (!current || current.contract.execution_id !== identity) throw new Error();
        const value = readCodexPaperStatus(response, current);
        if (!value) throw new Error(); setStatus(value);
      }
    } catch { if (alive.current) setError("Codex 검토 조건을 확인하지 못했습니다. 최신 검토 이력과 접근 권한을 확인한 뒤 다시 조회하세요."); }
    finally { locked.current = false; if (alive.current) working(false); }
  }
  async function submit(kind: "prepare" | "dispatch") {
    if (locked.current || disabled || !pending && !(kind === "prepare" ? mayPrepare : mayDispatch)) return;
    const command: Pending = pending ?? (kind === "prepare" ? { kind, shown: currentPreview!, body: { request_id: crypto.randomUUID(),
      expected_preparation_digest: currentPreview!.preparation_digest, expected_binding_sha256: currentPreview!.binding_sha256!, acknowledge_unknown: unknown && acknowledged } }
      : { kind, frozen: selected!, body: { request_id: crypto.randomUUID(), expected_sequence: selected!.current.sequence,
        expected_contract_sha256: currentStatus!.contract_sha256, expected_authority_sha256: currentStatus!.authority!.record_sha256 } });
    locked.current = true; working(true); remember(command); setError(""); setNotice("");
    try {
      const response = command.kind === "prepare" ? await prepareCodexPaper(root, command.body)
        : await dispatchCodexPaper(root, command.frozen.contract.execution_id, command.body);
      if (!alive.current) return;
      const value = command.kind === "prepare" ? readCodexPaperPrepared(response, command.shown, command.body)
        : readCodexPaperDispatched(response, command.frozen, command.body);
      if (!value || latest.current.selected?.contract.execution_id === value.contract.execution_id && value.current.sequence < latest.current.selected.current.sequence) throw new Error();
      remember(null); setPreview(null); setStatus(null); acknowledge(false); onUpdated(value);
      setNotice(command.kind === "prepare" ? "Codex 검토 요청 준비를 저장했습니다. 실행 승인이나 모델 호출은 아직 수행하지 않았습니다."
        : "Codex 실행 요청의 저장 상태를 확인했습니다. 결과 수신·입력 기록 저장·사람의 검토는 별도 단계입니다.");
    } catch (reason) {
      if (alive.current) {
        const rejected = reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && ![408, 429].includes(reason.status);
        if (rejected) { remember(null); setPreview(null); setStatus(null); acknowledge(false); }
        setError(rejected ? "Codex 검토 요청이 거절되었습니다. 이력을 새로고침한 뒤 현재 조건을 다시 확인하세요."
          : "Codex 검토 요청의 응답을 확인하지 못했습니다. 같은 요청을 다시 보내 저장된 결과를 확인하세요. 새 요청을 만들지 않습니다.");
      }
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  const scope = currentPreview?.prepared?.packet.source_preparation;
  const stamp = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");
  return <section className="review-preparation-record" aria-label="서버 연결 Codex 논문 검토" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <h4>Codex 논문 검토 준비·실행</h4>
    <p>고정된 저장 본문의 선택 범위를 검토하는 연결입니다. 논문 전체 검토 완료를 뜻하지 않습니다. 요청 준비·서버 승인·실행·결과 저장·사람의 검토는 별도 단계입니다.</p>
    <p>이 화면은 승인 발급·로그인·계정 연결을 하지 않습니다. 실제 모델·구독 과금·도구 격리·비용 상한은 독립적으로 검증되지 않았습니다.</p>
    <V2Button disabled={disabled || busy || !listing} onClick={() => void inspect("preview")}>Codex 검토 입력·실행 조건 확인</V2Button>
    {busy ? <p role="status">Codex 검토 요청 확인 중</p> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {!disabled && currentPreview ? !currentPreview.enabled ? <p role="status">{unavailable(currentPreview.reason)}</p> : scope && currentPreview.prepared && currentPreview.manifest ? <>
      <p>전달할 범위: {scope.selection.start}–{scope.selection.end} · {scope.selection.chars}자 (Unicode 코드 포인트). 정규화된 저장 본문의 선택 범위입니다.</p>
      <p>출처: {scope.source.run_id} · {scope.source.paper_id} · 정규화: {scope.normalization}</p>
      <details><summary>Codex 검토에 전달할 고정 본문</summary><pre tabIndex={0} aria-label="Codex 검토 고정 본문" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{currentPreview.prepared.packet.evidence_spans.map(span => span.text).join("\n")}</pre></details>
      <p>요청 모델: {currentPreview.prepared.model} · Codex CLI V2 · 공식 CLI의 ChatGPT 로그인 경로 요청</p>
      <p>요청 출력 토큰: {currentPreview.prepared.provider_request.max_output_tokens} · 온도: {currentPreview.prepared.provider_request.temperature}. 실제 강제 여부가 보장되지 않습니다.</p>
      <p>요청 재시도 0회 · 스트림 재시도 0회. 실제 내부 재시도·호출 횟수는 검증되지 않았습니다.</p>
      <p>{currentPreview.input_origin === "FIXTURE_SYNTHETIC" ? "합성 시험 자료 (FIXTURE_SYNTHETIC)" : "Provider 결과를 위한 요청 (PROVIDER_RESPONSE)"} · 미리보기는 실행 승인이 아닙니다.</p>
      <p className="local-report-id">입력·실행 조건 SHA-256: {currentPreview.binding_sha256}</p>
      {unknown ? <label htmlFor={unknownId}><input id={unknownId} type="checkbox" checked={acknowledged} disabled={blocked} style={{ width: "auto", display: "inline-block", marginRight: 8 }} onChange={e => acknowledge(e.target.checked)}/>이전 결과가 불명확하며 별도 요청이 중복 호출·비용을 만들 수 있음을 확인했습니다.</label> : null}
      {active ? <p>진행 중인 요청을 확인하거나 취소한 뒤 새 요청을 준비하세요.</p> : null}
      {listing?.page !== 1 ? <p>첫 페이지에서 최신 실행 이력을 확인한 뒤 새 요청을 준비하세요.</p> : null}
      <V2Button disabled={!mayPrepare} onClick={() => void submit("prepare")}>이 입력으로 Codex 검토 요청 준비</V2Button>
    </> : null : null}
    {isCodexPaperV2(selected) ? <div>
      <V2Button disabled={disabled || busy} onClick={() => void inspect("dispatch")}>선택한 Codex 요청의 서버 허용 확인</V2Button>
      {!disabled && currentStatus ? <>
        {!currentStatus.enabled ? <p role="status">{unavailable(currentStatus.reason)}</p> : null}
        {currentStatus.authority ? <><p>{validity[currentStatus.authority.validity]}</p><p>발급 주체: {currentStatus.authority.issuer_id} · 정책: {currentStatus.authority.policy_version}</p>
          <p>승인 기간: {stamp(currentStatus.authority.granted_at)}–{stamp(currentStatus.authority.expires_at)}</p>
          <p className="local-report-id">승인 SHA-256: {currentStatus.authority.record_sha256}</p></> : <p>서버에 등록된 실행 승인이 없습니다.</p>}
        <p>조회 시점의 서버 조건입니다. 실행 시 서버가 승인과 고정된 입력·실행 조건을 다시 확인합니다.</p>
        <V2Button disabled={!mayDispatch} onClick={() => void submit("dispatch")}>서버가 허용한 Codex 검토 실행</V2Button>
      </> : null}
    </div> : null}
    {pending ? <><p>응답 확인이 끝나지 않은 {pending.kind === "prepare" ? "준비 요청" : `실행 요청 (${pending.frozen.contract.execution_id})`}을 보존했습니다. 화면을 떠난 뒤에는 저장 이력부터 확인하세요.</p>
      <V2Button disabled={disabled || busy} onClick={() => void submit(pending.kind)}>같은 Codex 검토 {pending.kind === "prepare" ? "준비" : "실행"} 요청 다시 보내기</V2Button></> : null}
  </section>;
}
