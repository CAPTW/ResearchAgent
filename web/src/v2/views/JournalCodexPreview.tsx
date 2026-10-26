import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import type { CodexJournalManifest, CodexJournalV2Manifest, ExecutionDetail } from "../../lib/journalExecutions";
import { JournalAdapterManifest, readJournalAdapterManifest } from "./JournalAdapterManifest";
import { JournalCodexCompatibility } from "./JournalCodexCompatibility";

type Props = { url: string; saved: ExecutionDetail };
type Preview = {
  schema_version: "CODEX_JOURNAL_PREVIEW_V1"; preview_only: true; execution_allowed: false;
  execution_id: string; contract_digest: string; actor_id: string; observed_sequence: number;
  available: boolean; reason: string | null; manifest: CodexJournalManifest | CodexJournalV2Manifest | null;
  binding: { surface_id: string; adapter_id: string; adapter_version: string; command_shape_sha256: string } | null;
  preview_digest: string | null;
};
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
function reasonLabel(reason: unknown): string | null {
  switch (reason) {
    case "codex_preview_not_configured": return "서버의 Codex 미리보기 실행 환경이 설정되지 않았습니다. 서버 운영자가 미리보기용 실행 환경을 명시적으로 설정해야 합니다. 이 로컬 확인에는 계정 로그인이나 실제 모델 호출이 필요하지 않습니다.";
    case "codex_preview_provider_unsupported": return "이 요청은 Codex 미리보기를 지원하는 요청이 아닙니다. 저장된 요청 도구를 확인하세요.";
    case "codex_preview_configuration_invalid": return "서버의 Codex 미리보기 설정을 사용할 수 없습니다. 서버 운영자가 설정을 수정한 뒤 다시 확인하세요.";
    case "codex_preview_configuration_changed": return "서버의 Codex 미리보기 설정이 바뀌었습니다. 서버 운영자가 설정을 다시 고정하고 서버를 재시작한 뒤 확인하세요.";
    case "codex_preview_runtime_changed": return "Codex 미리보기 실행 환경이 바뀌었습니다. 서버 운영자가 실행 환경을 다시 고정하고 서버를 재시작한 뒤 확인하세요.";
    case "codex_preview_contract_unsupported": return "저장된 요청 형식으로 Codex 미리보기를 만들 수 없습니다. 고정된 요청 정보를 확인하세요.";
    default: return null;
  }
}
function readPreview(value: unknown, saved: ExecutionDetail): Preview | null {
  if (!record(value) || value.schema_version !== "CODEX_JOURNAL_PREVIEW_V1" || value.preview_only !== true || value.execution_allowed !== false
    || value.execution_id !== saved.contract.execution_id || value.actor_id !== saved.contract.actor_id
    || value.observed_sequence !== saved.current.sequence || !hash(value.contract_digest)) return null;
  if (value.available === false) {
    return reasonLabel(value.reason) && value.manifest === null && value.binding === null && value.preview_digest === null ? value as Preview : null;
  }
  const manifest = readJournalAdapterManifest(value.manifest), binding = value.binding;
  if (value.available !== true || value.reason !== null || !hash(value.preview_digest) || !manifest || !record(binding)
    || (manifest.schema_version !== "CODEX_JOURNAL_CLI_V1" && manifest.schema_version !== "CODEX_JOURNAL_CLI_V2") || manifest.contract_digest !== value.contract_digest
    || manifest.model !== saved.contract.specification.model || manifest.argv_shape[0] !== "{explicit_launcher}"
    || binding.surface_id !== "codex_cli" || binding.adapter_id !== "xrml-codex-frozen-journal"
    || binding.adapter_version !== (manifest.schema_version === "CODEX_JOURNAL_CLI_V2" ? "2" : "1") || !hash(binding.command_shape_sha256)) return null;
  return value as Preview;
}

export function JournalCodexPreview(props: Props) {
  return props.saved.contract.specification.provider_name === "codex_cli"
    ? <CurrentPreview key={`${props.url}:${props.saved.contract.execution_id}:${props.saved.current.sequence}`} {...props}/> : null;
}
function CurrentPreview({ url, saved }: Props) {
  const [preview, setPreview] = useState<Preview | null>(null), [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false), [error, setError] = useState(""), [changed, setChanged] = useState(false);
  const alive = useRef(true), locked = useRef(false), previousDigest = useRef<string | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function refresh() {
    if (locked.current) return;
    locked.current = true; setBusy(true); setRequested(true); setPreview(null); setError(""); setChanged(false);
    try {
      const response = await fetchJson<unknown>(`${url}/codex-preview`);
      if (!alive.current) return;
      const value = readPreview(response, saved);
      if (!value) { setError("Codex 실행 조건을 확인하지 못했습니다. 실행 이력을 새로고침한 뒤 다시 확인하세요."); return; }
      if (value.available) {
        setChanged(previousDigest.current !== null && previousDigest.current !== value.preview_digest);
        previousDigest.current = value.preview_digest;
      }
      setPreview(value);
    } catch {
      if (alive.current) setError("Codex 실행 조건을 불러오지 못했습니다. 연결과 조회 권한을 확인한 뒤 다시 확인하세요.");
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  return <section aria-label="현재 Codex 실행 조건 미리보기">
    <h5>현재 Codex 실행 조건 미리보기</h5>
    <p>현재 서버 조건으로 계산하는 읽기 전용 미리보기입니다. 과거 시도의 실행 기록이나 실행 승인이 아니며, CLI를 실행하거나 로그인·계정·모델 사용 가능 여부를 확인하지 않습니다.</p>
    <button type="button" disabled={busy} onClick={() => { void refresh(); }}>{requested ? "Codex 실행 조건 다시 확인" : "현재 Codex 실행 조건 확인"}</button>
    {busy ? <p role="status">현재 Codex 실행 조건 확인 중</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {preview && !preview.available ? <p role="status">{reasonLabel(preview.reason)}</p> : null}
    {preview?.available && preview.manifest && preview.binding ? <>
      {changed ? <p role="status">이전에 확인한 미리보기와 실행 조건이 달라졌습니다. 현재 조건을 다시 검토하세요. 저장된 요청이나 과거 실행 이력을 변경한 것은 아닙니다.</p> : null}
      <p>이 지문은 실행 파일 경로·바이트, 환경 변수 값, 작업 폴더 경로를 식별합니다. 인증 정보 파일·설정 파일·작업 폴더 내용의 동일성이나 안전성을 보장하지 않습니다.</p>
      <p className="local-report-id">미리보기 SHA-256: {preview.preview_digest}</p>
      <p className="local-report-id">고정 요청 SHA-256: {preview.contract_digest}</p>
      <p className="local-report-id">전송 조건 SHA-256: {preview.binding.command_shape_sha256}</p>
      <p className="local-report-id">실행 파일·경로 지문: {preview.manifest.runtime_identity.launcher_sha256}</p>
      <p className="local-report-id">환경 변수 지문: {preview.manifest.runtime_identity.environment_sha256}</p>
      <p className="local-report-id">작업 폴더 경로 지문: {preview.manifest.runtime_identity.workspace_sha256}</p>
      <JournalAdapterManifest value={preview.manifest} context="preview"/>
      <JournalCodexCompatibility url={url} executionId={saved.contract.execution_id} actorId={saved.contract.actor_id}
        sequence={saved.current.sequence} contractDigest={preview.contract_digest} runtimeIdentity={preview.manifest.runtime_identity} profile={preview.manifest.schema_version}/>
      <details><summary>요청할 명령·제한 설정</summary>
        <p>실행 파일과 작업 경로를 자리표시자로 바꾼 명령 형태입니다. 복사해서 실행할 실제 명령이 아니며, 표시된 제한의 실제 강제 여부를 검증한 결과도 아닙니다.</p>
        <pre aria-label="Codex 명령 형태" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxWidth: "100%" }}>{JSON.stringify(preview.manifest.argv_shape, null, 2)}</pre>
      </details>
    </> : null}
  </section>;
}
