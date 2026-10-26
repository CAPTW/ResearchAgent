import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import type { PaperApprovalPreview } from "../../lib/paperApproval";
import type { TransportDetail, TransportList } from "../../lib/reviewTransport";
import { actAnthropicNative, decideAnthropicNative, getAnthropicNativeApproval, getAnthropicNativePreview, getAnthropicNativeStatus, isAnthropicNativeV2,
  prepareAnthropicNative, readAnthropicNativeApproval, readAnthropicNativeApproved, readAnthropicNativeDispatched,
  readAnthropicNativePrepared, readAnthropicNativePreview, readAnthropicNativeRevoked, readAnthropicNativeStatus, readNativeCredentialBinding,
  readAnthropicOwnerDecision, type AnthropicOwnerDecision, type AnthropicNativeApproval,
  type AnthropicNativeDispatch, type AnthropicNativePrepare, type AnthropicNativePreview, type AnthropicNativeStatus } from "../../lib/reviewAnthropicNative";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";

type Props = { root: string; preparationId: string; listing: TransportList | null; selected: TransportDetail | null; disabled?: boolean;
  onBusy: (value: boolean) => void; onPending: (value: boolean, executionId?: string) => void; onUpdated: (value: TransportDetail) => void };
type Pending = { kind: "prepare"; body: AnthropicNativePrepare; shown: AnthropicNativePreview }
  | { kind: "decision"; body: AnthropicOwnerDecision; frozen: TransportDetail }
  | { kind: "approve"; body: AnthropicNativeDispatch; frozen: TransportDetail; offer: NonNullable<PaperApprovalPreview["offer"]> }
  | { kind: "dispatch" | "revoke"; body: AnthropicNativeDispatch; frozen: TransportDetail };
const actionName = { prepare: "준비", decision: "승인 결정", approve: "승인", dispatch: "실행", revoke: "철회" };
const activeStates = ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED"];
const validity = { NOT_YET_VALID: "승인 유효 기간 전", WITHIN_VALIDITY_WINDOW: "승인 유효 기간 내", EXPIRED: "승인 유효 기간 만료", REVOKED: "승인 철회됨" };
function unavailable(reason: string | null) {
  if (reason === "paper_anthropic_native_model_required") return "설정의 모델 연결에서 Anthropic API 모델을 저장한 뒤 전송 조건을 다시 확인하세요. 기본 Provider를 바꿀 필요는 없습니다.";
  if (reason?.includes("credential_changed")) return "저장된 API 키가 바뀌었거나 삭제되어 새 실행을 차단했습니다. 기존 결과 조회·취소·철회는 계속 사용할 수 있습니다.";
  if (reason?.includes("credential_unavailable")) return "저장 API 키를 사용할 수 없습니다. 설정의 Anthropic API 키 관리에서 입력·저장한 뒤 다시 확인하세요. 환경 변수 키로 자동 전환하지 않습니다.";
  if (reason?.includes("disabled") || reason?.includes("closed")) return "이 서버에서는 Anthropic Native 연결이 비활성화되어 있습니다. 기존 결과 조회와 허용된 로컬 복구는 계속 사용할 수 있습니다.";
  if (reason?.includes("expired")) return "승인 유효 기간이 지났습니다. 기존 이력 조회는 새 승인이나 실행을 만들지 않습니다.";
  if (reason?.includes("revoked")) return "이 실행의 서버 승인이 철회되었습니다.";
  if (reason?.includes("approval") || reason?.includes("authority") || reason === "review_execution_not_approved") return "현재 요청에 사용할 수 있는 서버 승인 기록이 없습니다. 독립적으로 제공된 승인과 최신 요청 상태를 확인하세요.";
  return "현재 서버의 Native 실행 조건을 사용할 수 없습니다. 고정된 요청 조건과 저장 이력을 확인하세요.";
}
export function ReviewAnthropicNative(props: Props) { return <Workspace key={JSON.stringify([props.root, props.preparationId])} {...props}/>; }
function Workspace({ root, preparationId, listing, selected, disabled = false, onBusy, onPending, onUpdated }: Props) {
  const { preferences } = useOptionalAppearance(), unknownId = useId(), consentId = useId();
  const [preview, setPreview] = useState<AnthropicNativePreview | null>(null), [approval, setApproval] = useState<AnthropicNativeApproval | null>(null);
  const [consented, consent] = useState(false);
  const [status, setStatus] = useState<AnthropicNativeStatus | null>(null), [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false), [acknowledged, acknowledge] = useState(false), [error, setError] = useState("");
  const [notice, setNotice] = useState<{ executionId: string; text: string } | null>(null);
  const alive = useRef(true), locked = useRef(false), latest = useRef({ listing, selected }), callbacks = useRef({ onBusy, onPending });
  latest.current = { listing, selected }; callbacks.current = { onBusy, onPending };
  useEffect(() => { alive.current = true; return () => { alive.current = false; callbacks.current.onBusy(false); callbacks.current.onPending(false); }; }, []);
  useEffect(() => { setApproval(null); setStatus(null); acknowledge(false); consent(false); }, [selected?.contract.execution_id, selected?.current.sequence, listing?.preparation_digest]);
  const shown = preview?.preparation_digest === listing?.preparation_digest ? preview : null;
  const currentApproval = approval?.execution_id === selected?.contract.execution_id && approval?.observed_sequence === selected?.current.sequence ? approval : null;
  const currentStatus = status?.execution_id === selected?.contract.execution_id && status?.observed_sequence === selected?.current.sequence ? status : null;
  const blocked = disabled || busy || pending !== null, eligible = isAnthropicNativeV2(selected);
  const unknown = !!listing?.outcome_unknown_pending || !!selected?.current.outcome_unknown;
  const active = !!listing?.items.some(item => activeStates.includes(item.current.state)) || !!selected && activeStates.includes(selected.current.state);
  const mayPrepare = !blocked && shown?.enabled && listing?.page === 1 && !active && (!unknown || acknowledged);
  const offer = currentApproval?.offer;
  const mayApprove = !blocked && eligible && selected?.current.state === "READY" && currentApproval?.approval_allowed && !!offer && Date.parse(offer.expires_at) > Date.now();
  const mayDecide = !blocked && eligible && selected?.current.state === "READY" && currentApproval?.local_owner_decision_allowed === true && consented;
  const mayDispatch = !blocked && eligible && selected?.current.state === "APPROVED" && currentStatus?.enabled && currentStatus.reason === null
    && currentStatus.authority?.validity === "WITHIN_VALIDITY_WINDOW" && currentStatus.authority.revoked_at === null
    && Date.parse(currentStatus.authority.expires_at) > Date.now() && !!selected.current.deadline && Date.parse(selected.current.deadline) > Date.now();
  const mayRevoke = !blocked && eligible && !!selected?.authority && selected.authority.validity !== "REVOKED" && selected.authority.revoked_at === null;
  function working(value: boolean) { setBusy(value); callbacks.current.onBusy(value); }
  function remember(value: Pending | null) { setPending(value); callbacks.current.onPending(value !== null, value && value.kind !== "prepare" ? value.frozen.contract.execution_id : undefined); }
  async function inspect(kind: "preview" | "approval" | "status") {
    if (locked.current || disabled || pending || !listing || kind !== "preview" && !eligible) return;
    locked.current = true; working(true); setError(""); setNotice(null);
    if (kind === "preview") { setPreview(null); acknowledge(false); } else if (kind === "approval") setApproval(null); else setStatus(null);
    const identity = selected?.contract.execution_id;
    try {
      const response = kind === "preview" ? await getAnthropicNativePreview(root) : kind === "approval"
        ? await getAnthropicNativeApproval(root, identity!) : await getAnthropicNativeStatus(root, identity!);
      if (!alive.current) return;
      if (kind === "preview") {
        const value = latest.current.listing && readAnthropicNativePreview(response, preparationId, latest.current.listing);
        if (!value) throw new Error(); setPreview(value);
      } else {
        const current = latest.current.selected;
        if (!current || current.contract.execution_id !== identity) throw new Error();
        if (kind === "approval") { const value = readAnthropicNativeApproval(response, current); if (!value) throw new Error(); setApproval(value); }
        else { const value = readAnthropicNativeStatus(response, current); if (!value) throw new Error(); setStatus(value); }
      }
    } catch (reason) {
      if (alive.current) setError(reason instanceof ApiError && reason.status === 403
        ? "Native 검토는 로컬 같은 출처의 /app에서 관리자 권한으로만 확인할 수 있습니다. 저장된 이력과 접근 조건을 확인하세요."
        : "Anthropic Native 조건을 확인하지 못했습니다. 최신 실행 이력과 접근 권한을 확인한 뒤 다시 조회하세요.");
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  async function submit(kind: Pending["kind"]) {
    const allowed = kind === "prepare" ? mayPrepare : kind === "decision" ? mayDecide : kind === "approve" ? mayApprove : kind === "dispatch" ? mayDispatch : mayRevoke;
    if (locked.current || disabled || !pending && !allowed) return;
    if (!pending && (kind === "approve" && (!offer || Date.parse(offer.expires_at) <= Date.now())
      || kind === "dispatch" && (!selected?.current.deadline || Date.parse(selected.current.deadline) <= Date.now()
        || !currentStatus?.authority || Date.parse(currentStatus.authority.expires_at) <= Date.now()))) {
      setError("승인 유효 시간이 지났습니다. 실행 이력을 새로고침하고 현재 조건을 확인하세요."); return;
    }
    let command: Pending;
    if (pending) command = pending;
    else if (kind === "prepare") command = { kind, shown: shown!, body: { request_id: crypto.randomUUID(), expected_preparation_digest: shown!.preparation_digest,
      expected_binding_sha256: shown!.binding_sha256!, acknowledge_unknown: unknown && acknowledged } };
    else if (kind === "decision") command = { kind, frozen: selected!, body: { request_id: crypto.randomUUID(), expected_sequence: selected!.current.sequence,
      expected_contract_sha256: selected!.contract_sha256!, expected_binding_sha256: currentApproval!.local_owner_binding_sha256! } };
    else {
      const body = { request_id: crypto.randomUUID(), expected_sequence: selected!.current.sequence, expected_contract_sha256: selected!.contract_sha256!,
        expected_authority_sha256: kind === "approve" ? offer!.record_sha256 : kind === "dispatch" ? currentStatus!.authority!.record_sha256 : selected!.authority!.record_sha256 };
      command = kind === "approve" ? { kind, frozen: selected!, body, offer: offer! } : { kind, frozen: selected!, body };
    }
    locked.current = true; working(true); remember(command); setError(""); setNotice(null);
    try {
      const response = command.kind === "prepare" ? await prepareAnthropicNative(root, command.body)
        : command.kind === "decision" ? await decideAnthropicNative(root, command.frozen.contract.execution_id, command.body)
        : await actAnthropicNative(root, command.frozen.contract.execution_id, command.kind, command.body);
      if (!alive.current) return;
      if (command.kind === "decision") {
        if (!readAnthropicOwnerDecision(response, command.frozen, command.body)
          || latest.current.selected?.contract.execution_id !== command.frozen.contract.execution_id) throw new Error();
        remember(null); setApproval(null); consent(false);
        setNotice({ executionId: command.frozen.contract.execution_id, text: "로컬 사용자의 승인 결정을 저장했습니다. 승인 등록 조건을 다시 확인한 뒤 등록하세요. 외부 실행은 별도 실행 버튼으로 시작하며 아직 수행하지 않았습니다." });
        return;
      }
      const value = command.kind === "prepare" ? readAnthropicNativePrepared(response, command.shown, command.body)
        : command.kind === "approve" ? readAnthropicNativeApproved(response, command.frozen, command.body, command.offer)
          : command.kind === "dispatch" ? readAnthropicNativeDispatched(response, command.frozen, command.body)
            : readAnthropicNativeRevoked(response, command.frozen, command.body);
      if (!value || latest.current.selected?.contract.execution_id === value.contract.execution_id && value.current.sequence < latest.current.selected.current.sequence) throw new Error();
      remember(null); setApproval(null); setStatus(null); acknowledge(false); onUpdated(value);
      setNotice({ executionId: value.contract.execution_id, text: command.kind === "prepare" ? "Native 요청 준비를 저장했습니다. 승인 등록과 외부 실행은 아직 수행하지 않았습니다."
        : command.kind === "approve" ? "서버 승인 등록을 확인했습니다. 외부 실행은 별도 실행 버튼으로 시작합니다."
          : command.kind === "dispatch" ? "Native 실행 요청의 저장 상태를 확인했습니다. 결과 수신과 입력 기록 저장은 실행 이력에서 확인하세요."
            : "Native 승인을 철회했습니다. 기존 결과는 보존되며 진행 중인 외부 요청의 물리적 중단을 보장하지 않습니다." });
    } catch (reason) {
      if (alive.current) {
        const rejected = reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && ![408, 429].includes(reason.status);
        if (rejected) { remember(null); setPreview(null); setApproval(null); setStatus(null); acknowledge(false); }
        setError(rejected ? "Native 요청이 거부되었습니다. 키 revision·승인·최신 실행 이력을 확인하고 전송 조건을 다시 조회하세요."
          : "Native 요청의 응답을 확인하지 못했습니다. 처음 요청을 그대로 보존했습니다. 같은 요청 다시 확인으로 저장 결과를 확인하세요. 자동으로 실행하거나 새 요청을 만들지 않습니다.");
      }
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  const scope = shown?.prepared?.packet.source_preparation, keyBinding = readNativeCredentialBinding(shown?.manifest);
  const selectedKey = readNativeCredentialBinding(selected?.contract.transport);
  const stamp = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");
  return <section className="review-preparation-record" aria-label="서버 연결 Anthropic Native 검토" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <h4>Anthropic Native 선택 구간 검토</h4>
    <p>저장 API 키와 서버에 고정된 모델로 선택 구간을 전송하는 경로입니다. 준비·승인 등록·외부 실행·결과 저장·사람의 검토는 별도 단계입니다.</p>
    <p>키 입력은 설정에서 합니다. 정상 로컬 앱은 아래에서 이 요청에 대한 사용자의 승인 결정을 저장할 수 있습니다. Provider 결과만으로 논문 전체 검토나 과학적 품질의 완료를 뜻하지 않습니다.</p>
    <V2Button disabled={disabled || busy || !!pending || !listing} onClick={() => void inspect("preview")}>Anthropic Native 전송 조건 확인</V2Button>
    {busy ? <p role="status">Anthropic Native 조건 확인 중</p> : null}{error ? <p role="alert">{error}</p> : null}
    {notice && notice.executionId === selected?.contract.execution_id ? <p role="status">{notice.text}</p> : null}
    {shown && !shown.enabled ? <p role="status">{unavailable(shown.reason)}</p> : null}
    {shown?.enabled && scope && shown.prepared && shown.manifest && keyBinding ? <>
      <p>전송할 선택 범위: {scope.selection.start}–{scope.selection.end} · {scope.selection.chars}자 (Unicode 코드 포인트). 정규화: {scope.normalization}</p>
      <p>출처: {scope.source.run_id} · {scope.source.paper_id} · PROVIDER_RESPONSE</p>
      <details><summary>Anthropic Native로 전송할 고정 본문</summary><pre tabIndex={0} aria-label="Anthropic Native 전송 본문" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{shown.prepared.packet.evidence_spans.map(span => span.text).join("\n")}</pre></details>
      <p>요청 모델: {shown.prepared.model} · 요청 출력 토큰: {shown.prepared.provider_request.max_output_tokens}</p>
      <p>저장 키: anthropic_api · revision {keyBinding.revision} · Windows 사용자 암호화 저장소 (LOCAL_DPAPI). 키 유효성·계정·비용 한도를 보증하지 않습니다.</p>
      <p>전송 대상: https://api.anthropic.com/v1/messages · Native HTTP · 자동 재시도 없음. 온도는 전송하지 않고 Provider 기본값을 사용합니다.</p>
      <p>HTTP 비활성 제한: {String(shown.manifest.timeout_seconds)}초 (개별 작업 기준이며 총 실행 시간 상한이 아닙니다).</p>
      <p>Messages V2 · ANTHROPIC_JSON_SCHEMA_V2 · 원래 로컬 스키마를 system 문맥에도 포함하고 결과를 로컬 계약으로 검사합니다.</p>
      <p>요청 본문 {String(shown.manifest.body_bytes)}바이트 · SHA-256: {String(shown.manifest.body_sha256)}</p>
      <details><summary>고정 전송 조건 해시</summary><p>{shown.binding_sha256}</p><p>system SHA-256: {String(shown.manifest.effective_system_sha256)}</p></details>
      {unknown ? <label htmlFor={unknownId}><input id={unknownId} type="checkbox" checked={acknowledged} disabled={blocked} style={{ width: "auto", display: "inline-block", marginRight: 8 }} onChange={event => acknowledge(event.target.checked)}/>이전 결과가 불명확하며 별도 요청이 중복 전송·비용을 만들 수 있음을 확인했습니다.</label> : null}
      {active ? <p>진행 중인 요청을 확인하거나 취소한 뒤 새 요청을 준비하세요.</p> : null}
      {listing?.page !== 1 ? <p>첫 페이지에서 최신 실행 이력을 확인한 뒤 새 요청을 준비하세요.</p> : null}
      <V2Button disabled={!mayPrepare} onClick={() => void submit("prepare")}>이 구간으로 Anthropic Native 요청 준비</V2Button>
    </> : null}
    {eligible && selected ? <div>
      <p>선택한 Native 요청: {selected.contract.execution_id} · 고정 저장 키 revision {selectedKey?.revision}</p>
      {selected.authority?.authorization_kind === "OWNER_FIRST_OUTPUT" ? <>
        <p>첫 출력 실행 승인 — 이 요청의 출력 검증 실적이 아닙니다</p><p>결정 SHA-256: {selected.authority.decision_sha256}</p>
      </> : null}
      <V2Button disabled={blocked} onClick={() => void inspect("approval")}>선택한 Native 요청의 승인 등록 조건 확인</V2Button>
      {currentApproval && !currentApproval.approval_allowed ? <p role="status">승인 등록 불가: {unavailable(currentApproval.reason)}</p> : null}
      {currentApproval?.local_owner_decision_allowed && selected.current.state === "READY" ? <div>
        <p>로컬 사용자 승인: 고정된 본문·문맥·스키마를 https://api.anthropic.com/v1/messages로 전송합니다. 요청 모델 {selected.contract.prepared.model}, 출력 토큰 상한 요청 {selected.contract.prepared.provider_request.max_output_tokens}, 저장 키 revision {selectedKey?.revision}입니다. API 비용이 발생할 수 있으며 실제 토큰·요금 상한은 보증하지 않습니다.</p>
        <details><summary>승인할 요청의 고정 본문·문맥·계획 확인</summary><pre tabIndex={0} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(selected.contract.prepared.packet, null, 2)}</pre></details>
        <p>이 결정은 5분 동안 한 번의 실행만 허용하며 자동 재시도하지 않습니다. 출력 검증 실적·논문 전체 검토·과학적 품질 승인과는 다릅니다. 결정 저장 후 승인 등록과 실행을 각각 눌러야 합니다.</p>
        <label htmlFor={consentId}><input id={consentId} type="checkbox" checked={consented} disabled={blocked}
          style={{ width: "auto", display: "inline-block", marginRight: 8 }} onChange={event => consent(event.target.checked)}/>
          고정된 본문과 요청 모델, 저장 키 revision, 외부 전송 및 API 비용 가능성을 확인하고 이 요청의 1회 실행을 승인합니다.</label>
        <V2Button disabled={!mayDecide} onClick={() => void submit("decision")}>이 요청의 외부 전송·1회 실행 승인 기록 만들기</V2Button>
      </div> : null}
      {offer ? <>
        {offer.authorization_kind === "OWNER_FIRST_OUTPUT" ? <>
          <p>첫 출력 실행 승인 — 이 요청의 출력 검증 실적이 아닙니다</p><p>결정 SHA-256: {offer.decision_sha256}</p>
        </> : null}
        <p>발급 주체: {offer.issuer_id} · 정책: {offer.policy_version}</p><p>승인 만료: {stamp(offer.expires_at)}</p>
        <p>허용 시도: {offer.max_attempts}회 · 예약 출력 토큰: {offer.reserved_output_tokens}. 실제 과금 한도 보증은 아닙니다.</p>
        <details><summary>등록할 승인 원본</summary><p>{offer.authorization_id}</p><p>{offer.record_sha256}</p></details>
        <V2Button disabled={!mayApprove} onClick={() => void submit("approve")}>확인한 Anthropic Native 승인 등록</V2Button>
      </> : null}
      <V2Button disabled={blocked} onClick={() => void inspect("status")}>선택한 Native 요청의 실행 조건 확인</V2Button>
      {currentStatus ? <>
        {!currentStatus.enabled ? <p role="status">{unavailable(currentStatus.reason)}</p> : null}
        {currentStatus.authority ? <p>{validity[currentStatus.authority.validity]} · 만료 {stamp(currentStatus.authority.expires_at)}</p> : <p>등록된 서버 실행 승인이 없습니다.</p>}
        <p>실행 시 서버가 현재 키 revision과 고정 입력·승인 조건을 다시 확인합니다. 이 버튼은 실제 외부 전송을 요청합니다.</p>
        <V2Button disabled={!mayDispatch} onClick={() => void submit("dispatch")}>서버가 허용한 Anthropic Native 검토 실행</V2Button>
      </> : null}
      {selected.authority && selected.authority.validity !== "REVOKED" && selected.authority.revoked_at === null ? <V2Button disabled={!mayRevoke} onClick={() => void submit("revoke")}>Anthropic Native 승인 철회</V2Button> : null}
    </div> : null}
    {pending ? <><p>응답을 확인하지 못한 Native {actionName[pending.kind]} 요청을 그대로 보존했습니다. {pending.kind !== "prepare" ? `실행: ${pending.frozen.contract.execution_id}. 같은 실행의 조회·취소·만료 복구·수신 결과 저장은 아래 이력에서 가능합니다. ` : ""}화면을 떠난 뒤에는 저장 이력부터 확인하세요.</p>
      <V2Button disabled={disabled || busy} onClick={() => void submit(pending.kind)}>같은 Anthropic Native {actionName[pending.kind]} 요청 다시 확인</V2Button></> : null}
  </section>;
}
