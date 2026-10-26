import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  controlReviewExecution, dispatchReviewExecution, executionRoot, getReviewExecution, listReviewExecutions, prepareReviewExecution,
  type PrepareReviewExecution, type ReviewExecutionAction, type ReviewExecutionControl, type ReviewExecutionDetail,
  type ReviewExecutionDispatch, type ReviewExecutionList, type ReviewExecutionState,
} from "../../lib/reviewExecution";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";

type Props = { root: string; id: string; onOpenReceipt?: (receiptId: string) => void };
type Pending = { kind: "prepare"; body: PrepareReviewExecution }
  | { kind: "control"; id: string; body: ReviewExecutionControl }
  | { kind: "dispatch"; id: string; body: ReviewExecutionDispatch };
const labels: Record<ReviewExecutionState, string> = {
  READY: "요청 준비됨", APPROVED: "오프라인 실행 승인됨", LEASED: "실행 담당 할당", RUNNING: "시도 시작 기록됨",
  RESULT_RECEIVED: "결과 수신됨 · 입력 기록 저장 대기", RESULT_SAVED: "결과 저장됨", FAILED: "실행 전 실패",
  INTERRUPTED: "중단됨", CANCELLED: "취소됨", REVOKED: "철회됨",
};
const activeStates: ReviewExecutionState[] = ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED"];
const reasons: Record<string, string> = {
  deadline_expired: "유효 시간이 지나 종료됨",
  worker_stopped: "작업자 종료로 정상 완료를 확인하지 못함",
  worker_start_failed: "작업자 시작 실패 · 실행 안 됨",
  adapter_failed: "검토 처리 오류 · 결과 상태 확인 필요",
  result_invalid: "수신 결과 형식·기록 계약 검증 실패",
};
const reasonText = (value: string) => Object.hasOwn(reasons, value) ? `${reasons[value]} (${value})` : value;
const errorText = (value: unknown) => value instanceof Error ? value.message : String(value);
const definitive = (value: unknown) => value instanceof ApiError && value.status >= 400 && value.status < 500 && ![408, 429].includes(value.status);

function Workspace({ root, onOpenReceipt }: { root: string; onOpenReceipt?: Props["onOpenReceipt"] }) {
  const { preferences } = useOptionalAppearance();
  const checkboxId = useId();
  const alive = useRef(true), locked = useRef(false), readVersion = useRef(0);
  const [listing, setListing] = useState<ReviewExecutionList | null>(null), [selected, setSelected] = useState<ReviewExecutionDetail | null>(null);
  const [page, setPage] = useState(1), [revision, setRevision] = useState(0), [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState(""), [error, setError] = useState("");
  const [pending, setPending] = useState<Pending | null>(null), [saving, setSaving] = useState(false), [acknowledgeUnknown, setAcknowledgeUnknown] = useState(false);
  const selectedId = useRef<string | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; readVersion.current += 1; }; }, []);
  useEffect(() => {
    const version = ++readVersion.current;
    setLoading(true); setReadError("");
    void listReviewExecutions(root, page).then(value => {
      if (!alive.current || readVersion.current !== version) return;
      setListing(value);
      const next = value.items.find(item => item.contract.execution_id === selectedId.current) ?? value.items[0] ?? null;
      setSelected(next); selectedId.current = next?.contract.execution_id ?? null; setLoading(false);
    }, reason => { if (alive.current && readVersion.current === version) { setReadError(errorText(reason)); setLoading(false); } });
  }, [root, page, revision]);
  const busy = loading || saving || pending !== null;
  const readable = !!listing && !readError && !loading;
  const enabled = readable && listing.enabled;
  const current = selected?.current;
  const unknown = !!listing?.outcome_unknown_pending || !!current?.outcome_unknown;
  const active = !!listing?.items.some(item => activeStates.includes(item.current.state)) || !!current && activeStates.includes(current.state);
  const mayPrepare = enabled && !busy && page === 1 && !active && (!unknown || acknowledgeUnknown);
  const mayControl = (action: ReviewExecutionAction) => readable && !busy && (listing.enabled || ["cancel", "recover", "save_result"].includes(action));
  const date = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");

  async function select(id: string) {
    if (busy) return;
    const version = ++readVersion.current; setLoading(true); setReadError("");
    try {
      const value = await getReviewExecution(root, id);
      if (alive.current && readVersion.current === version) { setSelected(value); selectedId.current = id; }
    } catch (reason) { if (alive.current && readVersion.current === version) setReadError(errorText(reason)); }
    finally { if (alive.current && readVersion.current === version) setLoading(false); }
  }
  async function submit(command: Pending) {
    if (locked.current) return;
    const exact = pending ?? command;
    locked.current = true; setSaving(true); setPending(exact); setError("");
    try {
      const value = exact.kind === "prepare" ? await prepareReviewExecution(root, exact.body)
        : exact.kind === "control" ? await controlReviewExecution(root, exact.id, exact.body)
        : await dispatchReviewExecution(root, exact.id, exact.body);
      if (alive.current) {
        setSelected(value); selectedId.current = value.contract.execution_id; setPending(null); setAcknowledgeUnknown(false);
        setListing(previous => previous ? {
          ...previous, outcome_unknown_pending: previous.outcome_unknown_pending || value.current.outcome_unknown,
          items: [value, ...previous.items.filter(item => item.contract.execution_id !== value.contract.execution_id)],
        } : previous);
      }
    } catch (reason) {
      if (alive.current) {
        setError(errorText(reason));
        if (definitive(reason)) { setPending(null); setLoading(true); setRevision(value => value + 1); }
      }
    } finally { locked.current = false; if (alive.current) setSaving(false); }
  }
  function prepare() {
    if (!mayPrepare || !listing) return;
    void submit({ kind: "prepare", body: { request_id: crypto.randomUUID(), expected_preparation_digest: listing.preparation_digest, acknowledge_unknown: unknown && acknowledgeUnknown } });
  }
  function control(action: ReviewExecutionAction) {
    if (!selected || !mayControl(action)) return;
    void submit({ kind: "control", id: selected.contract.execution_id, body: { request_id: crypto.randomUUID(), expected_sequence: selected.current.sequence, action } });
  }
  function dispatch() {
    if (!selected || !enabled || busy || selected.current.state !== "APPROVED") return;
    void submit({ kind: "dispatch", id: selected.contract.execution_id, body: { request_id: crypto.randomUUID(), expected_sequence: selected.current.sequence } });
  }
  return <section className="review-preparation-record" aria-label="고정 입력 검토 실행">
    <p>오프라인 합성 실행입니다. 실제 AI 모델·외부 전송을 사용하지 않으며 논문 전체 검토·과학적 타당성·사람의 채택을 입증하지 않습니다.</p>
    <p>요청 준비, 실행 승인, 시도 시작, 결과 수신, 결과 저장은 별도 단계입니다. 아래는 마지막 저장 상태이며 현재 작업자가 살아 있다는 증거는 아닙니다.</p>
    <V2Button disabled={busy} onClick={() => setRevision(value => value + 1)}>검토 실행 이력 새로고침</V2Button>
    {loading ? <p role="status">검토 실행 이력 확인 중</p> : readError ? <p role="alert">검토 실행 이력 조회 오류: {readError}</p> : null}
    {readable && !listing.enabled ? <p>이 환경에서는 새 실행·승인·시작이 비활성화되어 있습니다. 저장 이력 조회와 취소·만료 복구·수신 결과 저장은 가능합니다.{listing.reason ? ` (${listing.reason})` : ""}</p> : null}
    {readable ? <>
      {!listing.items.length ? <p>저장된 검토 실행 요청이 없습니다.</p> : <ul className="review-preparation-list">{listing.items.map(item => <li key={item.contract.execution_id}>
        <p>{date(item.contract.created_at)} · {labels[item.current.state]}</p>
        <V2Button disabled={busy} aria-pressed={selected?.contract.execution_id === item.contract.execution_id} onClick={() => void select(item.contract.execution_id)} aria-label={`검토 실행 기록 열기 · ${item.contract.execution_id}`}>검토 실행 기록 열기</V2Button>
        <small>{item.contract.execution_id}</small>
      </li>)}</ul>}
      <nav aria-label="검토 실행 이력 페이지"><V2Button disabled={busy || page <= 1} onClick={() => setPage(value => value - 1)}>이전 실행 기록 페이지</V2Button><span>{page} 페이지</span><V2Button disabled={busy || !listing.hasMore} onClick={() => setPage(value => value + 1)}>다음 실행 기록 페이지</V2Button></nav>
    </> : null}
    {selected ? <section aria-label="선택한 검토 실행 기록" style={{ overflowWrap: "anywhere", minWidth: 0 }}>
      <h4>선택한 검토 실행</h4><p>실행 기록: {selected.contract.execution_id}</p>
      <p role="status">마지막 저장 상태: {labels[selected.current.state]} · {date(selected.current.at)}</p>
      <p>요청 대상: {selected.contract.prepared.provider_name} · {selected.contract.prepared.model}</p>
      {selected.current.reason ? <p>기록 사유: {reasonText(selected.current.reason)}</p> : null}
      {selected.current.deadline ? <p>현재 단계 만료 시각: {date(selected.current.deadline)}. 시각 경과만으로 저장 상태가 바뀌거나 실행이 다시 시작되지 않습니다. 만료 후 새로고침해 복구를 선택하세요.</p> : null}
      {selected.current.outcome_unknown ? <p role="alert">이 시도의 결과가 불명확합니다. 자동 재실행하지 않습니다. 새 요청 준비는 별도 작업이며 이전 결과를 복구하지 않습니다.</p> : null}
      {selected.current.state === "READY" && enabled ? <V2Button disabled={!mayControl("approve")} busy={saving} onClick={() => control("approve")}>이 요청의 오프라인 실행 승인</V2Button> : null}
      {selected.current.state === "APPROVED" && enabled ? <V2Button disabled={busy} busy={saving} onClick={dispatch}>승인된 합성 검토 실행 시작</V2Button> : null}
      {selected.current.state === "RESULT_RECEIVED" ? <><p>이미 수신한 결과를 입력 기록에 저장합니다. 검토 모델을 다시 실행하지 않습니다.</p><V2Button disabled={!mayControl("save_result")} onClick={() => control("save_result")}>수신 결과 저장 다시 시도</V2Button></> : null}
      {activeStates.includes(selected.current.state) ? <V2Button disabled={!mayControl("cancel")} onClick={() => control("cancel")}>이 실행 요청 취소</V2Button> : null}
      {["APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED"].includes(selected.current.state) && enabled ? <V2Button disabled={!mayControl("revoke")} onClick={() => control("revoke")}>이 실행 승인 철회</V2Button> : null}
      {["APPROVED", "LEASED", "RUNNING"].includes(selected.current.state) && selected.current.deadline ? <V2Button disabled={!mayControl("recover") || Date.parse(selected.current.deadline) > Date.now()} onClick={() => control("recover")}>만료 상태 확인·복구</V2Button> : null}
      {activeStates.includes(selected.current.state) ? <p>취소·철회는 이 요청에 뒤늦게 도착한 결과가 저장되지 않도록 합니다. 이미 시작한 작업의 물리적 중단이나 비용 환불을 뜻하지 않습니다.</p> : null}
      {selected.current.receipt_id ? <><p>입력 기록: {selected.current.receipt_id}</p>{onOpenReceipt ? <V2Button disabled={busy} onClick={() => onOpenReceipt(selected.current.receipt_id!)}>저장된 검토 입력·결과 열기</V2Button> : null}</> : null}
      <details><summary>저장된 상태 변화와 실행 계약</summary><ol>{selected.events.map(event => <li key={event.sequence}>{event.sequence}. {labels[event.state]} · {date(event.at)}{event.reason ? ` · ${reasonText(event.reason)}` : ""}{event.outcome_unknown ? " · 결과 불명" : ""}</li>)}</ol><pre tabIndex={0}>{JSON.stringify(selected.contract, null, 2)}</pre></details>
    </section> : null}
    {error ? <p role="alert">검토 실행 요청 오류: {error}</p> : null}
    {pending && !saving ? <><p>응답을 확인하지 못했습니다. 같은 요청을 다시 보내면 동일한 요청 ID와 내용을 사용합니다. 화면을 나갔다 돌아오면 먼저 저장 이력을 확인하세요.</p><V2Button onClick={() => void submit(pending)}>같은 실행 요청 다시 보내기</V2Button></> : null}
    {enabled ? <section aria-label="새 검토 실행 요청">
      <h4>새 실행 요청</h4><p>기존 결과 확인과 새 시도는 별개입니다. 준비만으로 승인·실행되지는 않습니다.</p>
      {unknown ? <label htmlFor={checkboxId}><input style={{ display: "inline-block", width: "auto", marginRight: 8 }} type="checkbox" id={checkboxId} disabled={busy} checked={acknowledgeUnknown} onChange={event => setAcknowledgeUnknown(event.target.checked)}/>이전 결과가 불명확함을 확인했으며 별도의 새 요청을 준비합니다.</label> : null}
      {active ? <p>진행 중인 요청의 결과를 확인하거나 취소한 뒤 새 요청을 준비하세요.</p> : null}
      {page !== 1 ? <p>새 요청을 준비하려면 첫 페이지에서 최근 실행 상태를 확인하세요.</p> : null}
      <V2Button disabled={!mayPrepare} busy={saving} onClick={prepare}>새 합성 검토 실행 요청 준비</V2Button>
    </section> : null}
  </section>;
}

function Panel({ root, id, onOpenReceipt }: Props) {
  const [opened, setOpened] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>검토 실행 이력</summary>{opened ? <Workspace root={executionRoot(root, id)} onOpenReceipt={onOpenReceipt}/> : null}</details>;
}
export function ReviewExecution(props: Props) { return <Panel key={JSON.stringify([props.root, props.id])} {...props}/>; }
