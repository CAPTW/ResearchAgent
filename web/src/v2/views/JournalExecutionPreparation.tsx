import { useEffect, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import { offlineJournalTarget, type ExecutionDetail, type OfflineCapability } from "../../lib/journalExecutions";

type Request = { request_id: string; expected_input_digest: string; provider_name: string; model: string; max_attempts: number; max_output_tokens: number };
type SelectedRequest = Request & { connector_id: string; expected_selection_digest: string };
type Pending = { url: string; body: Request | SelectedRequest };
type Connection = {
  connector_id: string | null; provider_name: string | null; model: string; display_name: string;
  auth_mode: "subscription_cli" | "api_key" | null; selection_digest: string;
  environment_override: boolean; ready: boolean; reason: "model_connection_model_required" | "model_connection_unavailable" | "model_connection_adapter_unavailable" | null;
  execution_allowed: false;
};

type Props = { root: string; inputDigest: string; onPrepared: (id: string) => void; offline?: OfflineCapability };
export function JournalExecutionPreparation(props: Props) {
  return <Preparation key={`${props.root}:${props.inputDigest}`} {...props}/>;
}
function Preparation({ root, inputDigest, onPrepared, offline }: Props) {
  const [expanded, setExpanded] = useState(false), [mode, setMode] = useState<"selected" | "manual">("selected");
  const [synthetic, setSynthetic] = useState(false);
  const [provider, setProvider] = useState("openai"), [model, setModel] = useState("");
  const [attempts, setAttempts] = useState("1"), [tokens, setTokens] = useState("4000");
  const [pending, setPending] = useState<Pending | null>(null), [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null), [connectionBusy, setConnectionBusy] = useState(false);
  const [connectionError, setConnectionError] = useState(""), [selectionChanged, setSelectionChanged] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const alive = useRef(true), locked = useRef(false), connectionLoaded = useRef(false), connectionLocked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (expanded && mode === "selected" && !synthetic && !connectionLoaded.current) void refreshConnection();
  }, [expanded, mode, synthetic]);
  async function refreshConnection() {
    if (locked.current || pending || connectionLocked.current) return;
    connectionLocked.current = true; connectionLoaded.current = true; setConnectionBusy(true); setConnectionError("");
    try {
      const value = await fetchJson<Connection>(`${root}/journal-connection`);
      if (alive.current) { setConnection(value); setSelectionChanged(false); if (selectionChanged) setError(""); }
    } catch (e) {
      if (alive.current) setConnectionError(`저장된 모델 연결을 확인하지 못했습니다: ${String(e)}. 연결을 다시 확인하거나 직접 입력 모드를 선택하세요.`);
    } finally { connectionLocked.current = false; if (alive.current) setConnectionBusy(false); }
  }
  const selectedReady = !connectionBusy && !connectionError && !selectionChanged && connection?.ready
    && !!connection.connector_id && !!connection.provider_name && !!connection.model.trim() && !!connection.selection_digest;
  const targetUnavailable = synthetic ? !offline?.enabled : mode === "selected" && !selectedReady;
  async function save() {
    if (locked.current || (!pending && targetUnavailable)) return;
    const selected = !synthetic && mode === "selected";
    const request = pending ?? {
      url: `${root}/journal-executions${selected ? "/selected-connection" : ""}`,
      body: {
        request_id: crypto.randomUUID(), expected_input_digest: inputDigest,
        provider_name: synthetic ? offlineJournalTarget.provider_name : selected ? connection!.provider_name! : provider.trim(),
        model: synthetic ? offlineJournalTarget.model : selected ? connection!.model : model.trim(),
        max_attempts: Number(attempts), max_output_tokens: Number(tokens),
        ...(selected ? { connector_id: connection!.connector_id!, expected_selection_digest: connection!.selection_digest } : {}),
      },
    };
    locked.current = true; setBusy(true); setPending(request); setError("");
    try {
      const result = await fetchJson<{ execution: ExecutionDetail; created: boolean }>(request.url, { method: "POST", body: JSON.stringify(request.body) });
      if (alive.current) {
        setPending(null);
        setNotice(result.created ? "생성 요청을 로컬에 준비했습니다. 외부 호출은 시작되지 않았습니다." : "이미 저장된 요청을 찾았습니다. 현재 이력을 확인하세요.");
        onPrepared(result.execution.contract.execution_id);
      }
    } catch (e) {
      if (alive.current) {
        if (e instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(e.status)) {
          setPending(null);
          if (e.status === 409 && e.message === "model_connection_selection_changed") {
            setSelectionChanged(true);
            setError("저장된 모델 연결이 변경되었습니다. 입력한 한도는 유지됩니다. 연결을 다시 확인한 뒤 새 대상을 확인하고 직접 준비 요청을 보내세요.");
          } else setError(`준비 요청이 거절되었습니다: ${String(e)}. 입력·권한과 고정 초안의 현재 상태를 확인하세요.`);
        } else {
          setError(`저장 응답을 확인하지 못했습니다: ${String(e)}. 중복 기록을 피하려면 같은 요청을 다시 보내세요. 화면을 떠나면 저장된 실행 목록을 먼저 확인하세요.`);
        }
      }
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  return <details className="local-journal-editor" open={expanded}><summary onClick={e => { e.preventDefault(); setExpanded(value => !value); }}>새 생성 요청 준비</summary>
    <p>현재 고정된 문헌·인용과 요청할 도구·모델, 한도를 변경할 수 없는 로컬 기록으로 남깁니다.</p>
    <p>도구·모델 이름은 요청 내용이며 로그인·모델 사용 가능 여부는 확인하지 않았습니다. 실행 환경과 실행 승인은 아직 연결되지 않았습니다. 실제 생성에는 별도의 실행 허용·비용 정책 연결이 필요합니다.</p>
    {notice ? <><p role="status">{notice}</p><button type="button" onClick={() => setNotice("")}>다른 요청 준비</button></> : <form aria-label="생성 요청 준비" onSubmit={e => { e.preventDefault(); void save(); }}>
      <fieldset disabled={busy || pending !== null}><legend>요청 내용</legend>
        {offline?.enabled || synthetic ? <label><input type="checkbox" checked={synthetic} onChange={e => setSynthetic(e.target.checked)}/>오프라인 합성 요청 준비</label> : null}
        {synthetic ? <p>외부 전송 없이 앱 연결을 시험하는 합성 결과입니다. 실제 AI 본문 검토가 아닙니다. 준비 후 승인과 실행을 각각 선택해야 합니다.</p> : null}
        {synthetic && !offline?.enabled ? <p role="status">오프라인 기능을 확인할 때까지 선택한 합성 대상과 입력을 유지합니다.</p> : null}
        {!synthetic ? <>
          <label><input type="radio" name="journal-preparation-source" checked={mode === "selected"} onChange={() => setMode("selected")}/>저장된 모델 연결 사용</label>
          <label><input type="radio" name="journal-preparation-source" checked={mode === "manual"} onChange={() => setMode("manual")}/>도구·모델 직접 입력</label>
        </> : null}
        {!synthetic && mode === "selected" ? <section aria-label="저장된 Journal 연결">
          {connectionBusy ? <p role="status">저장된 모델 연결 확인 중</p> : null}
          {connection ? <>
            <dl><dt>선택한 연결</dt><dd>{connection.display_name}</dd><dt>요청 도구</dt><dd>{connection.provider_name ?? "알 수 없음"}</dd><dt>요청 모델</dt><dd>{connection.model || "설정되지 않음"}</dd><dt>연결 방식</dt><dd>{connection.auth_mode === "subscription_cli" ? "구독 CLI" : connection.auth_mode === "api_key" ? "API 키" : "알 수 없음"}</dd></dl>
            {connection.environment_override ? <p>서버 환경 설정이 저장된 선택보다 우선합니다. 표시된 요청 대상이 현재 적용되는 연결입니다.</p> : null}
            {connection.reason === "model_connection_model_required" ? <p role="status">모델 이름을 설정에서 저장한 뒤 연결을 다시 확인하세요.</p> : null}
            {connection.reason === "model_connection_unavailable" ? <p role="status">저장된 연결을 사용할 수 없습니다. 설정의 모델 연결과 서버 환경 설정을 확인하세요.</p> : null}
            {connection.reason === "model_connection_adapter_unavailable" ? <p role="status">Journal 준비 어댑터가 연결되지 않았습니다. 설정에서 다른 연결을 선택하거나 직접 입력으로 요청만 보존할 수 있습니다.</p> : null}
          </> : null}
          {connectionError ? <p role="alert">{connectionError}</p> : null}
          <button type="button" disabled={connectionBusy || busy || pending !== null} onClick={() => { void refreshConnection(); }}>연결 다시 확인</button>
        </section> : <>
          <label>요청 도구 식별자<input value={synthetic ? offlineJournalTarget.provider_name : provider} readOnly={synthetic} required maxLength={100} pattern={"[a-z0-9_\\-]+"} onChange={e => setProvider(e.target.value)}/></label>
          <label>요청 모델 이름<input value={synthetic ? offlineJournalTarget.model : model} readOnly={synthetic} required maxLength={200} pattern=".*\S.*" onChange={e => setModel(e.target.value)}/></label>
        </>}
        <label>최대 시도 횟수<input type="number" value={attempts} required min={1} max={3} step={1} onChange={e => setAttempts(e.target.value)}/></label>
        <label>시도당 출력 토큰 상한<input type="number" value={tokens} required min={256} max={8000} step={1} onChange={e => setTokens(e.target.value)}/></label>
      </fieldset>
      <p>이 한도는 실행 허용이나 금액 예산이 아닙니다. 요청 준비는 토큰을 예약하거나 자동 재시도를 시작하지 않습니다.</p>
      <button type="submit" disabled={busy || (!pending && targetUnavailable)}>{busy ? "준비 요청 저장 중" : pending ? "같은 준비 요청 다시 보내기" : "로컬 생성 요청 준비"}</button>
    </form>}
    {error ? <p role="alert">{error}</p> : null}
  </details>;
}
