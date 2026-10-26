import { useEffect, useId, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import { executionStates, type CodexDispatchRequest, type CodexDispatchStatus, type ExecutionDetail } from "../../lib/journalExecutions";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { JournalAdapterManifest, readJournalAdapterManifest } from "./JournalAdapterManifest";

type Props = { url: string; saved: ExecutionDetail; onUpdated: (execution: ExecutionDetail) => void;
  onBusy: (busy: boolean) => void; onPending?: (pending: boolean) => void; disabled?: boolean };
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const timestamp = (value: unknown): value is string => typeof value === "string" && Number.isFinite(Date.parse(value));
const authorityLabels = { AVAILABLE: "현재 사용 가능", EXPIRED: "기한 만료", REVOKED: "철회됨", MISMATCH: "현재 실행 조건과 불일치", BUDGET_EXHAUSTED: "허용된 시도·예약 한도 소진" };

function readStatus(value: unknown, saved: ExecutionDetail): CodexDispatchStatus | null {
  if (!record(value) || value.schema_version !== "CODEX_JOURNAL_DISPATCH_STATUS_V1" || value.profile !== "CODEX_JOURNAL_CLI_V2"
    || value.execution_id !== saved.contract.execution_id || value.actor_id !== saved.contract.actor_id
    || value.observed_sequence !== saved.current.sequence || !hash(value.contract_digest) || typeof value.enabled !== "boolean"
    || (value.reason !== null && !text(value.reason)) || typeof value.authorities_has_more !== "boolean"
    || !Array.isArray(value.authorities) || value.authorities.length > 100) return null;
  const ids = new Set<string>();
  for (const item of value.authorities) {
    if (!record(item) || !text(item.authorization_id) || item.authorization_id.length > 100 || ids.has(item.authorization_id)
      || !hash(item.record_digest) || !text(item.status) || !Object.hasOwn(authorityLabels, item.status)
      || !timestamp(item.expires_at) || !text(item.issuer) || !text(item.policy_version)) return null;
    ids.add(item.authorization_id);
  }
  if (value.manifest === null && value.binding === null) return value.enabled ? null : value as CodexDispatchStatus;
  const manifest = readJournalAdapterManifest(value.manifest), binding = value.binding;
  if (!manifest || manifest.schema_version !== "CODEX_JOURNAL_CLI_V2" || manifest.contract_digest !== value.contract_digest
    || manifest.model !== saved.contract.specification.model || manifest.argv_shape[0] !== "{explicit_launcher}" || !record(binding)
    || binding.surface_id !== "codex_cli" || binding.adapter_id !== "xrml-codex-frozen-journal" || binding.adapter_version !== "2"
    || !hash(binding.command_shape_sha256)) return null;
  return value as CodexDispatchStatus;
}

function readExecution(value: unknown, saved: ExecutionDetail, request: CodexDispatchRequest): ExecutionDetail | null {
  if (!record(value) || !record(value.execution)) return null;
  const result = value.execution, contract = result.contract;
  if (!record(contract) || !record(contract.specification) || !record(contract.packet)
    || contract.execution_id !== saved.contract.execution_id || contract.actor_id !== saved.contract.actor_id
    || contract.created_at !== saved.contract.created_at || contract.packet.packet_id !== saved.contract.packet.packet_id
    || !record(result.current) || !Array.isArray(result.events)) return null;
  const specification = contract.specification;
  if (Object.entries(saved.contract.specification).some(([key, item]) => specification[key] !== item)) return null;
  for (const event of [result.current, ...result.events]) {
    if (!record(event) || !Number.isInteger(event.sequence) || Number(event.sequence) < 0 || Number(event.sequence) > 100
      || !text(event.state) || !Object.hasOwn(executionStates, event.state)
      || !Number.isInteger(event.attempt) || Number(event.attempt) < 0 || Number(event.attempt) > saved.contract.specification.max_attempts
      || typeof event.outcome_unknown !== "boolean" || (event.reason !== null && !text(event.reason))
      || (event.at !== null && !timestamp(event.at)) || (event.deadline !== null && !timestamp(event.deadline))) return null;
  }
  const receiptEvent = result.events.find(event => record(event) && record(event.dispatch) && record(event.dispatch.request)
    && event.dispatch.request.request_id === request.request_id);
  const receipt = record(receiptEvent) && record(receiptEvent.dispatch) ? receiptEvent.dispatch : null;
  if (!receipt || receipt.schema_version !== "JOURNAL_DISPATCH_RECEIPT_V2" || receipt.actor_id !== saved.contract.actor_id
    || !record(receipt.request) || receipt.request.expected_sequence !== request.expected_sequence
    || receipt.request.acknowledge_unknown !== request.acknowledge_unknown || !record(receipt.authority)
    || receipt.authority.authorization_id !== request.authorization_id || receipt.authority.record_digest !== request.expected_authority_digest
    || !record(receipt.binding) || receipt.binding.command_shape_sha256 !== request.expected_binding_digest
    || receipt.binding.surface_id !== "codex_cli" || receipt.binding.adapter_id !== "xrml-codex-frozen-journal" || receipt.binding.adapter_version !== "2") return null;
  return Number(result.current.sequence) >= saved.current.sequence ? result as ExecutionDetail : null;
}

function unavailable(reason: string | null): string {
  if (reason === "journal_codex_dispatch_disabled") return "이 서버에서는 Codex 실행이 비활성화되어 있습니다. 실행 권한을 발급하거나 계정을 연결하는 화면이 아닙니다.";
  if (reason === "journal_dispatch_closed") return "서버 실행 처리가 종료되었습니다. 저장된 이력을 확인하세요.";
  return "현재 서버 실행 조건을 사용할 수 없습니다. 이력과 서버 설정을 확인한 뒤 다시 조회하세요.";
}

export function JournalCodexDispatch(props: Props) {
  return props.saved.contract.specification.provider_name === "codex_cli" ? <Workspace key={props.url} {...props}/> : null;
}

function Workspace({ url, saved, onUpdated, onBusy, onPending, disabled = false }: Props) {
  const { preferences } = useOptionalAppearance();
  const selectId = useId(), acknowledgementId = useId();
  const [status, setStatus] = useState<CodexDispatchStatus | null>(null), [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [acknowledged, acknowledge] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [pending, setPending] = useState<CodexDispatchRequest | null>(null);
  const alive = useRef(true), locked = useRef(false), current = useRef(saved);
  const busyCallback = useRef(onBusy); current.current = saved; busyCallback.current = onBusy;
  const pendingCallback = useRef(onPending); pendingCallback.current = onPending;
  useEffect(() => { alive.current = true; return () => { alive.current = false; busyCallback.current(false); pendingCallback.current?.(false); }; }, []);
  useEffect(() => { setSelected(""); acknowledge(false); }, [saved.current.sequence]);
  const currentStatus = status?.observed_sequence === saved.current.sequence ? status : null;
  const available = currentStatus?.authorities.find(item => item.authorization_id === selected && item.status === "AVAILABLE");
  const canAttempt = ["READY", "FAILED", "INTERRUPTED"].includes(saved.current.state)
    && saved.current.attempt < saved.contract.specification.max_attempts;
  const canStart = !disabled && !loading && !busy && !pending && canAttempt && currentStatus?.enabled && currentStatus.reason === null
    && currentStatus.binding && available && (!saved.current.outcome_unknown || acknowledged);
  async function inspect() {
    if (locked.current || disabled) return;
    locked.current = true; setLoading(true); setStatus(null); setSelected(""); acknowledge(false); setError(""); setNotice("");
    try {
      const response = await fetchJson<unknown>(`${url}/codex-dispatch`);
      if (!alive.current) return;
      const value = readStatus(response, current.current);
      if (!value) throw new Error("invalid status");
      setStatus(value);
    } catch { if (alive.current) setError("서버 실행 허용을 확인하지 못했습니다. 실행 이력을 새로고침한 뒤 다시 확인하세요."); }
    finally { locked.current = false; if (alive.current) setLoading(false); }
  }
  async function dispatch() {
    if (locked.current || disabled || (!pending && !canStart)) return;
    const request = pending ?? { request_id: crypto.randomUUID(), expected_sequence: saved.current.sequence, acknowledge_unknown: acknowledged,
      authorization_id: available!.authorization_id, expected_authority_digest: available!.record_digest,
      expected_binding_digest: currentStatus!.binding!.command_shape_sha256 };
    locked.current = true; setBusy(true); busyCallback.current(true); setPending(request); pendingCallback.current?.(true); setError(""); setNotice("");
    try {
      const response = await fetchJson<unknown>(`${url}/codex-dispatch`, { method: "POST", body: JSON.stringify(request) });
      if (!alive.current) return;
      const execution = readExecution(response, current.current, request);
      if (!execution) throw new Error("invalid execution response");
      setPending(null); pendingCallback.current?.(false); setStatus(null); setSelected(""); acknowledge(false); onUpdated(execution);
      setNotice("실행 요청의 저장 상태를 확인했습니다. 이력 조회는 새 실행을 시작하지 않으며 결과를 자동 채택하지 않습니다.");
    } catch (e) {
      if (alive.current) {
        const rejected = e instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(e.status);
        if (rejected) { setPending(null); pendingCallback.current?.(false); setStatus(null); setSelected(""); acknowledge(false); }
        setError(rejected ? "Codex 실행 요청이 거절되었습니다. 실행 이력과 서버 실행 허용을 다시 확인하세요."
          : "Codex 실행 응답을 확인하지 못했습니다. 같은 요청을 다시 보내면 저장된 처리 결과를 확인합니다. 새 실행 요청을 만들지 않습니다.");
      }
    } finally { locked.current = false; if (alive.current) { setBusy(false); busyCallback.current(false); } }
  }
  const stamp = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");
  return <section aria-label="서버 허용 Codex 실행" className="local-journal-editor">
    <h5>서버 허용 Codex 실행</h5>
    <p>서버에 이미 등록된 실행 허용으로 고정된 입력을 수동 전송합니다. 실제 계정·모델·과금 경로를 검증했다는 뜻은 아닙니다. 이 화면에서 실행 권한을 발급하지 않습니다.</p>
    <button type="button" disabled={disabled || busy || loading} onClick={() => { void inspect(); }}>Codex 실행 허용 확인·새로고침</button>
    {loading ? <p role="status">서버 실행 허용 확인 중</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {status && !currentStatus ? <p>실행 상태가 바뀌었습니다. 새 실행 전 서버 실행 허용을 다시 확인하세요.</p> : null}
    {!disabled && currentStatus ? <>
      {!currentStatus.enabled || currentStatus.reason !== null ? <p role="status">{unavailable(currentStatus.reason)}</p> : null}
      {currentStatus.manifest ? <><p>현재 서버가 실행에 사용할 전송 조건입니다. 과거 시도의 기록과 구분하여 확인하세요.</p>
        <JournalAdapterManifest value={currentStatus.manifest} context="preview"/>
        <p className="local-report-id">서버 실행 조건 SHA-256: {currentStatus.binding?.command_shape_sha256}</p></> : null}
      {!currentStatus.authorities.length ? <p>이 요청에 등록된 실행 허용이 없습니다.</p> : <ul aria-label="등록된 Codex 실행 허용">{currentStatus.authorities.map(item => <li key={item.authorization_id}>
        <strong>{authorityLabels[item.status]}</strong><p className="local-report-id">허용: {item.authorization_id}</p>
        <p>등록 주체: {item.issuer} · 정책: {item.policy_version} · 만료: {stamp(item.expires_at)}</p>
        <p className="local-report-id">허용 기록 SHA-256: {item.record_digest}</p>
      </li>)}</ul>}
      {currentStatus.authorities_has_more ? <p>등록된 허용 중 일부만 표시합니다. 원하는 허용이 보이지 않으면 서버 운영자가 확인해야 합니다.</p> : null}
      <label htmlFor={selectId}>서버 실행 허용 선택</label>
      <select id={selectId} value={selected} disabled={disabled || busy || loading || pending !== null || !currentStatus.enabled || currentStatus.reason !== null}
        onChange={e => setSelected(e.target.value)} style={{ maxWidth: "100%" }}>
        <option value="">실행 허용을 선택하세요</option>
        {currentStatus.authorities.map(item => <option key={item.authorization_id} value={item.authorization_id} disabled={item.status !== "AVAILABLE"}>{item.authorization_id}</option>)}
      </select>
      {saved.current.outcome_unknown && canAttempt ? <label htmlFor={acknowledgementId} style={{ display: "block" }}>
        <input id={acknowledgementId} type="checkbox" checked={acknowledged} disabled={disabled || busy || loading || pending !== null} onChange={e => acknowledge(e.target.checked)}/>
        이전 시도의 결과가 불명확하며 새 시도가 중복 호출·비용을 만들 수 있음을 확인했습니다.
      </label> : null}
      {!canAttempt ? <p>현재 상태 또는 최대 시도 수 때문에 새 실행을 요청할 수 없습니다.</p> : null}
      <button type="button" disabled={!canStart} onClick={() => { void dispatch(); }}>선택한 허용으로 Codex 실행 요청</button>
    </> : null}
    {pending ? <><p>응답 확인이 끝나지 않은 요청을 보존했습니다. 화면을 떠나면 저장 이력부터 확인하세요.</p>
      <button type="button" disabled={disabled || busy || loading} onClick={() => { void dispatch(); }}>같은 Codex 실행 요청 다시 보내기</button></> : null}
    {busy ? <p role="status">Codex 실행 요청의 저장 상태 확인 중</p> : null}
  </section>;
}
