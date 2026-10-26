import { fetchJson } from "./api";
import type { TransportAuthority, TransportDetail, TransportList } from "./reviewTransport";
import { hasCodexV2Policy } from "../v2/views/JournalAdapterManifest";

export type CodexPaperPrepare = { request_id: string; expected_preparation_digest: string; acknowledge_unknown: boolean; expected_binding_sha256: string };
export type CodexPaperDispatch = { request_id: string; expected_sequence: number; expected_contract_sha256: string; expected_authority_sha256: string };
export type CodexPaperPreview = { schema_version: "CODEX_PAPER_PREVIEW_V1"; enabled: boolean; reason: string | null;
  actor_id: string; preparation_id: string; preparation_digest: string; profile: "CODEX_PREPARED_REVIEW_CLI_V2";
  binding_sha256: string | null; prepared: TransportDetail["contract"]["prepared"] | null;
  manifest: TransportDetail["contract"]["transport"] | null; input_origin: TransportDetail["contract"]["input_origin"] | null; execution_allowed: false };
export type CodexPaperStatus = { schema_version: "CODEX_PAPER_DISPATCH_STATUS_V1"; enabled: boolean; reason: string | null;
  actor_id: string; preparation_id: string; profile: "CODEX_PREPARED_REVIEW_CLI_V2"; execution_id: string; observed_sequence: number;
  contract_sha256: string; binding_sha256: string; input_origin: TransportDetail["contract"]["input_origin"];
  authority: TransportAuthority | null; manifest: TransportDetail["contract"]["transport"] };

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const integer = (value: unknown, minimum = 0): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const date = (value: unknown): value is string => typeof value === "string" && Number.isFinite(Date.parse(value));
const origin = (value: unknown) => value === "FIXTURE_SYNTHETIC" || value === "PROVIDER_RESPONSE";
function ordered(value: unknown): unknown {
  return Array.isArray(value) ? value.map(ordered) : record(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value;
}
const same = (left: unknown, right: unknown) => JSON.stringify(ordered(left)) === JSON.stringify(ordered(right));

function validManifest(value: unknown): value is TransportDetail["contract"]["transport"] {
  if (!record(value) || !hasCodexV2Policy(value) || value.schema_version !== "CODEX_PREPARED_REVIEW_CLI_V2"
    || value.adapter_id !== "xrml-codex-prepared-review" || value.adapter_version !== "2"
    || !text(value.model) || !text(value.reasoning_effort) || !record(value.runtime_identity)
    || !["launcher_sha256", "environment_sha256", "workspace_sha256"].every(key => hash(value.runtime_identity && (value.runtime_identity as Record<string, unknown>)[key]))
    || !["contract_sha256", "request_sha256", "stdin_sha256", "schema_sha256"].every(key => hash(value[key]))
    || !Array.isArray(value.argv_shape) || value.argv_shape[0] !== "{explicit_launcher}" || !value.argv_shape.every(text)
    || !["stdin_bytes", "max_stdout_bytes", "max_stderr_bytes", "max_final_bytes", "max_review_output_bytes"].every(key => integer(value[key], 1))
    || typeof value.timeout_seconds !== "number" || !Number.isFinite(value.timeout_seconds) || value.timeout_seconds <= 0 || value.timeout_seconds > 120
    || value.automatic_retry !== false || value.output_token_limit_enforced !== false || value.temperature_enforced !== false
    || value.tool_isolation !== "REQUESTED_NOT_LIVE_VERIFIED" || value.model_identity !== "REQUESTED_NOT_INDEPENDENTLY_VERIFIED") return false;
  return true;
}
function validPrepared(value: unknown, preparationId: string, preparationDigest?: string): value is TransportDetail["contract"]["prepared"] {
  if (!record(value) || !["PREPARED_REVIEW_REQUEST_V1", "PREPARED_REVIEW_REQUEST_V2", "PREPARED_REVIEW_REQUEST_V3", "PREPARED_REVIEW_REQUEST_V4"].includes(String(value.schema_version))
    || value.provider_name !== "codex_cli" || !text(value.model) || !hash(value.request_sha256) || !hash(value.contract_sha256)
    || !record(value.provider_request) || !integer(value.provider_request.max_output_tokens, 256) || value.provider_request.max_output_tokens > 8000
    || typeof value.provider_request.temperature !== "number" || !Number.isFinite(value.provider_request.temperature)
    || !record(value.packet) || !record(value.packet.source_preparation) || !Array.isArray(value.packet.evidence_spans)) return false;
  const source = value.packet.source_preparation;
  if (source.preparation_id !== preparationId || !hash(source.preparation_sha256) || (preparationDigest && source.preparation_sha256 !== preparationDigest)
    || source.normalization !== "XRML_WHITESPACE_V1" || !record(source.selection) || !record(source.source)
    || !integer(source.selection.start) || !integer(source.selection.end, 1) || !integer(source.selection.chars, 1)
    || source.selection.end - source.selection.start !== source.selection.chars || !hash(source.selection.sha256)
    || !text(source.source.run_id) || !text(source.source.paper_id) || !integer(source.source.source_chars, source.selection.end)
    || !value.packet.evidence_spans.length || !value.packet.evidence_spans.every(span => record(span) && typeof span.text === "string")) return false;
  return value.packet.evidence_spans.reduce((count, span) => count + Array.from(String(span.text)).length, 0) === source.selection.chars;
}
function validAuthority(value: unknown): value is TransportAuthority | null {
  return value === null || record(value) && text(value.authorization_id) && hash(value.record_sha256) && text(value.issuer_id) && text(value.policy_version)
    && date(value.granted_at) && date(value.expires_at) && (value.revoked_at === null || date(value.revoked_at))
    && ["NOT_YET_VALID", "WITHIN_VALIDITY_WINDOW", "EXPIRED", "REVOKED"].includes(String(value.validity));
}
export function isCodexPaperV2(value: TransportDetail | null): boolean {
  return value?.contract.mode === "TRUSTED_PAPER_CLI" && value.contract.prepared.provider_name === "codex_cli" && validManifest(value.contract.transport);
}
export function readCodexPaperPreview(value: unknown, preparationId: string, listing: TransportList): CodexPaperPreview | null {
  if (!record(value) || value.schema_version !== "CODEX_PAPER_PREVIEW_V1" || value.profile !== "CODEX_PREPARED_REVIEW_CLI_V2"
    || value.preparation_id !== preparationId || value.preparation_digest !== listing.preparation_digest || !hash(value.preparation_digest)
    || !text(value.actor_id) || value.execution_allowed !== false || typeof value.enabled !== "boolean" || !(value.reason === null || text(value.reason))) return null;
  if (!value.enabled) return value.prepared === null && value.manifest === null && value.binding_sha256 === null && value.input_origin === null ? value as CodexPaperPreview : null;
  if (value.reason !== null || !hash(value.binding_sha256) || !origin(value.input_origin) || !validPrepared(value.prepared, preparationId, value.preparation_digest)
    || !validManifest(value.manifest) || value.manifest.model !== value.prepared.model || value.manifest.request_sha256 !== value.prepared.request_sha256
    || value.manifest.stdin_sha256 !== value.prepared.request_sha256) return null;
  return value as CodexPaperPreview;
}
export function readCodexPaperStatus(value: unknown, selected: TransportDetail): CodexPaperStatus | null {
  if (!record(value) || value.schema_version !== "CODEX_PAPER_DISPATCH_STATUS_V1" || value.profile !== "CODEX_PREPARED_REVIEW_CLI_V2"
    || value.preparation_id !== selected.contract.preparation_id || value.actor_id !== selected.contract.actor_id
    || value.execution_id !== selected.contract.execution_id || value.observed_sequence !== selected.current.sequence
    || !hash(value.contract_sha256) || value.contract_sha256 !== selected.contract_sha256 || !hash(value.binding_sha256)
    || value.input_origin !== selected.contract.input_origin || !validManifest(value.manifest) || !same(value.manifest, selected.contract.transport)
    || typeof value.enabled !== "boolean" || !(value.reason === null || text(value.reason)) || !validAuthority(value.authority)) return null;
  if (value.authority && selected.authority && (value.authority.record_sha256 !== selected.authority.record_sha256
    || value.authority.authorization_id !== selected.authority.authorization_id)) return null;
  if (value.enabled && (value.reason !== null || selected.current.state !== "APPROVED" || !value.authority
    || value.authority.validity !== "WITHIN_VALIDITY_WINDOW" || value.authority.revoked_at !== null)) return null;
  return value as CodexPaperStatus;
}
function readDetail(value: unknown, preparationId: string, requireProjection = true): TransportDetail | null {
  if (!record(value) || !record(value.contract) || !record(value.current) || !Array.isArray(value.events) || !value.events.length
    || !validAuthority(value.authority)) return null;
  if (requireProjection && (!hash(value.contract_sha256) || !hash(value.binding_sha256))) return null;
  if (("contract_sha256" in value && !hash(value.contract_sha256)) || ("binding_sha256" in value && !hash(value.binding_sha256))) return null;
  const contract = value.contract;
  if (contract.preparation_id !== preparationId || contract.mode !== "TRUSTED_PAPER_CLI" || !text(contract.execution_id) || !text(contract.actor_id)
    || !date(contract.created_at) || !origin(contract.input_origin) || !validPrepared(contract.prepared, preparationId) || !validManifest(contract.transport)
    || contract.transport.model !== contract.prepared.model || contract.transport.request_sha256 !== contract.prepared.request_sha256
    || !record(contract.specification) || !hash(contract.specification.expected_preparation_digest)) return null;
  for (const event of value.events) {
    if (!record(event) || !integer(event.sequence) || !["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED", "FAILED", "INTERRUPTED", "CANCELLED", "REVOKED"].includes(String(event.state))
      || !date(event.at) || !(event.deadline === null || date(event.deadline)) || typeof event.outcome_unknown !== "boolean"
      || !(event.reason === null || text(event.reason)) || !(event.receipt_id === null || text(event.receipt_id))) return null;
  }
  return same(value.current, value.events[value.events.length - 1]) ? value as TransportDetail : null;
}
export function readCodexPaperPrepared(value: unknown, shown: CodexPaperPreview, body: CodexPaperPrepare): TransportDetail | null {
  const result = readDetail(value, shown.preparation_id), specification = result?.contract.specification;
  if (!result || !record(value) || !record(specification) || specification.request_id !== body.request_id
    || specification.expected_preparation_digest !== body.expected_preparation_digest || specification.acknowledge_unknown !== body.acknowledge_unknown
    || value.binding_sha256 !== body.expected_binding_sha256 || result.contract.actor_id !== shown.actor_id
    || result.contract.input_origin !== shown.input_origin || !same(result.contract.prepared, shown.prepared) || !same(result.contract.transport, shown.manifest)) return null;
  return result;
}
export function readCodexPaperDispatched(value: unknown, frozen: TransportDetail, body: CodexPaperDispatch): TransportDetail | null {
  const result = readDetail(value, frozen.contract.preparation_id);
  if (!result || !same(result.contract, frozen.contract) || result.contract_sha256 !== body.expected_contract_sha256
    || result.authority?.record_sha256 !== body.expected_authority_sha256 || result.current.sequence < frozen.current.sequence) return null;
  const matched = result.events.some(event => record(event.command) && event.command.kind === "dispatch" && record(event.command.body)
    && event.command.body.request_id === body.request_id && event.command.body.expected_sequence === body.expected_sequence && event.state === "LEASED");
  return matched ? result : null;
}
export function readCodexPaperApproved(value: unknown, frozen: TransportDetail, body: CodexPaperDispatch, authorizationId: string): TransportDetail | null {
  // Approval returns raw stored history; prepare/dispatch additionally project digests.
  const result = readDetail(value, frozen.contract.preparation_id, false);
  if (!result || !same(result.contract, frozen.contract) || !hash(frozen.contract_sha256) || frozen.contract_sha256 !== body.expected_contract_sha256
    || (result.contract_sha256 !== undefined && result.contract_sha256 !== body.expected_contract_sha256)
    || (record(value) && record(frozen) && "binding_sha256" in value && "binding_sha256" in frozen && value.binding_sha256 !== frozen.binding_sha256)
    || result.authority?.record_sha256 !== body.expected_authority_sha256 || result.authority.authorization_id !== authorizationId
    || result.current.sequence < frozen.current.sequence) return null;
  const matched = result.events.some(event => event.state === "APPROVED" && record(event.command) && event.command.kind === "control"
    && record(event.command.body) && event.command.body.action === "approve" && event.command.body.request_id === body.request_id
    && event.command.body.expected_sequence === body.expected_sequence && event.authority?.authorization_id === authorizationId
    && event.authority.record_sha256 === body.expected_authority_sha256);
  // Retain the observed digest only after matching the complete frozen contract.
  return matched ? { ...result, contract_sha256: frozen.contract_sha256 } : null;
}
export const getCodexPaperPreview = (root: string) => fetchJson<unknown>(`${root}/codex-preview`);
export const prepareCodexPaper = (root: string, body: CodexPaperPrepare) => fetchJson<unknown>(`${root}/codex-requests`, { method: "POST", body: JSON.stringify(body) });
export const getCodexPaperStatus = (root: string, id: string) => fetchJson<unknown>(`${root}/${encodeURIComponent(id)}/codex-dispatch`);
export const dispatchCodexPaper = (root: string, id: string, body: CodexPaperDispatch) => fetchJson<unknown>(`${root}/${encodeURIComponent(id)}/codex-dispatch`, { method: "POST", body: JSON.stringify(body) });
