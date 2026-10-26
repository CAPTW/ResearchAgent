import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { getModelConnections, saveModelConnections, type ModelConnections, type ModelConnectionsUpdate } from "../../lib/modelConnections";
import { V2Button, V2Panel } from "../V2Primitives";
import { ModelCredentialEditor } from "./ModelCredentialEditor";
import { AnthropicTrialPanel } from "./AnthropicTrialPanel";
import "./ModelConnectionsPanel.css";

type Draft = Pick<ModelConnectionsUpdate, "default_connector_id" | "models">;
function savedDraft(value: ModelConnections): Draft {
  if (!value || !Number.isSafeInteger(value.revision) || value.revision < 0 || value.execution_allowed !== false
      || !Array.isArray(value.connectors) || !value.connectors.length
      || !value.connectors.every(item => item && typeof item.connector_id === "string" && typeof item.display_name === "string"
        && typeof item.model === "string" && ["subscription_cli", "api_key"].includes(item.auth_mode))
      || !value.connectors.some(item => item.connector_id === value.default_connector_id)) throw new Error("invalid_model_connections");
  return { default_connector_id: value.default_connector_id, models: Object.fromEntries(value.connectors.map(item => [item.connector_id, item.model])) };
}
function documentationUrl(value: string) {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : null; } catch { return null; }
}
function saveFailure(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 409) return "다른 곳에서 설정이 변경되었습니다. 입력은 유지됩니다. 상태를 새로고침하고 내용을 확인한 뒤 다시 저장하세요.";
    if (error.status === 422) return "연결 또는 모델 값을 확인하세요. 입력한 값은 유지됩니다.";
    if (error.status === 403) return "관리자 권한이 없어 저장하지 못했습니다. 입력한 값은 유지됩니다.";
  }
  return "모델 연결 설정을 저장하지 못했습니다. 입력한 값은 유지됩니다. 상태를 확인하거나 같은 값으로 다시 저장하세요.";
}

export function ModelConnectionsPanel() {
  const id = useId();
  const [settings, setSettings] = useState<ModelConnections | null>(null);
  const [draft, setDraft] = useState<Draft>({ default_connector_id: "", models: {} });
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [readError, setReadError] = useState(""), [saveError, setSaveError] = useState(""), [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const alive = useRef(false), generation = useRef(0), controller = useRef<AbortController | null>(null);
  const dirty = useRef(false), saveLocked = useRef(false);

  async function reload() {
    if (saveLocked.current) return;
    const request = ++generation.current;
    controller.current?.abort();
    const active = new AbortController(); controller.current = active;
    setLoading(true); setReadError(""); setNotice("");
    try {
      const value = await getModelConnections(active.signal);
      const next = savedDraft(value);
      if (!alive.current || generation.current !== request) return;
      const preserve = dirty.current;
      setSettings(value);
      setDraft(current => preserve ? {
        default_connector_id: current.default_connector_id,
        models: Object.fromEntries(value.connectors.map(item => [item.connector_id, current.models[item.connector_id] ?? item.model])),
      } : next);
      setConflict(false); setSaveError("");
      if (preserve) setNotice("최신 저장 상태를 확인했습니다. 입력 중인 값은 유지했습니다. 내용을 확인한 뒤 다시 저장하세요.");
    } catch {
      if (alive.current && generation.current === request) setReadError("모델 연결 상태를 불러오지 못했습니다. 새로고침으로 다시 확인하세요. 입력한 값은 유지됩니다.");
    } finally {
      if (alive.current && generation.current === request) setLoading(false);
    }
  }
  useEffect(() => {
    alive.current = true; void reload();
    return () => { alive.current = false; generation.current++; controller.current?.abort(); };
  }, []);

  function edit(next: Draft) { dirty.current = true; setDraft(next); setNotice(""); }
  function restore() {
    if (!settings || saveLocked.current) return;
    dirty.current = false; setDraft(savedDraft(settings)); if (!conflict) setSaveError("");
    setNotice("입력을 마지막으로 확인한 저장값으로 되돌렸습니다.");
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings || loading || readError || conflict || saveLocked.current) return;
    const body: ModelConnectionsUpdate = { expected_revision: settings.revision, default_connector_id: draft.default_connector_id, models: { ...draft.models } };
    const request = ++generation.current;
    controller.current?.abort(); saveLocked.current = true;
    setSaving(true); setSaveError(""); setNotice("");
    try {
      const value = await saveModelConnections(body);
      const next = savedDraft(value);
      if (!alive.current || generation.current !== request) return;
      dirty.current = false; setSettings(value); setDraft(next); setConflict(false);
      setNotice("모델 연결 설정을 저장했습니다. 로그인·모델 실행·권한 승인은 시작하지 않았습니다.");
    } catch (error) {
      if (alive.current && generation.current === request) {
        setSaveError(saveFailure(error));
        if (error instanceof ApiError && error.status === 409) setConflict(true);
      }
    } finally {
      saveLocked.current = false;
      if (alive.current && generation.current === request) setSaving(false);
    }
  }
  const connectorName = (value: string) => settings?.connectors.find(item => item.connector_id === value)?.display_name ?? value;

  return <section className="model-connections-panel" aria-label="모델 연결 설정">
    <V2Panel title="모델 연결" subtitle="구독 CLI와 API 키 경로를 구분해 선택합니다.">
      <div className="model-connections-content">
        <p id={`${id}-scope`}>기본 연결과 모델 이름을 저장합니다. 저장은 로그인·모델 실행·실행 권한 승인이 아닙니다.</p>
        <p><strong>실행 권한: 미등록</strong> · 계정 로그인과 실제 실행 승인은 별도입니다.</p>
        <V2Button type="button" disabled={saving} onClick={() => void reload()}>모델 연결 상태 새로고침</V2Button>
        {loading ? <p role="status">모델 연결 상태 확인 중</p> : null}
        {readError ? <p role="alert">{readError}</p> : null}
        {settings ? <>
          <div className="model-connections-summary">
            <p>저장된 기본 연결: {connectorName(settings.default_connector_id)}</p>
            <p>{loading || readError ? "마지막 확인한 적용 연결" : "현재 적용 연결"}: {connectorName(settings.effective_connector_id)}</p>
            {settings.environment_override !== null ? <p>환경 변수 선택: {settings.environment_override} · 저장한 기본 연결보다 우선합니다.</p> : null}
          </div>
          <form aria-label="모델 연결 설정 저장" aria-describedby={`${id}-scope`} onSubmit={event => void save(event)}>
            <fieldset className="model-connections-fields" disabled={saving}>
              <label className="model-connections-choice" htmlFor={`${id}-default`}>기본 모델 연결
                <select id={`${id}-default`} value={draft.default_connector_id} onChange={event => edit({ ...draft, default_connector_id: event.target.value })}>
                  {settings.connectors.map(item => <option key={item.connector_id} value={item.connector_id}>{item.display_name}</option>)}
                </select>
              </label>
              <p id={`${id}-model-help`}>모델 이름은 미선택 상태로 저장할 수 있습니다. 실제 파이프라인 실행에는 모델 선택이 필요합니다. API 키나 로그인 토큰을 모델 칸에 입력하지 마세요.</p>
              <div className="model-connections-grid">{settings.connectors.map(item => {
                const docs = documentationUrl(item.docs_url);
                return <fieldset className="model-connection-card" key={item.connector_id}>
                  <legend>{item.display_name}</legend>
                  <p>{item.auth_mode === "subscription_cli" ? "구독 계정 · 공식 CLI 로그인" : "API 키 · 로컬 암호화 저장 또는 서버 환경 변수"}</p>
                  <p><strong>{item.adapter_available ? "연결 어댑터 지원" : "연결 어댑터 미구현"}</strong></p>
                  <p>{item.availability_reason}</p>
                  <label htmlFor={`${id}-${item.connector_id}`}>{item.display_name} 모델
                    <input id={`${id}-${item.connector_id}`} type="text" autoComplete="off" spellCheck={false} maxLength={200}
                      aria-describedby={`${id}-model-help`} value={draft.models[item.connector_id] ?? ""} placeholder="미선택"
                      onChange={event => edit({ ...draft, models: { ...draft.models, [item.connector_id]: event.target.value } })}/>
                  </label>
                  {item.connector_id === "anthropic_api" ? <V2Button type="button" onClick={() => edit({ ...draft, models: { ...draft.models, [item.connector_id]: "claude-opus-5-5" } })}>Opus 5.5 모델 입력</V2Button> : null}
                  {item.auth_mode === "api_key" ? <p>환경 변수 이름: <code>{item.credential_env || "미지정"}</code></p>
                    : <p>CLI 로그인 상태는 이 화면에서 확인하지 않습니다.</p>}
                  {item.cli_command ? <p>공식 CLI 안내: <code>{item.cli_command}</code></p> : null}
                  <p className="model-connections-instructions">{item.login_instructions}</p>
                  {docs ? <a href={docs} target="_blank" rel="noopener noreferrer">{item.display_name} 공식 안내</a> : null}
                </fieldset>;
              })}</div>
              <div className="model-connections-actions">
                <V2Button type="submit" className="is-primary" busy={saving} disabled={loading || !!readError || conflict}>모델 연결 설정 저장</V2Button>
                <V2Button type="button" onClick={restore}>저장된 모델 연결 값으로 되돌리기</V2Button>
              </div>
            </fieldset>
          </form>
          <div className="model-connections-grid">{settings.connectors.filter(item => item.auth_mode === "api_key").map(item =>
            <ModelCredentialEditor key={item.connector_id} connectorId={item.connector_id} displayName={item.display_name}/>)}</div>
          {settings.connectors.some(item => item.connector_id === "anthropic_api") ? <AnthropicTrialPanel/> : null}
        </> : null}
        {saveError ? <p role="alert">{saveError}</p> : null}
        {notice ? <p role="status">{notice}</p> : null}
      </div>
    </V2Panel>
  </section>;
}
