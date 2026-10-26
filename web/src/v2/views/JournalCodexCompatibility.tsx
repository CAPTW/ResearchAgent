import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import type { CodexJournalProfile } from "../../lib/journalExecutions";

type Identity = { launcher_sha256: string; environment_sha256: string; workspace_sha256: string };
type Props = { url: string; executionId: string; actorId: string; sequence: number; contractDigest: string; runtimeIdentity: Identity; profile?: CodexJournalProfile };
type Observation = { status: string; report: { cli_version: string | null; checked_at: string } | null };
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const labels: Record<string, string> = {
  NOT_CHECKED: "서버에 연결된 로컬 검사 기록이 없습니다. 운영자가 별도 검사 도구로 기록을 생성해야 합니다.",
  INVALID: "로컬 검사 기록을 읽거나 검증할 수 없습니다. 운영자가 기록 경로와 형식을 확인해야 합니다.",
  STALE: "검사 기록의 실행 환경 또는 검사 조건이 현재와 다릅니다. 현재 환경으로 다시 검사해야 합니다.",
  UNAVAILABLE: "현재 미리보기 실행 환경을 확인할 수 없습니다. 실행 조건을 다시 조회하세요.",
  CONFIGURATION_REJECTED: "V1 설정을 적용한 로컬 CLI 검사에 실패했습니다. 실제 실행에 사용하기 전에 실패 원인을 확인해야 합니다.",
  INCOMPLETE: "로컬 CLI 검사가 끝나지 않았거나 필요한 옵션을 확인하지 못했습니다. 실행 호환성은 확인되지 않았습니다.",
  CONFIGURATION_OBSERVED: "격리된 로컬 명령·설정 목록을 확인했습니다. 실제 exec 실행 호환성을 통과했다는 뜻은 아닙니다.",
};
function consistent(report: Record<string, unknown>): boolean {
  if (!Array.isArray(report.checks) || report.checks.length > 3) return false;
  const checks = report.checks, names = ["version", "exec_help", "configuration"];
  if (!checks.every((check, i) => record(check) && check.name === names[i] && Number.isSafeInteger(check.exit_code) && hash(check.stdout_sha256))) return false;
  if (report.cli_version !== null && (!checks.length || checks[0].exit_code !== 0)) return false;
  if (checks.length > 1 && (report.cli_version === null || checks[0].exit_code !== 0)) return false;
  if (checks.length === 3 && checks[1].exit_code !== 0) return false;
  if (report.status === "CONFIGURATION_OBSERVED") return report.reason === null && checks.length === 3 && report.cli_version !== null && checks.every(check => check.exit_code === 0);
  if (report.status === "CONFIGURATION_REJECTED") return report.reason === "config_rejected" && checks.length === 3 && report.cli_version !== null && checks[2].exit_code !== 0;
  if (report.reason === "invalid_version") return checks.length === 1 && checks[0].exit_code === 0 && report.cli_version === null;
  if (report.reason === "required_flags_missing") return checks.length === 2 && checks[1].exit_code === 0 && report.cli_version !== null;
  return report.reason === "probe_failed" && checks.length < 3 && (!checks.length || report.cli_version !== null || checks[0].exit_code !== 0);
}
function read(value: unknown, props: Props): Observation | null {
  if (!record(value) || value.schema_version !== "CODEX_JOURNAL_COMPATIBILITY_V1"
    || value.execution_id !== props.executionId || value.actor_id !== props.actorId || value.observed_sequence !== props.sequence
    || value.contract_digest !== props.contractDigest || value.execution_allowed !== false || value.model_invoked !== false
    || typeof value.status !== "string" || !Object.hasOwn(labels, value.status)) return null;
  if (["NOT_CHECKED", "INVALID", "STALE", "UNAVAILABLE"].includes(value.status)) return value.report === null ? { status: value.status, report: null } : null;
  const report = value.report;
  const profile = props.profile ?? "CODEX_JOURNAL_CLI_V1";
  if (!record(report) || report.schema_version !== (profile === "CODEX_JOURNAL_CLI_V2" ? "CODEX_CLI_COMPATIBILITY_V2" : "CODEX_CLI_COMPATIBILITY_V1") || report.profile !== profile
    || report.status !== value.status || report.reason !== value.reason || report.execution_allowed !== false || report.model_invoked !== false || !consistent(report)
    || !hash(report.requirements_sha256) || typeof report.checked_at !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(report.checked_at) || !Number.isFinite(Date.parse(report.checked_at))
    || !(report.cli_version === null || (typeof report.cli_version === "string" && report.cli_version.length <= 100
      && /^codex-cli \d+\.\d+\.\d+(?:[-+][A-Za-z0-9][A-Za-z0-9.+-]*)?$/.test(report.cli_version)))
    || !record(report.runtime_identity) || !Object.entries(props.runtimeIdentity).every(([key, entry]) => report.runtime_identity && (report.runtime_identity as Record<string, unknown>)[key] === entry)) return null;
  return { status: value.status, report: { cli_version: report.cli_version, checked_at: report.checked_at } };
}

export function JournalCodexCompatibility(props: Props) {
  return <Current key={`${props.url}:${props.executionId}:${props.sequence}:${props.contractDigest}:${props.runtimeIdentity.launcher_sha256}:${props.runtimeIdentity.environment_sha256}:${props.runtimeIdentity.workspace_sha256}:${props.profile ?? "CODEX_JOURNAL_CLI_V1"}`} {...props}/>;
}
function Current(props: Props) {
  const { preferences } = useOptionalAppearance();
  const [value, setValue] = useState<Observation | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function refresh() {
    if (locked.current) return;
    locked.current = true; setBusy(true); setValue(null); setError("");
    try {
      const response = await fetchJson<unknown>(`${props.url}/codex-compatibility`);
      if (!alive.current) return;
      const observation = read(response, props);
      if (!observation) { setError("검사 기록의 형식을 확인하지 못했습니다. 실행 이력과 조건을 새로고침하세요."); return; }
      setValue(observation);
    } catch { if (alive.current) setError("검사 기록을 불러오지 못했습니다. 연결과 조회 권한을 확인한 뒤 다시 확인하세요."); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  return <section aria-label="Codex CLI 호환성 기록">
    <h6 style={{ fontSize: "1em" }}>로컬 CLI 설정 검사</h6>
    {props.profile === "CODEX_JOURNAL_CLI_V2" ? <p>검사 대상 프로필: Codex CLI V2</p> : null}
    <p>이 버튼은 저장된 검사 기록만 읽으며 CLI를 실행하지 않습니다. 기록은 실행 승인이 아니며 로그인·모델 사용·도구 격리·내부 재시도·비용 상한을 검증하지 않습니다.</p>
    <button type="button" disabled={busy} onClick={() => { void refresh(); }}>CLI 설정 검사 기록 확인</button>
    {busy ? <p role="status">검사 기록을 읽는 중</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {value ? <p role="status">{props.profile === "CODEX_JOURNAL_CLI_V2" && value.status === "CONFIGURATION_REJECTED"
      ? "V2 설정을 적용한 로컬 CLI 검사에 실패했습니다. 실제 실행에 사용하기 전에 실패 원인을 확인해야 합니다." : labels[value.status]}</p> : null}
    {value?.report ? <>
      <p>검사 CLI: {value.report.cli_version ?? "버전 미확인"}</p>
      <p>검사 시각: {formatTimestamp(value.report.checked_at, preferences.timeFormat, "ko-KR")}</p>
      <p>별도 빈 프로필에서 수행한 관찰입니다. 현재 계정과 사용자 설정을 읽거나 대신 검사한 결과가 아닙니다.</p>
    </> : null}
  </section>;
}
