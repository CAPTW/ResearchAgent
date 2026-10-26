import { fetchJson } from "./api";

export type TrialState = "PREPARED" | "CLAIMED" | "CAPTURED" | "STRUCTURE_VALID" | "STRUCTURE_INVALID" | "INCOMPLETE" | "REFUSED" | "UNKNOWN" | "CANCELLED" | "EXPIRED";
export const trialDiagnosticLabels = {
  SCHEMA_COMPLEXITY: "응답 형식이 너무 복잡하여 Provider가 거절했습니다. 앱의 응답 스키마 조정이 필요합니다.",
  INSUFFICIENT_CREDITS: "API 크레딧 부족으로 Provider가 거절했습니다. Console 결제 화면에서 해당 조직의 잔액을 확인하세요.",
  BILLING_ERROR: "Provider가 결제 문제를 보고했습니다. Console 결제 설정을 확인하세요.",
  AUTHENTICATION_ERROR: "Provider가 인증 문제를 보고했습니다. 저장한 API 키의 유효성을 확인하세요.",
} as const;
export const trialOutputReasonLabels = {
  PAPER_ID_MISMATCH: "응답의 논문 ID가 선택한 논문과 다릅니다.",
  SOURCE_HASH_MISMATCH: "응답의 본문 해시가 전송한 발췌와 다릅니다.",
  MISSING_CITATIONS: "인용 ID가 없거나 비어 있습니다.",
  UNKNOWN_CITATIONS: "전송한 근거에 없는 인용 ID가 포함되어 있습니다.",
  OUTPUT_SCHEMA_INVALID: "필수 항목·값의 형식이 검토 결과 규칙과 맞지 않습니다.",
  OUTPUT_CONTRACT_INVALID: "응답이 고정된 검토 요청의 검증 조건을 충족하지 못했습니다.",
} as const;
type OutputDiagnostic = {
  schema_version: "ANTHROPIC_TRIAL_OUTPUT_DIAGNOSTIC_V1"; source: "SAVED_RAW_RESPONSE";
  raw_response_sha256: string; reported_model: string; response_id: string;
  usage: Record<string, number>; reason_codes: (keyof typeof trialOutputReasonLabels)[];
};
export type AnthropicTrial = {
  trial_id: string; binding_sha256: string; state: TrialState;
  source: { title: string; pdf_sha256: string; page: number; start: number; end: number; excerpt_sha256: string; excerpt: string };
  model: string; max_output_tokens: number; credential_revision: number; expires_at: string;
  origin: "PUBLIC_EXCERPT" | "FIXTURE_SYNTHETIC"; raw_capture_available: boolean;
  actual_model: string | null; response_id: string | null; usage: Record<string, number> | null;
  draft: Record<string, unknown> | null; http_status: number | null; error_code: string | null;
  attempts: 0 | 1; can_execute: boolean; scientific_support: "NOT_ASSESSED"; full_document_review: "NOT_ESTABLISHED";
  http_timeouts?: { connect: number; read: number; write: number; pool: number };
  transport_trace?: { event: string; elapsed_ms: number }[] | null;
  provider_diagnostic?: keyof typeof trialDiagnosticLabels | null;
  output_diagnostic?: OutputDiagnostic | null;
};
export type TrialEnvelope = { enabled: boolean; reason: string | null; trial: AnthropicTrial | null };
export type TrialAction = { expected_binding_sha256: string };
export const trialRoot = "/settings/model-connections/anthropic_api/first-trial";
const states = new Set<TrialState>(["PREPARED", "CLAIMED", "CAPTURED", "STRUCTURE_VALID", "STRUCTURE_INVALID", "INCOMPLETE", "REFUSED", "UNKNOWN", "CANCELLED", "EXPIRED"]);
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max: number): value is string => typeof value === "string" && value.trim().length > 0 && [...value].length <= max;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const integer = (value: unknown, min: number, max = Number.MAX_SAFE_INTEGER): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max;
const date = (value: unknown): value is string => typeof value === "string" && /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const responseIdentity = (value: unknown): value is string => text(value, 200) && value === value.trim()
  && !/[\u0000-\u001f\u007f\uD800-\uDFFF]/u.test(value);
const usageKeys = new Set(["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"]);
export const trialTerminal = (state: TrialState) => !["PREPARED", "CLAIMED", "CAPTURED"].includes(state);
const tracePhases: Record<string, string> = { "connection.connect_tcp": "TCP 연결", "connection.start_tls": "TLS 연결",
  "http11.send_request_headers": "요청 헤더 전송", "http11.send_request_body": "요청 본문 전송",
  "http11.receive_response_headers": "응답 헤더 수신", "http11.receive_response_body": "응답 본문 수신",
  "http11.response_closed": "응답 연결 정리" };
const traceStatuses: Record<string, string> = { started: "시작", complete: "완료", failed: "실패" };
export function trialTraceLabel(event: string): string {
  const split = event.lastIndexOf(".");
  return `${tracePhases[event.slice(0, split)]} · ${traceStatuses[event.slice(split + 1)]}`;
}

export function readTrialEnvelope(value: unknown): TrialEnvelope {
  if (!record(value) || typeof value.enabled !== "boolean" || !(value.reason === null || text(value.reason, 300))) throw new Error("invalid_trial_envelope");
  if (value.trial === null) {
    if (value.enabled) throw new Error("invalid_trial_envelope");
    return value as TrialEnvelope;
  }
  const trial = value.trial;
  if (!record(trial) || !text(trial.trial_id, 200) || !hash(trial.binding_sha256) || !states.has(trial.state as TrialState)
    || !text(trial.model, 200) || !integer(trial.max_output_tokens, 256, 8000) || !integer(trial.credential_revision, 0)
    || !date(trial.expires_at) || !["PUBLIC_EXCERPT", "FIXTURE_SYNTHETIC"].includes(trial.origin as string)
    || typeof trial.raw_capture_available !== "boolean" || typeof trial.can_execute !== "boolean"
    || !(trial.actual_model === null || text(trial.actual_model, 200)) || !(trial.response_id === null || text(trial.response_id, 200))
    || !(trial.http_status === null || integer(trial.http_status, 100, 599)) || !(trial.error_code === null || text(trial.error_code, 300))
    || !(trial.usage === null || record(trial.usage) && Object.values(trial.usage).every(count => integer(count, 0, 1_000_000_000)))
    || !(trial.draft === null || record(trial.draft)) || !integer(trial.attempts, 0, 1)
    || trial.scientific_support !== "NOT_ASSESSED" || trial.full_document_review !== "NOT_ESTABLISHED") throw new Error("invalid_trial");
  if (trial.provider_diagnostic !== undefined && trial.provider_diagnostic !== null
    && (typeof trial.provider_diagnostic !== "string" || !Object.hasOwn(trialDiagnosticLabels, trial.provider_diagnostic)
      || !trial.raw_capture_available || trial.draft !== null
      || trial.http_status !== ({ SCHEMA_COMPLEXITY: 400, INSUFFICIENT_CREDITS: 400, BILLING_ERROR: 402, AUTHENTICATION_ERROR: 401 } as Record<string, number>)[trial.provider_diagnostic])) throw new Error("invalid_trial_diagnostic");
  const output = trial.output_diagnostic;
  if (output !== undefined && output !== null && (!record(output) || Object.keys(output).length !== 7
    || output.schema_version !== "ANTHROPIC_TRIAL_OUTPUT_DIAGNOSTIC_V1" || output.source !== "SAVED_RAW_RESPONSE"
    || !hash(output.raw_response_sha256) || !responseIdentity(output.reported_model) || !responseIdentity(output.response_id)
    || !record(output.usage) || !Object.entries(output.usage).every(([key, count]) => usageKeys.has(key) && integer(count, 0, 1_000_000_000))
    || !Array.isArray(output.reason_codes) || output.reason_codes.length === 0 || output.reason_codes.length > 6
    || new Set(output.reason_codes).size !== output.reason_codes.length
    || !output.reason_codes.every(code => typeof code === "string" && Object.hasOwn(trialOutputReasonLabels, code))
    || trial.state !== "STRUCTURE_INVALID" || trial.http_status !== 200 || !trial.raw_capture_available
    || trial.draft !== null || trial.attempts !== 1 || trial.can_execute)) throw new Error("invalid_trial_output_diagnostic");
  if (trial.http_timeouts !== undefined && (!record(trial.http_timeouts) || Object.keys(trial.http_timeouts).length !== 4
    || !["connect", "read", "write", "pool"].every(key => {
      const limit = (trial.http_timeouts as Record<string, unknown>)[key];
      return typeof limit === "number" && Number.isFinite(limit) && limit > 0 && limit <= 600;
    }))) throw new Error("invalid_trial_timeouts");
  if (trial.transport_trace !== undefined && trial.transport_trace !== null) {
    let previous = 0;
    if (!Array.isArray(trial.transport_trace) || trial.transport_trace.length > 32 || !trial.transport_trace.every(item => {
      if (!record(item) || Object.keys(item).length !== 2 || typeof item.event !== "string"
        || typeof item.elapsed_ms !== "number" || !Number.isFinite(item.elapsed_ms) || item.elapsed_ms < previous) return false;
      previous = item.elapsed_ms;
      const split = item.event.lastIndexOf(".");
      return Object.hasOwn(tracePhases, item.event.slice(0, split)) && Object.hasOwn(traceStatuses, item.event.slice(split + 1));
    })) throw new Error("invalid_trial_trace");
  }
  const source = trial.source;
  if (!record(source) || !text(source.title, 500) || !hash(source.pdf_sha256) || !hash(source.excerpt_sha256)
    || !integer(source.page, 1) || !integer(source.start, 0) || !integer(source.end, 1) || source.end <= source.start
    || !text(source.excerpt, 20_000) || [...source.excerpt].length !== source.end - source.start) throw new Error("invalid_trial_source");
  if (trial.can_execute && (!value.enabled || trial.state !== "PREPARED" || trial.attempts !== 0)
    || trial.state === "PREPARED" && (trial.attempts !== 0 || trial.raw_capture_available || trial.draft !== null)
    || ["CLAIMED", "CAPTURED", "STRUCTURE_VALID", "STRUCTURE_INVALID", "INCOMPLETE", "REFUSED", "UNKNOWN"].includes(trial.state as string) && trial.attempts !== 1
    || trial.state === "CAPTURED" && (!trial.raw_capture_available || trial.draft !== null)
    || trial.state === "STRUCTURE_VALID" && (!trial.raw_capture_available || trial.draft === null || trial.actual_model === null || trial.response_id === null || trial.http_status !== 200)
    || trial.draft !== null && trial.state !== "STRUCTURE_VALID") throw new Error("invalid_trial_state");
  return value as TrialEnvelope;
}

export function sameTrialInput(left: AnthropicTrial, right: AnthropicTrial): boolean {
  const key = (value: AnthropicTrial) => [value.trial_id, value.binding_sha256, value.model, value.max_output_tokens,
    value.credential_revision, value.expires_at, value.origin, value.source.title, value.source.pdf_sha256,
    value.source.page, value.source.start, value.source.end, value.source.excerpt_sha256, value.source.excerpt,
    value.http_timeouts?.connect, value.http_timeouts?.read, value.http_timeouts?.write, value.http_timeouts?.pool];
  return JSON.stringify(key(left)) === JSON.stringify(key(right));
}
export function advancesTrial(prior: AnthropicTrial, next: AnthropicTrial): boolean {
  if (!sameTrialInput(prior, next) || next.attempts < prior.attempts || prior.raw_capture_available && !next.raw_capture_available) return false;
  if (trialTerminal(prior.state)) return prior.state === next.state;
  if (prior.state === "CAPTURED") return next.state !== "PREPARED" && next.state !== "CLAIMED";
  if (prior.state === "CLAIMED") return next.state !== "PREPARED";
  return true;
}
export const getAnthropicTrial = async (signal?: AbortSignal) => readTrialEnvelope(await fetchJson<unknown>(trialRoot, { signal }));
export async function actOnAnthropicTrial(action: "execute" | "cancel", body: TrialAction): Promise<TrialEnvelope> {
  return readTrialEnvelope(await fetchJson<unknown>(`${trialRoot}/${action}`, { method: "POST",
    headers: { "Content-Type": "application/json", "X-XRML-Trial-Action": "1" }, body: JSON.stringify(body) }));
}
export async function getAnthropicCapture(trial: AnthropicTrial, signal?: AbortSignal): Promise<string> {
  const value = await fetchJson<unknown>(`${trialRoot}/capture`, { signal });
  if (!record(value) || value.trial_id !== trial.trial_id || value.binding_sha256 !== trial.binding_sha256
    || typeof value.raw_response !== "string" || !hash(value.sha256) || /[\uD800-\uDFFF]/u.test(value.raw_response)
    || trial.output_diagnostic && value.sha256 !== trial.output_diagnostic.raw_response_sha256) throw new Error("invalid_trial_capture");
  const bytes = new TextEncoder().encode(value.raw_response);
  if (bytes.length > 4_000_000) throw new Error("invalid_trial_capture");
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  if (Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("") !== value.sha256) throw new Error("invalid_trial_capture");
  return value.raw_response;
}
