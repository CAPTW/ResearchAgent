import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import type { OfflineTransportApproval, OfflineTransportCommand, TransportDetail, TransportList } from "../../lib/reviewTransport";
import { actAnthropic, getAnthropicPreview, isAnthropicStructuredOffline, matchesAnthropicPreview, prepareAnthropic, readAnthropicAction, readAnthropicPrepared, readAnthropicPreview,
  type AnthropicAction, type AnthropicPrepare, type AnthropicPreview } from "../../lib/reviewAnthropicExecution";
import { V2Button } from "../V2Primitives";

type Props = { root: string; preparationId: string; listing: TransportList | null; selected: TransportDetail | null; disabled?: boolean;
  onBusy: (value: boolean) => void; onPending: (value: boolean, executionId?: string) => void; onUpdated: (value: TransportDetail) => void };
type Pending = { kind: "prepare"; body: AnthropicPrepare; shown: AnthropicPreview }
  | { kind: AnthropicAction; body: OfflineTransportCommand | OfflineTransportApproval; frozen: TransportDetail };
const actionName = { prepare: "준비", approve: "승인", dispatch: "실행", revoke: "철회" };
const activeStates = ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED"];
export function ReviewAnthropicExecution(props: Props) { return <Workspace key={JSON.stringify([props.root, props.preparationId])} {...props}/>; }
function Workspace({ root, preparationId, listing, selected, disabled = false, onBusy, onPending, onUpdated }: Props) {
  const unknownId = useId(), [acknowledged, acknowledge] = useState(false);
  const [preview, setPreview] = useState<AnthropicPreview | null>(null), [pending, setPending] = useState<Pending | null>(null), [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ executionId: string; text: string } | null>(null);
  const alive = useRef(true), locked = useRef(false), latest = useRef({ listing, selected }), callbacks = useRef({ onBusy, onPending });
  latest.current = { listing, selected }; callbacks.current = { onBusy, onPending };
  useEffect(() => { alive.current = true; return () => { alive.current = false; callbacks.current.onBusy(false); callbacks.current.onPending(false); }; }, []);
  useEffect(() => { acknowledge(false); }, [listing?.preparation_digest]);
  const currentPreview = preview?.enabled && preview.preview.preparation_digest !== listing?.preparation_digest ? null : preview;
  const shown = currentPreview?.enabled ? currentPreview.preview : null;
  const blocked = disabled || busy || pending !== null;
  const unknown = !!listing?.outcome_unknown_pending || !!selected?.current.outcome_unknown;
  const active = !!listing?.items.some(item => activeStates.includes(item.current.state)) || !!selected && activeStates.includes(selected.current.state);
  const eligible = isAnthropicStructuredOffline(selected);
  const canAct = !blocked && !!selected && matchesAnthropicPreview(selected, currentPreview);
  const mayPrepare = !blocked && !!shown && listing?.page === 1 && !active && (!unknown || acknowledged);
  const mayApprove = canAct && selected?.current.state === "READY";
  const mayDispatch = canAct && selected?.current.state === "APPROVED" && selected.authority?.validity === "WITHIN_VALIDITY_WINDOW"
    && selected.authority.revoked_at === null && !!selected.current.deadline && Date.parse(selected.current.deadline) > Date.now();
  const mayRevoke = canAct && !!selected?.authority && selected.authority.validity !== "REVOKED" && selected.authority.revoked_at === null;
  function working(value: boolean) { setBusy(value); callbacks.current.onBusy(value); }
  function remember(value: Pending | null) { setPending(value); callbacks.current.onPending(value !== null, value && value.kind !== "prepare" ? value.frozen.contract.execution_id : undefined); }
  async function inspect() {
    if (locked.current || disabled || pending || !listing) return;
    locked.current = true; working(true); setPreview(null); setError(""); setNotice(null); acknowledge(false);
    try {
      const response = await getAnthropicPreview(root); if (!alive.current) return;
      const value = latest.current.listing && readAnthropicPreview(response, preparationId, latest.current.listing);
      if (!value) throw new Error(); setPreview(value);
    } catch { if (alive.current) setError("Anthropic 합성 요청 조건을 확인하지 못했습니다. 접근 권한과 최신 준비 이력을 확인한 뒤 다시 조회하세요."); }
    finally { locked.current = false; if (alive.current) working(false); }
  }
  async function submit(kind: Pending["kind"]) {
    if (kind === "dispatch" && !pending && selected?.current.deadline && Date.parse(selected.current.deadline) <= Date.now()) {
      setError("합성 시험 승인 유효 시간이 지났습니다. 실행 이력을 새로고침하세요."); return;
    }
    const allowed = kind === "prepare" ? mayPrepare : kind === "approve" ? mayApprove : kind === "dispatch" ? mayDispatch : mayRevoke;
    if (locked.current || disabled || !pending && !allowed) return;
    const command: Pending = pending ?? (kind === "prepare" ? { kind, shown: currentPreview!, body: { request_id: crypto.randomUUID(),
      expected_preparation_digest: shown!.preparation_digest, expected_preview_sha256: shown!.preview_sha256, acknowledge_unknown: unknown && acknowledged } }
      : { kind, frozen: selected!, body: { request_id: crypto.randomUUID(), expected_sequence: selected!.current.sequence,
        ...(kind === "approve" ? { expected_contract_sha256: selected!.contract_sha256! } : {}) } });
    locked.current = true; working(true); remember(command); setError(""); setNotice(null);
    try {
      const response = command.kind === "prepare" ? await prepareAnthropic(root, command.body)
        : await actAnthropic(root, command.frozen.contract.execution_id, command.kind, command.body);
      if (!alive.current) return;
      const value = command.kind === "prepare" ? readAnthropicPrepared(response, command.shown, command.body, preparationId)
        : readAnthropicAction(response, command.frozen, command.kind, command.body);
      if (!value || latest.current.selected?.contract.execution_id === value.contract.execution_id && value.current.sequence < latest.current.selected.current.sequence) throw new Error();
      remember(null); acknowledge(false); onUpdated(value);
      setNotice({ executionId: value.contract.execution_id, text: command.kind === "prepare" ? "합성 요청 준비를 저장했습니다. 승인과 실행은 아직 수행하지 않았습니다."
        : command.kind === "approve" ? "이 고정 요청의 합성 시험 승인을 저장했습니다. 실행은 별도로 눌러야 합니다."
          : command.kind === "dispatch" ? "합성 실행 요청의 저장 상태를 확인했습니다. 결과 수신·입력 기록 저장은 실행 이력에서 확인하세요."
            : "합성 시험 승인 철회를 확인했습니다. 기존 결과는 보존되며 실행 중 작업의 물리적 중단을 뜻하지 않습니다." });
    } catch (reason) {
      if (alive.current) {
        const rejected = reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && ![408, 429].includes(reason.status);
        if (rejected) { remember(null); setPreview(null); acknowledge(false); }
        setError(rejected ? "합성 시험 요청이 거부되었습니다. 실행 이력을 새로고침하고 구조화 요청 조건을 다시 확인하세요."
          : "합성 시험 요청의 응답을 확인하지 못했습니다. 같은 요청 다시 보내기로 처음 요청의 저장 결과를 확인하세요. 자동으로 실행하거나 새 요청을 만들지 않습니다.");
      }
    } finally { locked.current = false; if (alive.current) working(false); }
  }
  const scope = shown?.prepared.packet.source_preparation;
  return <section className="review-preparation-record" aria-label="Anthropic 구조화 합성 검토" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <h4>Anthropic 구조화 요청 · 합성 연결 시험</h4>
    <p>고정된 선택 구간과 구조화 응답 계약으로 앱 연결을 시험합니다. 네트워크·실제 API 키·모델 호출을 사용하지 않는 합성 전용 경로입니다. 전체 논문 검토나 과학적 검증이 아닙니다.</p>
    <V2Button type="button" disabled={disabled || busy || !!pending || !listing} onClick={() => void inspect()}>Anthropic 구조화 요청 확인</V2Button>
    {busy ? <p role="status">합성 검토 요청 확인 중</p> : null}{error ? <p role="alert">{error}</p> : null}{notice && notice.executionId === selected?.contract.execution_id ? <p role="status">{notice.text}</p> : null}
    {currentPreview && !currentPreview.enabled ? <p role="status">이 서버에서는 Anthropic 구조화 합성 시험이 비활성화되어 있습니다. 기존 이력과 허용된 로컬 복구는 계속 사용할 수 있습니다.</p> : null}
    {shown && scope ? <>
      <p>선택 범위: {scope.selection.start}–{scope.selection.end} · {scope.selection.chars}자 (Unicode 코드 포인트). 정규화된 저장 본문의 선택 범위입니다.</p>
      <p>출처: {scope.source.run_id} · {scope.source.paper_id} · 정규화: {scope.normalization}</p>
      <details><summary>Anthropic 검토에 사용할 고정 본문</summary><pre tabIndex={0} aria-label="Anthropic 합성 검토 고정 본문" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{shown.prepared.packet.evidence_spans.map(span => span.text).join("\n")}</pre></details>
      <p>합성 시험 모델: {shown.specification.model} · 요청 출력 토큰: {shown.specification.max_output_tokens}</p>
      <p>Messages V2 구조화 형식 · 컴파일러 ANTHROPIC_JSON_SCHEMA_V2 · 원래 로컬 스키마를 system 문맥에도 포함합니다. 응답은 원래 로컬 계약으로 다시 검사합니다.</p>
      <p>HTTP 설정상 제한 시간: {String(shown.transport.timeout_seconds)}초 (개별 작업의 비활성 제한이며 총 실행 시간 상한이 아닙니다). 합성 시험에서는 실제 HTTP 통신을 하지 않습니다.</p>
      <p>요청 본문: {String(shown.transport.body_bytes)}바이트 · 요청 본문 SHA-256: {String(shown.transport.body_sha256)}</p>
      <p>미리보기 SHA-256: {shown.preview_sha256}</p>
      <p>준비·2분 동안 유효한 합성 시험 승인·실행·결과 저장·사람의 검토는 별도 단계입니다. 이 승인으로 실제 Anthropic HTTP 실행은 할 수 없습니다.</p>
      {unknown ? <label htmlFor={unknownId}><input id={unknownId} type="checkbox" checked={acknowledged} disabled={blocked} style={{ width: "auto", display: "inline-block", marginRight: 8 }} onChange={event => acknowledge(event.target.checked)}/>이전 결과가 불명확함을 확인했으며 별도의 합성 시험 요청을 준비합니다.</label> : null}
      {active ? <p>진행 중인 요청의 결과를 확인하거나 취소한 뒤 새 요청을 준비하세요.</p> : null}
      {listing?.page !== 1 ? <p>첫 페이지에서 최신 실행 이력을 확인한 뒤 새 요청을 준비하세요.</p> : null}
      <V2Button type="button" disabled={!mayPrepare} onClick={() => void submit("prepare")}>이 구간으로 합성 요청 준비</V2Button>
    </> : null}
    {eligible && selected ? <div>
      <p>선택한 합성 요청: {selected.contract.execution_id}</p>
      {!shown ? <p>구조화 요청 확인을 눌러 현재 서버의 합성 시험 조건을 확인하세요.</p> : !matchesAnthropicPreview(selected, currentPreview) ? <p>현재 미리보기와 선택한 요청의 고정 조건이 다릅니다. 이 요청에 새 승인·실행을 제공하지 않습니다.</p> : null}
      {selected.current.state === "READY" ? <V2Button type="button" disabled={!mayApprove} onClick={() => void submit("approve")}>합성 시험 승인</V2Button> : null}
      {selected.current.state === "APPROVED" ? <V2Button type="button" disabled={!mayDispatch} onClick={() => void submit("dispatch")}>합성 응답 시험 실행</V2Button> : null}
      {selected.authority && selected.authority.validity !== "REVOKED" && selected.authority.revoked_at === null ? <V2Button type="button" disabled={!mayRevoke} onClick={() => void submit("revoke")}>합성 시험 승인 철회</V2Button> : null}
      <p>입력 기록 저장 후 기존 ‘저장된 HTTP 입력·결과 열기’로 근거를 확인하세요. 논문 화면의 검토 범위 집계는 새로고침으로 확인할 수 있습니다.</p>
    </div> : null}
    {pending ? <><p>응답을 확인하지 못한 {actionName[pending.kind]} 요청을 그대로 보존했습니다. {pending.kind !== "prepare" ? `실행: ${pending.frozen.contract.execution_id}. 같은 실행의 새로고침·취소·만료 복구·수신 결과 저장은 아래 실행 이력에서 사용할 수 있습니다. ` : ""}화면을 떠난 뒤에는 저장 이력부터 확인하세요.</p>
      <V2Button type="button" disabled={disabled || busy} onClick={() => void submit(pending.kind)}>같은 합성 {actionName[pending.kind]} 요청 다시 보내기</V2Button></> : null}
  </section>;
}
