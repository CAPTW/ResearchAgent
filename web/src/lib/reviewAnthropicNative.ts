import { fetchJson } from "./api";
import type { TransportAuthority, TransportDetail, TransportList } from "./reviewTransport";
import type { PaperApprovalPreview } from "./paperApproval";

export type AnthropicNativePrepare = { request_id: string; expected_preparation_digest: string; acknowledge_unknown: boolean; expected_binding_sha256: string };
export type AnthropicNativeDispatch = { request_id: string; expected_sequence: number; expected_contract_sha256: string; expected_authority_sha256: string };
export type AnthropicOwnerDecision = { request_id: string; expected_sequence: number; expected_contract_sha256: string; expected_binding_sha256: string };
export type AnthropicNativeApproval = PaperApprovalPreview & { local_owner_decision_allowed?: boolean; local_owner_binding_sha256?: string };
export type AnthropicNativePreview = { schema_version: "ANTHROPIC_PAPER_NATIVE_PREVIEW_V1"; enabled: boolean; reason: string | null;
  actor_id: string; preparation_id: string; preparation_digest: string; profile: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2";
  binding_sha256: string | null; prepared: TransportDetail["contract"]["prepared"] | null;
  manifest: TransportDetail["contract"]["transport"] | null; input_origin: TransportDetail["contract"]["input_origin"] | null; execution_allowed: false };
export type AnthropicNativeStatus = { schema_version: "ANTHROPIC_PAPER_NATIVE_DISPATCH_STATUS_V1"; enabled: boolean; reason: string | null;
  actor_id: string; preparation_id: string; profile: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"; execution_id: string; observed_sequence: number;
  contract_sha256: string; binding_sha256: string; input_origin: TransportDetail["contract"]["input_origin"];
  authority: TransportAuthority | null; manifest: TransportDetail["contract"]["transport"] };

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const integer = (value: unknown, minimum = 0): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const date = (value: unknown): value is string => typeof value === "string" && Number.isFinite(Date.parse(value));
const origin = (value: unknown) => value === "PROVIDER_RESPONSE";
function ordered(value: unknown): unknown {
  return Array.isArray(value) ? value.map(ordered) : record(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value;
}
const same = (left: unknown, right: unknown) => JSON.stringify(ordered(left)) === JSON.stringify(ordered(right));
const sameBinding = (value: unknown, frozen: unknown) => record(value) && record(frozen)
  && (!("binding_sha256" in frozen) || value.binding_sha256 === frozen.binding_sha256);

function validAuthorizationMarker(value: Record<string, unknown>): boolean {
  const kind = Object.hasOwn(value, "authorization_kind"), decision = Object.hasOwn(value, "decision_sha256");
  return !kind && !decision || kind && decision && value.authorization_kind === "OWNER_FIRST_OUTPUT" && hash(value.decision_sha256);
}
const sameAuthorizationMarker = (left: unknown, right: unknown) => record(left) && record(right)
  && validAuthorizationMarker(left) && validAuthorizationMarker(right)
  && left.authorization_kind === right.authorization_kind && left.decision_sha256 === right.decision_sha256;

function validManifest(value: unknown): value is TransportDetail["contract"]["transport"] {
  if (!record(value) || value.schema_version !== "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"
    || value.adapter_id !== "xrml-anthropic-prepared-review" || value.adapter_version !== "2"
    || value.provider_name !== "anthropic" || !text(value.model) || value.execution_surface !== "NATIVE_HTTP"
    || value.endpoint !== "https://api.anthropic.com/v1/messages" || value.api_version !== "2023-06-01" || value.method !== "POST"
    || value.schema_compiler !== "ANTHROPIC_JSON_SCHEMA_V2" || value.shared_schema_context !== "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM"
    || value.schema_constraints !== "LOCAL_ORIGINAL_VALIDATION" || !readNativeCredentialBinding(value)
    || !["contract_sha256", "request_sha256", "body_sha256", "local_schema_sha256", "wire_schema_sha256", "effective_system_sha256"].every(key => hash(value[key]))
    || !["body_bytes", "max_response_bytes", "max_review_output_bytes"].every(key => integer(value[key], 1))
    || typeof value.timeout_seconds !== "number" || !Number.isFinite(value.timeout_seconds) || value.timeout_seconds <= 0 || value.timeout_seconds > 120
    || value.timeout_semantics !== "PER_OPERATION_INACTIVITY" || value.stream !== false || value.redirects !== false || value.environment_proxy !== false
    || !Array.isArray(value.tools) || value.tools.length !== 0 || value.temperature_policy !== "OMITTED_PROVIDER_DEFAULT"
    || value.automatic_retry !== false || value.output_token_limit_enforced !== false || value.temperature_enforced !== false
    || value.raw_output_encoding !== "CONTENT_TEXT_JSON_UTF8_V1" || value.raw_envelope_retained !== false) return false;
  return true;
}
export function readNativeCredentialBinding(value: unknown): { connector_id: "anthropic_api"; revision: number; storage: "LOCAL_DPAPI" } | null {
  if (!record(value) || !record(value.credential_binding)) return null;
  const binding = value.credential_binding;
  return Object.keys(binding).sort().join(",") === "connector_id,revision,storage"
    && binding.connector_id === "anthropic_api" && integer(binding.revision, 1) && binding.storage === "LOCAL_DPAPI"
    ? { connector_id: "anthropic_api", revision: binding.revision, storage: "LOCAL_DPAPI" } : null;
}
function validPrepared(value: unknown, preparationId: string, preparationDigest?: string): value is TransportDetail["contract"]["prepared"] {
  if (!record(value) || !["PREPARED_REVIEW_REQUEST_V1", "PREPARED_REVIEW_REQUEST_V2", "PREPARED_REVIEW_REQUEST_V3", "PREPARED_REVIEW_REQUEST_V4"].includes(String(value.schema_version))
    || value.provider_name !== "anthropic" || !text(value.model) || !hash(value.request_sha256) || !hash(value.contract_sha256)
    || !record(value.provider_request) || !integer(value.provider_request.max_output_tokens, 256) || value.provider_request.max_output_tokens > 8000
    || typeof value.provider_request.temperature !== "number" || !Number.isFinite(value.provider_request.temperature)
    || !record(value.packet) || !record(value.packet.source_preparation) || !Array.isArray(value.packet.evidence_spans)) return false;
  const source = value.packet.source_preparation;
  if (value.schema_version === "PREPARED_REVIEW_REQUEST_V4") {
    const unit = source.analysis_unit;
    if (value.packet.packet_version !== "ai-review-packet-v5" || source.schema_version !== "REVIEW_PREPARATION_BINDING_V3"
      || !record(unit) || unit.schema_version !== "PAPER_ANALYSIS_UNIT_V1" || !record(unit.budget)
      || unit.budget.provider_name !== value.provider_name || unit.budget.model !== value.model
      || unit.budget.max_output_tokens !== value.provider_request.max_output_tokens || !hash(unit.plan_sha256)
      || !text(unit.plan_id) || !integer(unit.unit_index) || !integer(unit.unit_count, 1) || unit.unit_index >= unit.unit_count
      || !integer(unit.budget.context_tokens, 1) || !integer(unit.budget.input_tokens, 1) || !integer(unit.budget.max_output_tokens, 256) || !integer(unit.budget.safety_tokens)
      || unit.budget.input_tokens + unit.budget.max_output_tokens + unit.budget.safety_tokens > unit.budget.context_tokens
      || unit.budget.estimator !== "UTF8_BYTES_CONSERVATIVE_V1") return false;
  }
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
    && validAuthorizationMarker(value)
    && date(value.granted_at) && date(value.expires_at) && (value.revoked_at === null || date(value.revoked_at))
    && ["NOT_YET_VALID", "WITHIN_VALIDITY_WINDOW", "EXPIRED", "REVOKED"].includes(String(value.validity));
}
export function isAnthropicNativeV2(value: TransportDetail | null): boolean {
  return value?.contract.mode === "TRUSTED_PAPER_HTTP" && origin(value.contract.input_origin)
    && (value.authority === null || record(value.authority) && validAuthorizationMarker(value.authority))
    && value.contract.prepared.provider_name === "anthropic" && validManifest(value.contract.transport);
}
export function readAnthropicNativePreview(value: unknown, preparationId: string, listing: TransportList): AnthropicNativePreview | null {
  if (!record(value) || value.schema_version !== "ANTHROPIC_PAPER_NATIVE_PREVIEW_V1" || value.profile !== "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"
    || value.preparation_id !== preparationId || value.preparation_digest !== listing.preparation_digest || !hash(value.preparation_digest)
    || !text(value.actor_id) || value.execution_allowed !== false || typeof value.enabled !== "boolean" || !(value.reason === null || text(value.reason))) return null;
  if (!value.enabled) return text(value.reason) && value.prepared === null && value.manifest === null && value.binding_sha256 === null && value.input_origin === null ? value as AnthropicNativePreview : null;
  if (value.reason !== null || !hash(value.binding_sha256) || !origin(value.input_origin) || !validPrepared(value.prepared, preparationId, value.preparation_digest)
    || !validManifest(value.manifest) || value.manifest.model !== value.prepared.model || value.manifest.request_sha256 !== value.prepared.request_sha256) return null;
  return value as AnthropicNativePreview;
}
export function readAnthropicNativeStatus(value: unknown, selected: TransportDetail): AnthropicNativeStatus | null {
  if (!record(value) || value.schema_version !== "ANTHROPIC_PAPER_NATIVE_DISPATCH_STATUS_V1" || value.profile !== "ANTHROPIC_PREPARED_REVIEW_WIRE_V2"
    || value.preparation_id !== selected.contract.preparation_id || value.actor_id !== selected.contract.actor_id
    || value.execution_id !== selected.contract.execution_id || value.observed_sequence !== selected.current.sequence
    || !hash(value.contract_sha256) || value.contract_sha256 !== selected.contract_sha256 || !hash(value.binding_sha256)
    || value.input_origin !== selected.contract.input_origin || !validManifest(value.manifest) || !same(value.manifest, selected.contract.transport)
    || typeof value.enabled !== "boolean" || !(value.reason === null || text(value.reason)) || !validAuthority(value.authority)) return null;
  if (!isAnthropicNativeV2(selected) || !sameBinding(value, selected) || value.authority && (value.authority.issuer_id === "xrml-anthropic-mock-issuer-v1"
    || value.authority.policy_version === "PAPER_ANTHROPIC_MOCK_ONLY_V1")) return null;
  if (value.authority && selected.authority && (value.authority.record_sha256 !== selected.authority.record_sha256
    || value.authority.authorization_id !== selected.authority.authorization_id || !sameAuthorizationMarker(value.authority, selected.authority))) return null;
  if (value.enabled && (value.reason !== null || selected.current.state !== "APPROVED" || !selected.authority || !value.authority
    || value.authority.validity !== "WITHIN_VALIDITY_WINDOW" || value.authority.revoked_at !== null)) return null;
  return value as AnthropicNativeStatus;
}
function readDetail(value: unknown, preparationId: string): TransportDetail | null {
  if (!record(value) || !record(value.contract) || !record(value.current) || !Array.isArray(value.events) || !value.events.length
    || !validAuthority(value.authority)) return null;
  if (!hash(value.contract_sha256) || !hash(value.binding_sha256)) return null;
  if (("contract_sha256" in value && !hash(value.contract_sha256)) || ("binding_sha256" in value && !hash(value.binding_sha256))) return null;
  const contract = value.contract;
  if (contract.preparation_id !== preparationId || contract.mode !== "TRUSTED_PAPER_HTTP" || !text(contract.execution_id) || !text(contract.actor_id)
    || !date(contract.created_at) || !origin(contract.input_origin) || !validPrepared(contract.prepared, preparationId) || !validManifest(contract.transport)
    || contract.transport.model !== contract.prepared.model || contract.transport.request_sha256 !== contract.prepared.request_sha256
    || !record(contract.specification)
    || !hash(contract.specification.expected_preparation_digest) || contract.specification.provider_name !== "anthropic"
    || contract.specification.model !== contract.prepared.model || contract.specification.max_output_tokens !== contract.prepared.provider_request.max_output_tokens) return null;
  for (const event of value.events) {
    if (!record(event) || !integer(event.sequence) || !["READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED", "FAILED", "INTERRUPTED", "CANCELLED", "REVOKED"].includes(String(event.state))
      || !date(event.at) || !(event.deadline === null || date(event.deadline)) || typeof event.outcome_unknown !== "boolean"
      || !(event.reason === null || text(event.reason)) || !(event.receipt_id === null || text(event.receipt_id))) return null;
  }
  return same(value.current, value.events[value.events.length - 1]) ? value as TransportDetail : null;
}
export function readAnthropicNativePrepared(value: unknown, shown: AnthropicNativePreview, body: AnthropicNativePrepare): TransportDetail | null {
  const result = readDetail(value, shown.preparation_id), specification = result?.contract.specification;
  if (!shown.enabled || !result || !record(value) || !record(specification) || specification.request_id !== body.request_id
    || specification.expected_preparation_digest !== body.expected_preparation_digest || specification.acknowledge_unknown !== body.acknowledge_unknown
    || value.binding_sha256 !== body.expected_binding_sha256 || result.contract.actor_id !== shown.actor_id
    || result.contract.input_origin !== shown.input_origin || !same(result.contract.prepared, shown.prepared) || !same(result.contract.transport, shown.manifest)) return null;
  return result;
}
export function readAnthropicNativeDispatched(value: unknown, frozen: TransportDetail, body: AnthropicNativeDispatch): TransportDetail | null {
  const result = readDetail(value, frozen.contract.preparation_id);
  if (!result || !same(result.contract, frozen.contract) || !sameBinding(value, frozen) || body.expected_sequence !== frozen.current.sequence || result.contract_sha256 !== body.expected_contract_sha256
    || result.authority?.record_sha256 !== body.expected_authority_sha256 || !sameAuthorizationMarker(result.authority, frozen.authority)
    || result.current.sequence < frozen.current.sequence) return null;
  const matched = result.events.some(event => record(event.command) && event.command.kind === "dispatch" && record(event.command.body)
    && event.command.body.request_id === body.request_id && event.command.body.expected_sequence === body.expected_sequence && event.state === "LEASED");
  return matched ? result : null;
}
export function readAnthropicNativeApproved(value: unknown, frozen: TransportDetail, body: AnthropicNativeDispatch, offer: NonNullable<PaperApprovalPreview["offer"]>): TransportDetail | null {
  const result = readDetail(value, frozen.contract.preparation_id);
  if (!result || !same(result.contract, frozen.contract) || !hash(frozen.contract_sha256) || frozen.contract_sha256 !== body.expected_contract_sha256
    || (result.contract_sha256 !== undefined && result.contract_sha256 !== body.expected_contract_sha256)
    || (record(value) && record(frozen) && "binding_sha256" in value && "binding_sha256" in frozen && value.binding_sha256 !== frozen.binding_sha256)
    || result.authority?.record_sha256 !== body.expected_authority_sha256 || result.authority.authorization_id !== offer.authorization_id
    || result.authority.issuer_id !== offer.issuer_id || result.authority.policy_version !== offer.policy_version || result.authority.expires_at !== offer.expires_at
    || !sameAuthorizationMarker(result.authority, offer)
    || result.current.sequence < frozen.current.sequence) return null;
  const matched = result.events.some(event => event.state === "APPROVED" && record(event.command) && event.command.kind === "control"
    && record(event.command.body) && event.command.body.action === "approve" && event.command.body.request_id === body.request_id
    && event.command.body.expected_sequence === body.expected_sequence && event.authority?.authorization_id === offer.authorization_id
    && event.authority.record_sha256 === body.expected_authority_sha256);
  // Retain the observed digest only after matching the complete frozen contract.
  return matched ? { ...result, contract_sha256: frozen.contract_sha256 } : null;
}
export function readAnthropicNativeApproval(value: unknown, saved: TransportDetail): AnthropicNativeApproval | null {
  if (!isAnthropicNativeV2(saved) || !record(value) || value.schema_version !== "PAPER_APP_APPROVAL_V1"
    || value.execution_id !== saved.contract.execution_id || value.actor_id !== saved.contract.actor_id
    || !hash(value.contract_sha256) || value.contract_sha256 !== saved.contract_sha256
    || value.observed_sequence !== saved.current.sequence || typeof value.approval_allowed !== "boolean"
    || value.execution_allowed !== false || !(value.reason === null || text(value.reason))) return null;
  if ("local_owner_decision_allowed" in value && (typeof value.local_owner_decision_allowed !== "boolean" || !hash(value.local_owner_binding_sha256)
    || value.local_owner_decision_allowed && (saved.current.state !== "READY" || value.approval_allowed || value.offer !== null
      || value.reason !== "paper_approval_source_unavailable"))) return null;
  if ("local_owner_binding_sha256" in value && !("local_owner_decision_allowed" in value)) return null;
  if ("local_owner_binding_sha256" in value && !sameBinding({ binding_sha256: value.local_owner_binding_sha256 }, saved)) return null;
  if (!value.approval_allowed) return text(value.reason) && value.offer === null ? value as AnthropicNativeApproval : null;
  const offer = value.offer;
  if (saved.current.state !== "READY" || value.reason !== null || !record(offer) || !text(offer.authorization_id) || !hash(offer.record_sha256)
    || !validAuthorizationMarker(offer)
    || !text(offer.issuer_id) || !text(offer.policy_version) || offer.issuer_id === "xrml-anthropic-mock-issuer-v1"
    || offer.policy_version === "PAPER_ANTHROPIC_MOCK_ONLY_V1" || offer.input_origin !== "PROVIDER_RESPONSE"
    || !date(offer.expires_at) || offer.max_attempts !== 1 || offer.reserved_output_tokens !== saved.contract.prepared.provider_request.max_output_tokens) return null;
  return value as PaperApprovalPreview;
}
export function readAnthropicNativeRevoked(value: unknown, frozen: TransportDetail, body: AnthropicNativeDispatch): TransportDetail | null {
  const result = readDetail(value, frozen.contract.preparation_id);
  if (!result || !same(result.contract, frozen.contract) || !sameBinding(value, frozen) || result.contract_sha256 !== body.expected_contract_sha256
    || result.authority?.record_sha256 !== body.expected_authority_sha256 || result.authority.authorization_id !== frozen.authority?.authorization_id
    || !sameAuthorizationMarker(result.authority, frozen.authority)
    || result.current.sequence < frozen.current.sequence || result.authority.validity !== "REVOKED" || !result.authority.revoked_at) return null;
  if (["RESULT_SAVED", "FAILED", "INTERRUPTED", "CANCELLED", "REVOKED"].includes(frozen.current.state)) {
    return same(result.events, frozen.events) && same(result.current, frozen.current) ? result : null;
  }
  return result.events.some(event => event.state === "REVOKED" && record(event.command) && event.command.kind === "control"
    && record(event.command.body) && event.command.body.action === "revoke" && event.command.body.request_id === body.request_id
    && event.command.body.expected_sequence === body.expected_sequence) ? result : null;
}
export function readAnthropicOwnerDecision(value: unknown, frozen: TransportDetail, body: AnthropicOwnerDecision): boolean {
  return record(value) && value.schema_version === "PAPER_LOCAL_ANTHROPIC_DECISION_RECEIPT_V1"
    && value.execution_id === frozen.contract.execution_id && value.actor_id === frozen.contract.actor_id
    && value.contract_sha256 === body.expected_contract_sha256 && value.contract_sha256 === frozen.contract_sha256
    && value.binding_sha256 === body.expected_binding_sha256 && sameBinding(value, frozen)
    && same(value.command, body) && body.expected_sequence === frozen.current.sequence
    && hash(value.decision_sha256) && hash(value.record_sha256) && date(value.created_at) && value.execution_allowed === false;
}
const post = (url: string, body: AnthropicNativePrepare | AnthropicNativeDispatch | AnthropicOwnerDecision) => fetchJson<unknown>(url,
  { method: "POST", headers: { "X-XRML-Paper-Anthropic-Action": "1" }, body: JSON.stringify(body) });
export const getAnthropicNativePreview = (root: string) => fetchJson<unknown>(`${root}/anthropic-native-preview`);
export const prepareAnthropicNative = (root: string, body: AnthropicNativePrepare) => post(`${root}/anthropic-native-requests`, body);
export const getAnthropicNativeStatus = (root: string, id: string) => fetchJson<unknown>(`${root}/${encodeURIComponent(id)}/anthropic-native-status`);
export const getAnthropicNativeApproval = (root: string, id: string) => fetchJson<unknown>(`${root}/${encodeURIComponent(id)}/anthropic-native-approval`);
export const decideAnthropicNative = (root: string, id: string, body: AnthropicOwnerDecision) => post(`${root}/${encodeURIComponent(id)}/anthropic-native-owner-decision`, body);
export const actAnthropicNative = (root: string, id: string, kind: "approve" | "dispatch" | "revoke", body: AnthropicNativeDispatch) =>
  post(`${root}/${encodeURIComponent(id)}/anthropic-native-${kind === "approve" ? "approval" : kind === "dispatch" ? "dispatch" : "revocation"}`, body);
