import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  approveOfflineTransport, controlTransportExecution, dispatchOfflineTransport, getTransportExecution,
  listTransportExecutions, prepareOfflineTransport, revokeOfflineTransport, transportRoot,
  type OfflineTransportApproval, type OfflineTransportCommand, type OfflineTransportPrepare,
  type TransportAuthority, type TransportControl, type TransportDetail, type TransportList, type TransportState,
} from "../../lib/reviewTransport";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { hasCodexV2Policy } from "../views/JournalAdapterManifest";
import { isCodexPaperV2 } from "../../lib/reviewCodexExecution";
import { ReviewCodexExecution } from "./ReviewCodexExecution";
import { isAnthropicStructuredOffline } from "../../lib/reviewAnthropicExecution";
import { ReviewAnthropicExecution } from "./ReviewAnthropicExecution";
import { isAnthropicNativeV2 } from "../../lib/reviewAnthropicNative";
import { ReviewAnthropicNative } from "./ReviewAnthropicNative";
import { PaperApproval } from "./PaperApproval";

type Props = { root: string; id: string; onOpenReceipt?: (receiptId: string) => void };
type Pending = { kind: "local"; id: string; body: TransportControl }
  | { kind: "offline_prepare"; body: OfflineTransportPrepare }
  | { kind: "offline_approve"; id: string; body: OfflineTransportApproval }
  | { kind: "offline_dispatch"; id: string; body: OfflineTransportCommand }
  | { kind: "offline_revoke"; id: string; body: OfflineTransportCommand };
const stateLabel: Record<TransportState, string> = {
  READY: "요청 준비됨", APPROVED: "승인 기록됨", LEASED: "실행 담당 할당", RUNNING: "시도 시작 기록됨",
  RESULT_RECEIVED: "결과 수신됨 · 입력 기록 저장 대기", RESULT_SAVED: "결과 저장됨",
  FAILED: "실패", INTERRUPTED: "중단됨", CANCELLED: "취소됨", REVOKED: "철회됨",
};
const authorityLabel: Record<TransportAuthority["validity"], string> = {
  NOT_YET_VALID: "승인 유효 기간 전", WITHIN_VALIDITY_WINDOW: "승인 유효 기간 내",
  EXPIRED: "승인 유효 기간 만료", REVOKED: "승인 철회 기록 있음",
};
const reasons: Record<string, string> = {
  provider_failed: "Provider가 정상 완료하지 못했다고 보고했습니다.",
  provider_incomplete: "Provider가 응답을 끝까지 완료하지 못했다고 보고했습니다. 검토 결과는 저장되지 않았습니다.",
  provider_cancelled: "Provider가 실행 취소를 보고했습니다. 사용자가 앱에서 취소한 기록과 구분됩니다.",
  provider_refused: "Provider가 이 검토 요청을 거절했습니다.",
  output_invalid: "수신한 응답을 유효한 검토 결과로 저장하지 못했습니다.",
  adapter_failed: "검토 연결 과정에서 정상 완료를 확인하지 못했습니다.",
  deadline_expired: "승인 또는 시도의 유효 시간이 지났습니다.",
  worker_stopped: "작업자가 종료되어 정상 완료를 확인하지 못했습니다.",
  worker_start_failed: "작업자를 시작하지 못했습니다.",
  result_invalid: "수신 결과가 저장 계약 검증을 통과하지 못했습니다.",
  authority_invalid: "이 요청의 승인 조건을 확인하지 못했습니다.",
};
const reasonText = (value: string) => Object.hasOwn(reasons, value) ? `${reasons[value]} (${value})` : value;
const errorText = (value: unknown) => value instanceof Error ? value.message : String(value);
const definitive = (value: unknown) => value instanceof ApiError && value.status >= 400 && value.status < 500 && ![408, 429].includes(value.status);
const cancellable: TransportState[] = ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED"];
const transportName = (mode: TransportDetail["contract"]["mode"] | undefined) =>
  mode === "OFFLINE_SYNTHETIC_TRANSPORT" ? "오프라인" : mode === "TRUSTED_PAPER_HTTP" ? "HTTP" : "CLI";
const openRecordLabel = (mode: TransportDetail["contract"]["mode"]) =>
  mode === "OFFLINE_SYNTHETIC_TRANSPORT" ? "오프라인 시험 기록 열기" : `${transportName(mode)} 실행 기록 열기`;
const switchLabel = (value: boolean | undefined) => value === true ? "사용" : value === false ? "사용 안 함" : "미기록";

function Workspace({ root, preparationId, onOpenReceipt }: { root: string; preparationId: string; onOpenReceipt?: Props["onOpenReceipt"] }) {
  const { preferences } = useOptionalAppearance();
  const unknownId = useId();
  const alive = useRef(true), locked = useRef(false), readVersion = useRef(0), selectedId = useRef<string | null>(null);
  const [listing, setListing] = useState<TransportList | null>(null), [selected, setSelected] = useState<TransportDetail | null>(null);
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [readError, setReadError] = useState(""), [error, setError] = useState(""), [pending, setPending] = useState<Pending | null>(null);
  const [acknowledgeUnknown, setAcknowledgeUnknown] = useState(false);
  const [codexBusy, setCodexBusy] = useState(false), [codexPending, setCodexPending] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(false), [approvalPending, setApprovalPending] = useState(false);
  const [anthropicBusy, setAnthropicBusy] = useState(false), [anthropicPending, setAnthropicPending] = useState(false);
  const [anthropicPendingId, setAnthropicPendingId] = useState<string | null>(null);
  const anthropicLock = useRef(false), anthropicPendingLock = useRef(false), anthropicPendingIdentity = useRef<string | null>(null);
  const [nativeBusy, setNativeBusy] = useState(false), [nativePending, setNativePending] = useState(false), [nativePendingId, setNativePendingId] = useState<string | null>(null);
  const nativeLock = useRef(false), nativePendingLock = useRef(false), nativePendingIdentity = useRef<string | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; readVersion.current += 1; }; }, []);
  useEffect(() => {
    const version = ++readVersion.current; setLoading(true); setReadError("");
    void listTransportExecutions(root, page).then(value => {
      if (!alive.current || version !== readVersion.current) return;
      setListing(value);
      const next = value.items.find(item => item.contract.execution_id === selectedId.current) ?? value.items[0] ?? null;
      setSelected(next); selectedId.current = next?.contract.execution_id ?? null; setLoading(false);
    }, reason => { if (alive.current && version === readVersion.current) { setReadError(errorText(reason)); setLoading(false); } });
  }, [root, page, revision]);
  const busy = loading || saving || codexBusy || approvalBusy || anthropicBusy || nativeBusy || pending !== null;
  const providerPending = anthropicPending || nativePending;
  const selectionBusy = busy || providerPending;
  const recoveryWhilePending = anthropicPending && anthropicPendingId !== null && anthropicPendingId === selected?.contract.execution_id
    || nativePending && nativePendingId !== null && nativePendingId === selected?.contract.execution_id;
  const readable = !!listing && !loading && !readError;
  const mayControl = (action: TransportControl["action"]) => readable && !busy && (!providerPending || recoveryWhilePending) && listing.capabilities[action];
  const date = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");
  const deadline = selected?.current.deadline ? Date.parse(selected.current.deadline) : NaN;
  const offlineAvailable = readable && listing.offline?.enabled === true && listing.offline.mode === "OFFLINE_SYNTHETIC_TRANSPORT";
  const offlineEligible = selected?.contract.mode === "OFFLINE_SYNTHETIC_TRANSPORT" && selected.offline_eligible === true && typeof selected.contract_sha256 === "string" && /^[a-f0-9]{64}$/.test(selected.contract_sha256);
  const mayOffline = offlineAvailable && offlineEligible && !busy && !providerPending && !codexPending && !approvalPending;
  const unknown = !!listing?.outcome_unknown_pending || !!selected?.current.outcome_unknown;
  const active = !!listing?.items.some(item => cancellable.includes(item.current.state)) || !!selected && cancellable.includes(selected.current.state);
  const mayPrepare = offlineAvailable && !busy && !providerPending && !codexPending && !approvalPending && page === 1 && !active && (!unknown || acknowledgeUnknown);

  function updated(value: TransportDetail) {
    setSelected(value); selectedId.current = value.contract.execution_id;
    setListing(previous => {
      if (!previous) return previous;
      const items = previous.items.some(item => item.contract.execution_id === value.contract.execution_id)
        ? previous.items.map(item => item.contract.execution_id === value.contract.execution_id ? value : item) : [value, ...previous.items];
      return { ...previous, outcome_unknown_pending: previous.outcome_unknown_pending || value.current.outcome_unknown,
        items: items.slice(0, previous.pageSize), hasMore: previous.hasMore || items.length > previous.pageSize };
    });
  }
  useEffect(() => {
    if (!readable || busy || providerPending && !recoveryWhilePending || !(isCodexPaperV2(selected) || isAnthropicStructuredOffline(selected) || isAnthropicNativeV2(selected)) || !selected || !["LEASED", "RUNNING", "RESULT_RECEIVED"].includes(selected.current.state)) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void getTransportExecution(root, selected.contract.execution_id).then(value => {
        if (!cancelled && alive.current) {
          if (value.contract.execution_id === selected.contract.execution_id) updated(value);
          else setReadError("요청한 실행과 다른 기록이 반환되었습니다. 같은 실행 이력을 다시 확인하세요.");
        }
      }, reason => { if (!cancelled && alive.current) setReadError(errorText(reason)); });
    }, 1000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [root, readable, busy, selected, providerPending, recoveryWhilePending]);

  async function select(id: string, refreshCurrent = false) {
    if (busy || anthropicLock.current || nativeLock.current
      || anthropicPendingLock.current && (!refreshCurrent || id !== selectedId.current || id !== anthropicPendingIdentity.current)
      || nativePendingLock.current && (!refreshCurrent || id !== selectedId.current || id !== nativePendingIdentity.current)) return;
    const version = ++readVersion.current; setLoading(true); setReadError("");
    try {
      const value = await getTransportExecution(root, id);
      if (value.contract.execution_id !== id) throw new Error("요청한 실행과 다른 기록이 반환되었습니다.");
      if (alive.current && version === readVersion.current) { setSelected(value); selectedId.current = id; }
    } catch (reason) { if (alive.current && version === readVersion.current) setReadError(errorText(reason)); }
    finally { if (alive.current && version === readVersion.current) setLoading(false); }
  }
  async function submit(command: Pending) {
    const exact = pending ?? command;
    if (locked.current || anthropicLock.current || nativeLock.current || anthropicPendingLock.current
      && !(exact.kind === "local" && exact.id === selectedId.current && exact.id === anthropicPendingIdentity.current)
      || nativePendingLock.current && !(exact.kind === "local" && exact.id === selectedId.current && exact.id === nativePendingIdentity.current)) return;
    locked.current = true; setSaving(true); setPending(exact); setError("");
    try {
      const value = exact.kind === "offline_prepare" ? await prepareOfflineTransport(root, exact.body)
        : exact.kind === "offline_approve" ? await approveOfflineTransport(root, exact.id, exact.body)
        : exact.kind === "offline_dispatch" ? await dispatchOfflineTransport(root, exact.id, exact.body)
        : exact.kind === "offline_revoke" ? await revokeOfflineTransport(root, exact.id, exact.body)
        : await controlTransportExecution(root, exact.id, exact.body);
      if (exact.kind === "local" && (value.contract.execution_id !== exact.id || !Number.isSafeInteger(value.current.sequence)
        || value.current.sequence < Math.max(exact.body.expected_sequence, selected?.contract.execution_id === exact.id ? selected.current.sequence : 0))) {
        throw new Error("관리 요청과 다른 실행 또는 이전 상태가 반환되었습니다. 같은 관리 요청을 다시 확인하세요.");
      }
      if (alive.current) {
        updated(value); setPending(null); setAcknowledgeUnknown(false);
      }
    } catch (reason) {
      if (alive.current) {
        setError(reason instanceof ApiError && reason.status === 409 && reason.message === "review_execution_changed"
          ? "저장 상태가 바뀌어 요청을 적용하지 않았습니다. 최신 이력을 확인한 뒤 작업을 다시 선택하세요."
          : errorText(reason));
        if (definitive(reason)) { setPending(null); setLoading(true); setRevision(value => value + 1); }
      }
    } finally { locked.current = false; if (alive.current) setSaving(false); }
  }
  function control(action: TransportControl["action"]) {
    if (!selected || !mayControl(action)) return;
    void submit({ kind: "local", id: selected.contract.execution_id, body: { request_id: crypto.randomUUID(), expected_sequence: selected.current.sequence, action } });
  }
  function prepareOffline() {
    if (!mayPrepare || !listing) return;
    void submit({ kind: "offline_prepare", body: { request_id: crypto.randomUUID(), expected_preparation_digest: listing.preparation_digest, acknowledge_unknown: unknown && acknowledgeUnknown } });
  }
  function approveOffline() {
    if (!mayOffline || !selected || selected.current.state !== "READY" || !selected.contract_sha256) return;
    void submit({ kind: "offline_approve", id: selected.contract.execution_id, body: {
      request_id: crypto.randomUUID(), expected_sequence: selected.current.sequence, expected_contract_sha256: selected.contract_sha256,
    } });
  }
  const mayDispatch = mayOffline && selected?.current.state === "APPROVED" && selected.authority?.validity === "WITHIN_VALIDITY_WINDOW" && Number.isFinite(deadline) && deadline > Date.now();
  const hasRevocableAuthority = !!selected?.authority && selected.authority.validity !== "REVOKED" && selected.authority.revoked_at === null;
  function offlineCommand(kind: "offline_dispatch" | "offline_revoke") {
    if (!mayOffline || !selected || (kind === "offline_dispatch" ? !mayDispatch : !hasRevocableAuthority)) return;
    void submit({ kind, id: selected.contract.execution_id, body: { request_id: crypto.randomUUID(), expected_sequence: selected.current.sequence } });
  }
  const preparation = selected?.contract.prepared.packet.source_preparation;
  const offlineRecord = selected?.contract.mode === "OFFLINE_SYNTHETIC_TRANSPORT";
  const httpRecord = selected?.contract.mode === "TRUSTED_PAPER_HTTP";
  const name = transportName(selected?.contract.mode);
  const manifest = selected?.contract.transport;
  const anthropicHttp = httpRecord && selected?.contract.prepared.provider_name === "anthropic"
    && (manifest?.schema_version === "ANTHROPIC_PREPARED_REVIEW_WIRE_V1" || manifest?.schema_version === "ANTHROPIC_PREPARED_REVIEW_WIRE_V2");
  const anthropicV2 = anthropicHttp && manifest?.schema_version === "ANTHROPIC_PREPARED_REVIEW_WIRE_V2";
  const codexV2 = selected?.contract.mode === "TRUSTED_PAPER_CLI" && selected.contract.prepared.provider_name === "codex_cli"
    && manifest?.schema_version === "CODEX_PREPARED_REVIEW_CLI_V2" && manifest.adapter_id === "xrml-codex-prepared-review"
    && manifest.adapter_version === "2" && hasCodexV2Policy(manifest);
  return <section className="review-preparation-record" aria-label="검토 실행 기록 확인">
    <p>저장된 실행 요청의 조건과 결과를 확인합니다. 조회·새로고침은 실행이나 자동 복구를 시작하지 않습니다.</p>
    <V2Button disabled={busy || providerPending && !recoveryWhilePending} onClick={() => {
      if (anthropicPendingLock.current || nativePendingLock.current) { if (selectedId.current) void select(selectedId.current, true); }
      else setRevision(value => value + 1);
    }}>검토 실행 이력 새로고침</V2Button>
    {loading ? <p role="status">검토 실행 이력 확인 중</p> : readError ? <p role="alert">검토 실행 이력 조회 오류: {readError}</p> : null}
    {readable ? <>
      <p>이력 조회는 새 실행이나 승인을 만들지 않습니다. Codex 연결 상태는 아래에서 별도로 확인하며, 이 화면에는 실제 실행 승인을 발급하는 기능이 없습니다.</p>
      {listing.offline && !listing.offline.enabled ? <p>기본 합성 연결 시험은 비활성화되어 있습니다. Provider별 합성 시험의 사용 가능 여부는 아래에서 별도로 확인합니다.{listing.offline.reason ? ` (${listing.offline.reason})` : ""}</p> : null}
      {listing.capabilities.reason !== "paper_review_execution_not_configured" ? <p>연결 상태: {listing.capabilities.reason}</p> : null}
      {listing.outcome_unknown_pending ? <p>이 입력의 합성·CLI·HTTP 이력에 결과가 불명확한 기록이 있습니다. 관련 실행 이력을 함께 확인하세요.</p> : null}
      {!listing.items.length ? <p>저장된 검토 실행 기록이 없습니다.</p> : <ul className="review-preparation-list">{listing.items.map(item => <li key={item.contract.execution_id}>
        <p>{date(item.contract.created_at)} · {item.contract.mode === "OFFLINE_SYNTHETIC_TRANSPORT" ? "오프라인 합성 시험 · " : item.contract.mode === "TRUSTED_PAPER_HTTP" ? "HTTP · " : ""}{stateLabel[item.current.state]}</p>
        <V2Button disabled={selectionBusy} aria-pressed={selected?.contract.execution_id === item.contract.execution_id} aria-label={`${openRecordLabel(item.contract.mode)} · ${item.contract.execution_id}`} onClick={() => void select(item.contract.execution_id)}>{openRecordLabel(item.contract.mode)}</V2Button>
        <small>{item.contract.execution_id}</small>
      </li>)}</ul>}
      <nav aria-label="검토 실행 이력 페이지"><V2Button disabled={selectionBusy || page <= 1} onClick={() => setPage(value => value - 1)}>이전 실행 기록 페이지</V2Button><span>{page} 페이지</span><V2Button disabled={selectionBusy || !listing.hasMore} onClick={() => setPage(value => value + 1)}>다음 실행 기록 페이지</V2Button></nav>
    </> : null}
    <ReviewCodexExecution root={root} preparationId={preparationId} listing={listing} selected={selected}
      disabled={loading || saving || approvalBusy || approvalPending || anthropicBusy || anthropicPending || nativeBusy || nativePending || pending !== null || !!readError} onBusy={setCodexBusy} onPending={setCodexPending} onUpdated={updated}/>
    <ReviewAnthropicExecution root={root} preparationId={preparationId} listing={listing} selected={selected}
      disabled={loading || saving || approvalBusy || approvalPending || codexBusy || codexPending || nativeBusy || nativePending || pending !== null || !!readError && !anthropicPending}
      onBusy={value => { anthropicLock.current = value; setAnthropicBusy(value); }} onPending={(value, identity) => {
        const id = value ? identity ?? null : null;
        anthropicPendingLock.current = value; anthropicPendingIdentity.current = id; setAnthropicPending(value); setAnthropicPendingId(id);
      }} onUpdated={updated}/>
    <ReviewAnthropicNative root={root} preparationId={preparationId} listing={listing} selected={selected}
      disabled={loading || saving || approvalBusy || approvalPending || codexBusy || codexPending || anthropicBusy || anthropicPending || pending !== null || !!readError && !nativePending}
      onBusy={value => { nativeLock.current = value; setNativeBusy(value); }} onPending={(value, identity) => {
        const id = value ? identity ?? null : null;
        nativePendingLock.current = value; nativePendingIdentity.current = id; setNativePending(value); setNativePendingId(id);
      }} onUpdated={updated}/>
    {selected && isCodexPaperV2(selected) ? <PaperApproval root={root} selected={selected}
      disabled={loading || saving || codexBusy || codexPending || anthropicBusy || anthropicPending || nativeBusy || nativePending || pending !== null || !!readError}
      onBusy={setApprovalBusy} onPending={setApprovalPending} onUpdated={updated}/> : null}
    {selected && preparation ? <section aria-label={offlineRecord ? "선택한 오프라인 시험 기록" : `선택한 ${name} 실행 기록`} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
      <h4>{offlineRecord ? "선택한 오프라인 연결 시험" : `선택한 ${name} 실행`}</h4><p>실행 기록: {selected.contract.execution_id}</p>
      <p role="status">마지막 저장 상태: {stateLabel[selected.current.state]} · {date(selected.current.at)}</p>
      <p>저장된 시도 상태이며 현재 작업자가 실행 중이라는 증거는 아닙니다.</p>
      <p>{selected.contract.input_origin === "FIXTURE_SYNTHETIC" ? "합성 시험 자료 (FIXTURE_SYNTHETIC)" : "Provider 결과를 위한 요청 (PROVIDER_RESPONSE) · 응답 수신 여부는 마지막 저장 상태에서 확인하세요"}. 실제 계정·모델 실행이나 과학적 타당성·논문 전체 검토가 검증됐다는 뜻은 아닙니다.</p>
      {selected.contract.mode === "OFFLINE_SYNTHETIC_TRANSPORT" ? <p>고정된 합성 결과를 사용하는 오프라인 시험 기록입니다. 아래 Provider·모델 표시는 합성 시험용 식별자이며 실제 CLI 실행 기록을 뜻하지 않습니다.</p> : null}
      <p>고정 입력: {preparation.selection.start}–{preparation.selection.end} · {preparation.selection.chars}자 (Unicode 코드 포인트). 정규화된 저장 본문의 선택 범위입니다.</p>
      <p>출처: {preparation.source.run_id} · {preparation.source.paper_id}</p>
      <details><summary>{name} 고정 입력 본문 확인</summary><pre tabIndex={0} aria-label={`${name} 고정 입력 본문`}>{selected.contract.prepared.packet.evidence_spans.map(span => span.text).join("\n")}</pre></details>
      <p>요청 모델: {selected.contract.prepared.model} · Provider: {selected.contract.prepared.provider_name}</p>
      {codexV2 ? <>
        <h5>Codex CLI V2 프로필</h5>
        <p>공식 CLI가 관리하는 ChatGPT 로그인을 요구합니다. 실제 로그인·계정·구독 과금 경로는 확인되지 않았습니다.</p>
        <p>요청 재시도 0회 · 스트림 재시도 0회 · WebSocket 사용 안 함 요청. XRML은 프로세스를 자동 재실행하지 않습니다.</p>
        <p>실제 내부 재시도·외부 호출 횟수·도구 격리·비용 상한은 검증되지 않았습니다. 이 기록은 실행 승인이 아닙니다.</p>
      </> : null}
      {offlineRecord ? <p>실행 방식: 앱 안에서 고정 합성 응답을 생성합니다. CLI·별도 프로세스·외부 모델 호출은 없습니다. 모델 추론 설정은 적용되지 않습니다.</p> : <>
        {httpRecord ? <>
          <p>요청 대상: {manifest?.endpoint ?? "미기록"}</p>
          <p>HTTP 제한 시간: {manifest?.timeout_seconds === undefined ? "미기록" : `${manifest.timeout_seconds}초`}</p>
          <p>요청 본문: {manifest?.body_bytes ?? "미기록"}바이트 · HTTP 응답 읽기 한도: {manifest?.max_response_bytes ?? "미기록"}바이트 · 검토 결과 한도: {manifest?.max_review_output_bytes ?? "미기록"}바이트</p>
          {anthropicHttp ? <>
            <p>Anthropic Messages API · API 버전: {typeof manifest?.api_version === "string" ? manifest.api_version : "미기록"} · 스트리밍 {switchLabel(manifest?.stream)}</p>
            {anthropicV2 && manifest?.execution_surface === "MOCK_TRANSPORT" ? <p>MockTransport 합성 응답 기록입니다. 실제 네트워크·API 키·Anthropic 모델을 사용하지 않았습니다.</p>
              : <p>Claude 구독 로그인과 별도인 API 키 경로입니다. 이 기록으로 API 계정·인증·과금 경로가 확인되지는 않습니다.</p>}
            {anthropicV2 ? <><p>Messages V2 구조화 계약 · 스키마 컴파일러: {typeof manifest?.schema_compiler === "string" ? manifest.schema_compiler : "미기록"}.</p>
              <p>{manifest?.shared_schema_context === "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM" ? "원래 로컬 스키마를 system 문맥에도 포함하며 응답은 원래 로컬 계약으로 다시 검사합니다." : "system 스키마 문맥: 미확인"}</p>
              <p>유효 system SHA-256: {typeof manifest?.effective_system_sha256 === "string" ? manifest.effective_system_sha256 : "미기록"}</p>
              <p>요청 본문 SHA-256: {typeof manifest?.body_sha256 === "string" ? manifest.body_sha256 : "미기록"}</p>
              <p>HTTP 제한 시간은 개별 작업의 비활성 제한이며 총 실행 시간 상한이 아닙니다. {manifest?.execution_surface === "MOCK_TRANSPORT" ? "이 합성 시험은 실제 HTTP 통신을 하지 않습니다." : "저장된 설정만으로 실제 완료 시간은 확인할 수 없습니다."}</p></> : null}
          </> : <p>요청 설정: 응답 저장 {switchLabel(manifest?.store)} · 백그라운드 {switchLabel(manifest?.background)} · 스트리밍 {switchLabel(manifest?.stream)} · 입력 자동 잘라내기 {manifest?.truncation === "disabled" ? "사용 안 함" : manifest?.truncation ?? "미기록"}</p>}
          <p>로컬 전송 설정: 리다이렉트 {switchLabel(manifest?.redirects)} · 환경 프록시 {switchLabel(manifest?.environment_proxy)} · 자동 재시도 {switchLabel(manifest?.automatic_retry)}</p>
        </> : <p>요청 reasoning: {manifest?.reasoning_effort ?? "미기록"} · 제한 시간: {manifest?.timeout_seconds === undefined ? "미기록" : `${manifest.timeout_seconds}초`}</p>}
        {anthropicHttp ? <>
          <p>요청 출력 토큰 {selected.contract.prepared.provider_request.max_output_tokens}.
            {!selected.contract.transport.output_token_limit_enforced ? " 요청 상한의 실제 강제 여부가 보장되지 않습니다." : null}</p>
          <p>고정 요청 온도: {selected.contract.prepared.provider_request.temperature} · {manifest?.temperature_policy === "OMITTED_PROVIDER_DEFAULT" ? "전송하지 않으며 Provider 기본값을 사용합니다." : "전송 정책 미기록"}</p>
        </> : <p>요청 출력 토큰 {selected.contract.prepared.provider_request.max_output_tokens} · temperature {selected.contract.prepared.provider_request.temperature}.
          {!selected.contract.transport.output_token_limit_enforced || !selected.contract.transport.temperature_enforced ? " 요청값의 실제 강제 여부가 보장되지 않습니다." : null}</p>}
        {httpRecord ? <p>저장된 HTTP 요청 조건입니다. 서버의 실제 적용·실제 모델·계정·비용은 독립적으로 확인되지 않았습니다.</p>
          : <p>요청한 모델과 도구 제한의 실제 적용은 독립적으로 확인되지 않았습니다.</p>}
      </>}
      {selected.authority ? <div>
        <p>승인 참조: {selected.authority.authorization_id}</p><p>{authorityLabel[selected.authority.validity]}</p>
        <p>발급 주체: {selected.authority.issuer_id} · 정책: {selected.authority.policy_version}</p>
        <p>승인 기간: {date(selected.authority.granted_at)}–{date(selected.authority.expires_at)}</p>
        {selected.authority.revoked_at ? <p>철회 기록 시각: {date(selected.authority.revoked_at)}</p> : null}
        <p>시간상 유효 여부와 저장된 승인 참조이며 현재 실행 가능 여부나 실제 인증을 보증하지 않습니다.</p>
      </div> : <p>연결된 승인 기록이 없습니다.</p>}
      {selected.current.reason ? <p>기록 사유: {reasonText(selected.current.reason)}</p> : null}
      {selected.current.outcome_unknown ? <p role="alert">이 시도의 결과가 불명확합니다. 자동으로 다시 실행하지 않습니다. 저장 이력을 확인하세요.</p> : null}
      {selected.current.deadline ? <p>현재 단계 만료 시각: {date(selected.current.deadline)}. 만료 후 새로고침하고 필요한 경우 복구를 선택하세요.</p> : null}
      {offlineAvailable && !offlineEligible ? <p>이 기록은 현재 오프라인 시험 대상으로 확인되지 않아 새 승인·시작을 제공하지 않습니다. 기존 기록 확인과 허용된 로컬 복구는 가능합니다.</p> : null}
      {offlineAvailable && offlineEligible ? <div>
        {selected.current.state === "READY" ? <><p>이 고정 입력과 요청 조건에 대해 오프라인 시험만 승인합니다. 승인과 시작은 별도 단계입니다.</p><V2Button disabled={!mayOffline} busy={saving} onClick={approveOffline}>이 고정 요청의 오프라인 시험 승인</V2Button></> : null}
        {selected.current.state === "APPROVED" ? <V2Button disabled={!mayDispatch} busy={saving} onClick={() => offlineCommand("offline_dispatch")}>승인된 오프라인 연결 시험 시작</V2Button> : null}
        {hasRevocableAuthority ? <><V2Button disabled={!mayOffline} onClick={() => offlineCommand("offline_revoke")}>이 오프라인 시험 승인 철회</V2Button><p>철회해도 이미 저장된 결과는 유지됩니다. 승인을 철회하는 작업이며 실행 중인 작업의 물리적 중단을 뜻하지 않습니다.</p></> : null}
      </div> : null}
      {selected.current.state === "RESULT_RECEIVED" ? <><p>이미 수신한 결과를 저장합니다. Provider를 다시 호출하지 않습니다.</p><V2Button disabled={!mayControl("save_result")} busy={saving} onClick={() => control("save_result")}>{name} 수신 결과 저장 다시 시도</V2Button></> : null}
      {cancellable.includes(selected.current.state) ? <><V2Button disabled={!mayControl("cancel")} onClick={() => control("cancel")}>이 {name} 요청 취소</V2Button><p>취소는 이 요청에 뒤늦게 도착한 결과가 저장되지 않도록 합니다. 이미 시작된 작업의 물리적 중단이나 비용 환불을 뜻하지 않습니다.</p></> : null}
      {["APPROVED", "LEASED", "RUNNING"].includes(selected.current.state) && selected.current.deadline ? <V2Button disabled={!mayControl("recover") || !Number.isFinite(deadline) || deadline > Date.now()} onClick={() => control("recover")}>{name} 만료 상태 확인·복구</V2Button> : null}
      {selected.current.receipt_id ? <><p>입력 기록: {selected.current.receipt_id}</p>{onOpenReceipt ? <V2Button disabled={selectionBusy} onClick={() => onOpenReceipt(selected.current.receipt_id!)}>저장된 {name} 입력·결과 열기</V2Button> : null}</> : null}
      <details><summary>{name} 고정 계약·상태 원문</summary><ol>{selected.events.map(event => <li key={event.sequence}>{event.sequence}. {stateLabel[event.state]} · {date(event.at)}{event.reason ? ` · ${reasonText(event.reason)}` : ""}{event.outcome_unknown ? " · 결과 불명" : ""}</li>)}</ol><pre tabIndex={0} aria-label={`보존된 ${name} 실행 원문`}>{JSON.stringify({ contract: selected.contract, events: selected.events }, null, 2)}</pre></details>
    </section> : null}
    {error ? <p role="alert">검토 관리 요청 오류: {error}</p> : null}
    {pending && !saving ? <><p>관리 요청의 응답을 확인하지 못했습니다. 재시도는 같은 요청 ID와 내용을 사용합니다. 화면을 떠난 뒤에는 먼저 저장 이력을 확인하세요.</p><V2Button onClick={() => void submit(pending)}>같은 {pending.kind !== "local" ? "오프라인" : name} 관리 요청 다시 보내기</V2Button></> : null}
    {offlineAvailable ? <section aria-label="오프라인 연결 시험 준비">
      <h4>개발용 오프라인 연결 시험</h4>
      <p>고정된 합성 결과로 앱 연결을 시험합니다. CLI·별도 프로세스·외부 전송·실제 AI 모델을 실행하지 않습니다. 연구 품질과 논문 전체 검토는 미평가입니다.</p>
      <p>합성 시험 모델: {listing.offline!.model} · 시험 발급 주체: {listing.offline!.issuer_id}</p>
      <p>시험 정책: {listing.offline!.policy_version}. 실제 실행 권한을 발급하지 않습니다.</p>
      <p>새 요청 준비·오프라인 승인·시작은 별도 단계입니다. 기존 합성·CLI·HTTP 시도의 상태를 확인한 뒤 새 요청을 준비하세요.</p>
      {unknown ? <label htmlFor={unknownId}><input id={unknownId} type="checkbox" style={{ display: "inline-block", width: "auto", marginRight: 8 }} checked={acknowledgeUnknown} disabled={busy} onChange={event => setAcknowledgeUnknown(event.target.checked)}/>이전 결과가 불명확함을 확인했으며 별도의 오프라인 시험 요청을 준비합니다.</label> : null}
      {active ? <p>기존 요청의 결과를 확인하거나 취소한 뒤 새 요청을 준비하세요.</p> : null}
      {page !== 1 ? <p>첫 페이지에서 최근 실행 상태를 확인한 뒤 새 요청을 준비하세요.</p> : null}
      <V2Button disabled={!mayPrepare} busy={saving} onClick={prepareOffline}>새 오프라인 연결 시험 요청 준비</V2Button>
    </section> : null}
  </section>;
}
function Panel({ root, id, onOpenReceipt }: Props) {
  const [opened, setOpened] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>검토 실행 조건·이력</summary>{opened ? <Workspace root={transportRoot(root, id)} preparationId={id} onOpenReceipt={onOpenReceipt}/> : null}</details>;
}
export function ReviewTransport(props: Props) { return <Panel key={JSON.stringify([props.root, props.id])} {...props}/>; }
