import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { deleteModelCredential, getModelCredential, saveModelCredential, type ModelCredentialStatus } from "../../lib/modelConnections";
import { V2Button } from "../V2Primitives";
import "./ModelCredentialEditor.css";

type Props = { connectorId: string; displayName: string };

// A connector change drops the entire secret-bearing scope before rendering the new editor.
export function ModelCredentialEditor(props: Props) {
  return <CredentialScope key={props.connectorId} {...props}/>;
}

function CredentialScope({ connectorId, displayName }: Props) {
  const id = useId();
  const [status, setStatus] = useState<ModelCredentialStatus | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [mustRefresh, setMustRefresh] = useState(false);
  const alive = useRef(false), generation = useRef(0), locked = useRef(false);
  const controller = useRef<AbortController | null>(null), input = useRef<HTMLInputElement | null>(null);

  async function reload() {
    if (locked.current) return;
    const request = ++generation.current;
    controller.current?.abort();
    const active = new AbortController(); controller.current = active;
    setLoading(true); setError(""); setNotice("");
    try {
      const value = await getModelCredential(connectorId, active.signal);
      if (!alive.current || generation.current !== request) return;
      setStatus(value); setMustRefresh(false);
      setNotice("API 키 저장 상태를 확인했습니다. 입력 중인 값은 유지됩니다. 저장된 키의 값이나 유효성은 확인하지 않습니다.");
    } catch (failure) {
      if (alive.current && generation.current === request) {
        setError(failure instanceof ApiError && failure.status === 403
          ? "API 키 상태 확인에는 관리자 권한과 같은 서버의 로컬 /app 화면이 필요합니다. 다른 주소의 개발 화면에서는 확인할 수 없습니다. 입력은 유지됩니다. 상태 새로고침 후 다시 시도하세요."
          : "API 키 저장 상태를 확인하지 못했습니다. 입력은 유지됩니다. 상태 새로고침 후 다시 시도하세요.");
        setMustRefresh(true);
      }
    } finally {
      if (alive.current && generation.current === request) setLoading(false);
    }
  }
  useEffect(() => {
    alive.current = true; const element = input.current; void reload();
    return () => {
      alive.current = false; generation.current++; controller.current?.abort();
      if (element) element.value = "";
    };
  }, []);

  const writable = !!status?.storage_available && !loading && !saving && !mustRefresh;
  async function mutate(operation: "save" | "delete") {
    if (!alive.current || locked.current || !writable || !status || (operation === "delete" && !status.stored)) return;
    if (operation === "save" && (!apiKey.trim() || apiKey.length > 4096 || !/^[\x21-\x7e]+$/.test(apiKey.trim()))) {
      setError("API 키는 내부 공백 없이, 비어 있지 않은 4,096자 이하의 영문·숫자·ASCII 기호로 입력하세요."); setNotice(""); return;
    }
    locked.current = true;
    const request = ++generation.current, revision = status.revision;
    controller.current?.abort(); setSaving(true); setError(""); setNotice("");
    try {
      const value = operation === "save" ? await saveModelCredential(connectorId, revision, apiKey)
        : await deleteModelCredential(connectorId, revision);
      if (value.revision < revision || value.stored !== (operation === "save")) throw new Error("unexpected_credential_result");
      if (!alive.current || generation.current !== request) return;
      setStatus(value); setApiKey(""); setMustRefresh(false);
      setNotice(operation === "save" ? "API 키를 로컬 암호화 저장소에 저장했습니다. 키 유효성 확인이나 모델 실행은 하지 않았습니다."
        : value.environment_present ? "저장된 API 키를 삭제했습니다. 서버 환경 변수의 키가 적용됩니다."
          : "저장된 API 키를 삭제했습니다. 현재 적용할 API 키가 없습니다.");
    } catch (failure) {
      if (!alive.current || generation.current !== request) return;
      if (failure instanceof ApiError && failure.status === 422) {
        setError("API 키 입력값을 확인하세요. 입력은 유지됩니다.");
      } else {
        setMustRefresh(true);
        setError(failure instanceof ApiError && failure.status === 403
          ? "API 키 변경에는 관리자 권한과 같은 서버의 로컬 /app 화면이 필요합니다. 다른 주소의 개발 화면에서는 변경할 수 없습니다. 입력은 유지됩니다. 상태 새로고침 후 다시 시도하세요."
          : failure instanceof ApiError && failure.status === 409
            ? "다른 곳에서 API 키 저장 상태가 변경되었습니다. 입력은 유지됩니다. 상태 새로고침 후 내용을 확인하고 다시 저장하거나 삭제하세요."
            : failure instanceof ApiError && failure.status === 503
              ? "로컬 암호화 저장소를 사용할 수 없거나 변경 결과를 확인하지 못했습니다. 입력은 유지됩니다. 상태 새로고침 후 다시 시도하세요."
              : "API 키 변경 결과를 확인하지 못했습니다. 입력은 유지됩니다. 중복 변경을 피하려면 상태를 새로고침하고 확인한 뒤 다시 저장하거나 삭제하세요.");
      }
    } finally {
      locked.current = false;
      if (alive.current && generation.current === request) setSaving(false);
    }
  }

  return <section className="model-credential-editor" aria-label={`${displayName} 자격 증명`} aria-busy={saving}>
    <h4>{displayName} 자격 증명</h4>
    <p id={`${id}-help`}>Windows 현재 사용자용 로컬 암호화 저장소에 보관합니다. 저장은 키의 유효성 확인이나 모델 실행 승인이 아닙니다.</p>
    <V2Button type="button" disabled={saving} onClick={() => void reload()}>API 키 상태 새로고침</V2Button>
    {loading ? <p role="status">API 키 저장 상태 확인 중</p> : null}
    {status ? <div className="model-credential-status">
      {loading || mustRefresh ? <p>마지막으로 확인한 상태 · 변경 전 새로고침 필요</p> : null}
      <p>로컬 저장 키: {status.stored ? "저장됨 (값 표시 안 함)" : "없음"}</p>
      <p>서버 환경 변수: {status.environment_present ? "설정됨 (값 표시 안 함)" : "없음"}</p>
      <p>적용 경로: {status.source === "stored" ? "로컬 저장 키" : status.source === "environment" ? "서버 환경 변수" : "키 없음"}</p>
      {!status.storage_available ? <p>이 환경에서는 로컬 암호화 저장소를 사용할 수 없습니다. 서버 환경 변수 방식은 별도로 사용할 수 있습니다.</p> : null}
    </div> : null}
    <p>로컬 저장 키가 서버 환경 변수보다 우선합니다. 저장 키를 삭제하면 환경 변수의 키가 다시 적용될 수 있으며, 환경 변수 자체는 삭제하지 않습니다.</p>
    <label htmlFor={`${id}-key`}>{displayName} 값
      <input ref={input} id={`${id}-key`} type="password" autoComplete="off" autoCapitalize="none" spellCheck={false}
        maxLength={4096} aria-describedby={`${id}-help`} value={apiKey} disabled={saving || !status?.storage_available}
        onChange={event => { setApiKey(event.target.value); setNotice(""); }}
        onKeyDown={event => {
          if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); event.stopPropagation(); void mutate("save"); }
        }}/>
    </label>
    <p>새 키를 저장하면 기존 저장 키를 교체합니다. 키 값은 다시 표시하지 않습니다.</p>
    <div className="model-credential-actions">
      <V2Button type="button" disabled={!writable || !apiKey.trim()} busy={saving} onClick={() => void mutate("save")}>API 키 저장</V2Button>
      <V2Button type="button" disabled={!writable || !status?.stored} onClick={() => void mutate("delete")}>저장된 API 키 삭제</V2Button>
    </div>
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
  </section>;
}
