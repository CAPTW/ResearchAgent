import type { CodexJournalV2Manifest, ExecutionDetail } from "./journalExecutions";
import { executionStates } from "./journalExecutions";
import { readJournalAdapterManifest } from "../v2/views/JournalAdapterManifest";

export type JournalApprovalBody = { authorization_id: string; expected_sequence: number; expected_contract_digest: string;
  expected_record_digest: string; expected_record_sha256: string };
export type JournalApprovalOffer = { authorization_id: string; record_digest: string; record_sha256: string;
  binding: { surface_id: "codex_cli"; adapter_id: "xrml-codex-frozen-journal"; adapter_version: "2"; command_shape_sha256: string };
  issuer_id: string; policy_version: string; registered_at: string; expires_at: string; proof_expires_at: string | null;
  proof_limitations: string[]; max_attempts: number; max_reserved_output_tokens: number };
export type JournalApprovalPreview = { schema_version: "JOURNAL_APP_APPROVAL_V1"; execution_id: string; actor_id: string;
  contract_digest: string; observed_sequence: number; approval_allowed: boolean; execution_allowed: false; reason: string | null;
  offer: JournalApprovalOffer | null; manifest: CodexJournalV2Manifest | null };
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const integer = (value: unknown, min: number, max: number): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max;
const date = (value: unknown): value is string => typeof value === "string" && /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const sameDate = (left: unknown, right: string | null) => right === null ? left === null : date(left) && Date.parse(left) === Date.parse(right);
function ordered(value: unknown): unknown {
  return Array.isArray(value) ? value.map(ordered) : record(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value;
}
export const sameJournalValue = (left: unknown, right: unknown) => JSON.stringify(ordered(left)) === JSON.stringify(ordered(right));
const binding = (value: unknown) => record(value) && value.surface_id === "codex_cli" && value.adapter_id === "xrml-codex-frozen-journal"
  && value.adapter_version === "2" && hash(value.command_shape_sha256);

export function readJournalApprovalPreview(value: unknown, saved: ExecutionDetail): JournalApprovalPreview | null {
  if (!record(value) || value.schema_version !== "JOURNAL_APP_APPROVAL_V1" || value.execution_allowed !== false
    || value.execution_id !== saved.contract.execution_id || value.actor_id !== saved.contract.actor_id
    || value.observed_sequence !== saved.current.sequence || !hash(value.contract_digest) || typeof value.approval_allowed !== "boolean") return null;
  if (!value.approval_allowed) return text(value.reason) && value.offer === null && value.manifest === null ? value as JournalApprovalPreview : null;
  const offer = value.offer, manifest = readJournalAdapterManifest(value.manifest);
  if (value.reason !== null || !manifest || manifest.schema_version !== "CODEX_JOURNAL_CLI_V2"
    || manifest.contract_digest !== value.contract_digest || manifest.model !== saved.contract.specification.model
    || manifest.argv_shape[0] !== "{explicit_launcher}" || !record(offer) || !text(offer.authorization_id) || offer.authorization_id.length > 100
    || !hash(offer.record_digest) || !hash(offer.record_sha256) || !binding(offer.binding) || !text(offer.issuer_id) || !text(offer.policy_version)
    || !date(offer.registered_at) || !date(offer.expires_at) || !(offer.proof_expires_at === null || date(offer.proof_expires_at))
    || !Array.isArray(offer.proof_limitations) || !offer.proof_limitations.every(item => typeof item === "string")
    || !integer(offer.max_attempts, 1, 3) || !integer(offer.max_reserved_output_tokens, 256, 24000)) return null;
  return value as JournalApprovalPreview;
}

// Authority records contain only integer counters, strings, booleans and null.
// Reject unsupported numeric/Unicode forms rather than silently hashing changed
// bytes. This verifies received content against a server offer, not its authority.
function archiveJSON(value: unknown, normalize: boolean): string {
  if (typeof value === "string") {
    if (/[\uD800-\uDFFF]/u.test(value)) throw new Error("invalid Unicode");
    return JSON.stringify(normalize ? value.normalize("NFC") : value);
  }
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value) && !Object.is(value, -0)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(item => archiveJSON(item, normalize)).join(",")}]`;
  if (!record(value)) throw new Error("invalid record");
  // Every Record/Grant/proof field name in this version is ASCII. Fail closed
  // instead of silently changing Python's codepoint key order for new schemas.
  if (Object.keys(value).some(key => !/^[\x20-\x7e]+$/.test(key))) throw new Error("unsupported record key");
  const entries = Object.entries(value).map(([key, item]) => [normalize ? key.normalize("NFC") : key, item] as const);
  if (new Set(entries.map(([key]) => key)).size !== entries.length) throw new Error("duplicate normalized key");
  entries.sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  return `{${entries.map(([key, item]) => `${archiveJSON(key, false)}:${archiveJSON(item, normalize)}`).join(",")}}`;
}
async function recordHash(value: unknown, normalize: boolean): Promise<string> {
  const data = new TextEncoder().encode(archiveJSON(value, normalize));
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), item => item.toString(16).padStart(2, "0")).join("");
}
export async function readJournalApprovalResult(value: unknown, saved: ExecutionDetail, body: JournalApprovalBody,
  offered: JournalApprovalOffer): Promise<{ execution: ExecutionDetail; revoked: boolean } | null> {
  try {
    if (!record(value) || !record(value.execution) || !record(value.authority) || !record(value.authority.record)) return null;
    const execution = value.execution, authority = value.authority.record, grant = authority.grant;
    if (!sameJournalValue(execution.contract, saved.contract) || !record(execution.current) || !Array.isArray(execution.events)
      || !record(grant) || !record(grant.proof) || authority.schema_version !== "JOURNAL_AUTHORITY_RECORD_V1"
      || authority.execution_id !== saved.contract.execution_id || authority.expected_sequence !== body.expected_sequence
      || grant.authorization_id !== body.authorization_id || grant.actor_id !== saved.contract.actor_id
      || grant.contract_digest !== body.expected_contract_digest || !sameJournalValue(authority.binding, offered.binding)
      || authority.registered_by !== offered.issuer_id || grant.policy_version !== offered.policy_version
      || !sameDate(authority.registered_at, offered.registered_at) || !sameDate(grant.expires_at, offered.expires_at)
      || !sameDate(grant.proof.expires_at, offered.proof_expires_at) || !sameJournalValue(grant.proof.limitations, offered.proof_limitations)
      || grant.max_attempts !== offered.max_attempts || grant.max_reserved_output_tokens !== offered.max_reserved_output_tokens) return null;
    const scope = saved.contract as unknown as Record<string, unknown>;
    if (["topic_id", "job_id"].some(key => scope[key] !== undefined && scope[key] !== authority[key])) return null;
    for (const event of [execution.current, ...execution.events]) {
      if (!record(event) || !integer(event.sequence, 0, 100) || !text(event.state) || !Object.hasOwn(executionStates, event.state)
        || !integer(event.attempt, 0, saved.contract.specification.max_attempts) || typeof event.outcome_unknown !== "boolean"
        || !(event.reason === null || text(event.reason)) || !(event.at === null || date(event.at)) || !(event.deadline === null || date(event.deadline))) return null;
    }
    if ((!execution.events.length && (execution.current.sequence !== 0 || execution.current.state !== "READY" || execution.current.attempt !== 0))
      || Number(execution.current.sequence) < saved.current.sequence || (execution.events.length > 0
      && !sameJournalValue(execution.current, execution.events[execution.events.length - 1]))) return null;
    if (await recordHash(authority, false) !== body.expected_record_sha256 || await recordHash(authority, true) !== body.expected_record_digest) return null;
    const revoked = value.authority.revocation;
    if (revoked !== null && (!record(revoked) || revoked.authorization_id !== body.authorization_id
      || revoked.record_digest !== body.expected_record_digest || !text(revoked.actor_id) || !date(revoked.at) || !text(revoked.reason))) return null;
    return { execution: execution as ExecutionDetail, revoked: revoked !== null };
  } catch { return null; }
}
