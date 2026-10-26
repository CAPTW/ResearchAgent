import { fetchJson } from "./api";
import type { OfflineTransportApproval, OfflineTransportCommand, TransportDetail, TransportList } from "./reviewTransport";

export type AnthropicPrepare = { request_id: string; expected_preparation_digest: string; acknowledge_unknown: boolean; expected_preview_sha256: string };
export type AnthropicAction = "approve" | "dispatch" | "revoke";
export type AnthropicPreview = { enabled: false; mode: "ANTHROPIC_STRUCTURED_OFFLINE"; reason: "paper_anthropic_offline_disabled"; preview: null }
  | { enabled: true; mode: "ANTHROPIC_STRUCTURED_OFFLINE"; reason: null; preview: {
    schema_version: "PAPER_ANTHROPIC_OFFLINE_PREVIEW_V1"; preview_sha256: string; preparation_digest: string;
    prepared: TransportDetail["contract"]["prepared"]; transport: TransportDetail["contract"]["transport"];
    specification: { provider_name: "anthropic"; model: "synthetic-anthropic-structured-model"; max_output_tokens: 2200 };
    input_origin: "FIXTURE_SYNTHETIC"; execution: "NOT_STARTED";
  } };
const model = "synthetic-anthropic-structured-model";
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const integer = (value: unknown, min = 0): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= min;
const date = (value: unknown): value is string => typeof value === "string" && Number.isFinite(Date.parse(value));
function ordered(value: unknown): unknown { return Array.isArray(value) ? value.map(ordered) : object(value) ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value; }
const same = (a: unknown, b: unknown) => JSON.stringify(ordered(a)) === JSON.stringify(ordered(b));
function validManifest(value: unknown): value is TransportDetail["contract"]["transport"] {
  return object(value) && value.schema_version === "ANTHROPIC_PREPARED_REVIEW_WIRE_V2" && value.adapter_id === "xrml-anthropic-prepared-review"
    && value.adapter_version === "2" && value.provider_name === "anthropic" && value.model === model && value.execution_surface === "MOCK_TRANSPORT"
    && value.schema_compiler === "ANTHROPIC_JSON_SCHEMA_V2" && value.shared_schema_context === "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM"
    && value.schema_constraints === "LOCAL_ORIGINAL_VALIDATION" && value.endpoint === "https://api.anthropic.com/v1/messages"
    && value.api_version === "2023-06-01" && value.method === "POST"
    && ["body_sha256", "request_sha256", "contract_sha256", "local_schema_sha256", "wire_schema_sha256", "effective_system_sha256"].every(key => hash(value[key]))
    && ["body_bytes", "max_response_bytes", "max_review_output_bytes"].every(key => integer(value[key], 1))
    && value.timeout_seconds === 60 && value.timeout_semantics === "PER_OPERATION_INACTIVITY" && value.stream === false && value.automatic_retry === false && value.redirects === false && value.environment_proxy === false
    && Array.isArray(value.tools) && !value.tools.length && value.temperature_policy === "OMITTED_PROVIDER_DEFAULT"
    && value.output_token_limit_enforced === false && value.temperature_enforced === false
    && value.raw_output_encoding === "CONTENT_TEXT_JSON_UTF8_V1" && value.raw_envelope_retained === false;
}
function validPrepared(value: unknown, preparationId: string, preparationDigest?: string): value is TransportDetail["contract"]["prepared"] {
  if (!object(value) || value.schema_version !== "PREPARED_REVIEW_REQUEST_V3" || value.provider_name !== "anthropic" || value.model !== model
    || !hash(value.request_sha256) || !hash(value.contract_sha256) || !object(value.provider_request) || value.provider_request.max_output_tokens !== 2200
    || !object(value.packet) || !object(value.packet.source_preparation) || !Array.isArray(value.packet.evidence_spans)) return false;
  const source = value.packet.source_preparation, selection = source.selection;
  return source.preparation_id === preparationId && hash(source.preparation_sha256) && (!preparationDigest || source.preparation_sha256 === preparationDigest)
    && source.normalization === "XRML_WHITESPACE_V1" && object(selection) && integer(selection.start) && integer(selection.end, 1)
    && integer(selection.chars, 1) && selection.end - selection.start === selection.chars && hash(selection.sha256)
    && object(source.source) && text(source.source.run_id) && text(source.source.paper_id) && integer(source.source.source_chars, selection.end)
    && value.packet.evidence_spans.length > 0 && value.packet.evidence_spans.every(span => object(span) && text(span.text))
    && value.packet.evidence_spans.reduce((count, span) => count + Array.from(String(span.text)).length, 0) === selection.chars;
}
export function isAnthropicStructuredOffline(value: TransportDetail | null): boolean {
  return !!value && value.contract.mode === "TRUSTED_PAPER_HTTP" && value.contract.input_origin === "FIXTURE_SYNTHETIC"
    && validPrepared(value.contract.prepared, value.contract.preparation_id) && validManifest(value.contract.transport)
    && value.contract.prepared.request_sha256 === value.contract.transport.request_sha256 && hash(value.contract_sha256)
    && (value.authority === null || value.authority?.issuer_id === "xrml-anthropic-mock-issuer-v1" && value.authority.policy_version === "PAPER_ANTHROPIC_MOCK_ONLY_V1");
}
export function readAnthropicPreview(value: unknown, preparationId: string, listing: TransportList): AnthropicPreview | null {
  if (!object(value) || value.mode !== "ANTHROPIC_STRUCTURED_OFFLINE") return null;
  if (value.enabled === false) return value.reason === "paper_anthropic_offline_disabled" && value.preview === null ? value as AnthropicPreview : null;
  const preview = value.preview;
  if (value.enabled !== true || value.reason !== null || !object(preview) || preview.schema_version !== "PAPER_ANTHROPIC_OFFLINE_PREVIEW_V1"
    || !hash(preview.preview_sha256) || preview.preparation_digest !== listing.preparation_digest || !hash(preview.preparation_digest)
    || preview.input_origin !== "FIXTURE_SYNTHETIC" || preview.execution !== "NOT_STARTED"
    || !validPrepared(preview.prepared, preparationId, listing.preparation_digest) || !validManifest(preview.transport)
    || preview.transport.request_sha256 !== preview.prepared.request_sha256 || !object(preview.specification)
    || preview.specification.provider_name !== "anthropic" || preview.specification.model !== model || preview.specification.max_output_tokens !== 2200) return null;
  return value as AnthropicPreview;
}
export function matchesAnthropicPreview(selected: TransportDetail, preview: AnthropicPreview | null): boolean {
  return !!preview?.enabled && isAnthropicStructuredOffline(selected) && same(selected.contract.prepared, preview.preview.prepared) && same(selected.contract.transport, preview.preview.transport);
}
function readDetail(value: unknown, preparationId: string): TransportDetail | null {
  if (!object(value) || !object(value.contract) || value.contract.preparation_id !== preparationId || !text(value.contract.execution_id)
    || !text(value.contract.actor_id) || !date(value.contract.created_at) || !object(value.current) || !Array.isArray(value.events) || !value.events.length
    || !isAnthropicStructuredOffline(value as TransportDetail) || !same(value.current, value.events[value.events.length - 1])) return null;
  const authority = value.authority;
  if (authority !== null && (!object(authority) || !text(authority.authorization_id) || !hash(authority.record_sha256)
    || !text(authority.issuer_id) || !text(authority.policy_version) || !date(authority.granted_at) || !date(authority.expires_at)
    || !(authority.revoked_at === null || date(authority.revoked_at)) || !["NOT_YET_VALID", "WITHIN_VALIDITY_WINDOW", "EXPIRED", "REVOKED"].includes(String(authority.validity)))) return null;
  if (!value.events.every(event => object(event) && integer(event.sequence) && date(event.at) && typeof event.outcome_unknown === "boolean"
    && ["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED", "FAILED", "INTERRUPTED", "CANCELLED", "REVOKED"].includes(String(event.state))
    && (event.deadline === null || date(event.deadline)) && (event.receipt_id === null || text(event.receipt_id)))) return null;
  return value as TransportDetail;
}
export function readAnthropicPrepared(value: unknown, shown: AnthropicPreview, body: AnthropicPrepare, preparationId: string): TransportDetail | null {
  const result = readDetail(value, preparationId), specification = result?.contract.specification;
  if (!shown.enabled || body.expected_preview_sha256 !== shown.preview.preview_sha256 || !result || !object(specification)
    || specification.request_id !== body.request_id || specification.expected_preparation_digest !== body.expected_preparation_digest
    || specification.acknowledge_unknown !== body.acknowledge_unknown || specification.provider_name !== "anthropic" || specification.model !== model
    || specification.max_output_tokens !== 2200 || !matchesAnthropicPreview(result, shown)) return null;
  return result;
}
export function readAnthropicAction(value: unknown, frozen: TransportDetail, action: AnthropicAction, body: OfflineTransportCommand | OfflineTransportApproval): TransportDetail | null {
  const result = readDetail(value, frozen.contract.preparation_id);
  if (!result || !same(result.contract, frozen.contract) || result.contract_sha256 !== frozen.contract_sha256 || result.current.sequence < frozen.current.sequence
    || body.expected_sequence !== frozen.current.sequence || !text(body.request_id)
    || action === "approve" && (!("expected_contract_sha256" in body) || body.expected_contract_sha256 !== frozen.contract_sha256)) return null;
  // Terminal revocation is an independent durable authority row. The server
  // validates exact replay there; completed execution events remain unchanged.
  if (action === "revoke" && ["RESULT_SAVED", "FAILED", "INTERRUPTED", "CANCELLED", "REVOKED"].includes(frozen.current.state)) {
    return same(result.current, frozen.current) && same(result.events, frozen.events) && result.authority?.validity === "REVOKED"
      && !!result.authority.revoked_at && result.authority.record_sha256 === frozen.authority?.record_sha256
      && result.authority.authorization_id === frozen.authority?.authorization_id ? result : null;
  }
  const event = result.events.find(entry => object(entry.command) && entry.command.kind === (action === "dispatch" ? "dispatch" : "control")
    && object(entry.command.body) && entry.command.body.request_id === body.request_id && entry.command.body.expected_sequence === body.expected_sequence
    && (action === "dispatch" ? entry.state === "LEASED" : entry.command.body.action === action));
  if (!event || action === "approve" && (event.state !== "APPROVED" || !result.authority || !event.authority
    || event.authority.authorization_id !== result.authority.authorization_id || event.authority.record_sha256 !== result.authority.record_sha256)) return null;
  if (action === "dispatch" && result.authority?.record_sha256 !== frozen.authority?.record_sha256) return null;
  if (action === "revoke" && (event.state !== "REVOKED" || result.authority?.validity !== "REVOKED" || !result.authority.revoked_at || result.authority.record_sha256 !== frozen.authority?.record_sha256)) return null;
  return result;
}
export const getAnthropicPreview = (root: string) => fetchJson<unknown>(`${root}/anthropic-preview`);
export const prepareAnthropic = (root: string, body: AnthropicPrepare) => fetchJson<unknown>(`${root}/anthropic-requests`, { method: "POST", body: JSON.stringify(body) });
export const actAnthropic = (root: string, id: string, action: AnthropicAction, body: OfflineTransportCommand | OfflineTransportApproval) => fetchJson<unknown>(`${root}/${encodeURIComponent(id)}/anthropic-offline-${action === "approve" ? "approval" : action === "dispatch" ? "dispatch" : "revocation"}`, { method: "POST", body: JSON.stringify(body) });
