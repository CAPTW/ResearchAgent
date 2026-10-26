import { fetchJson } from "./api";
import type { TransportDetail } from "./reviewTransport";
import { isCodexPaperV2, type CodexPaperDispatch } from "./reviewCodexExecution";

export type PaperApprovalRequest = CodexPaperDispatch;
export type PaperApprovalOffer = { authorization_id: string; record_sha256: string; issuer_id: string; policy_version: string;
  authorization_kind?: "OWNER_FIRST_OUTPUT"; decision_sha256?: string;
  input_origin: "FIXTURE_SYNTHETIC" | "PROVIDER_RESPONSE"; expires_at: string; max_attempts: number; reserved_output_tokens: number };
export type PaperApprovalPreview = { schema_version: "PAPER_APP_APPROVAL_V1"; execution_id: string; actor_id: string;
  contract_sha256: string; observed_sequence: number; approval_allowed: boolean; execution_allowed: false;
  reason: string | null; offer: PaperApprovalOffer | null };
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && !!v.trim();
const hash = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
export function readPaperApproval(value: unknown, saved: TransportDetail): PaperApprovalPreview | null {
  if (!isCodexPaperV2(saved) || !object(value) || value.schema_version !== "PAPER_APP_APPROVAL_V1"
    || value.execution_id !== saved.contract.execution_id || value.actor_id !== saved.contract.actor_id
    || !hash(value.contract_sha256) || value.contract_sha256 !== saved.contract_sha256
    || value.observed_sequence !== saved.current.sequence || typeof value.approval_allowed !== "boolean"
    || value.execution_allowed !== false || !(value.reason === null || text(value.reason))) return null;
  if (!value.approval_allowed) return text(value.reason) && value.offer === null ? value as PaperApprovalPreview : null;
  const offer = value.offer;
  if (saved.current.state !== "READY" || value.reason !== null || !object(offer)
    || !text(offer.authorization_id) || !hash(offer.record_sha256) || !text(offer.issuer_id) || !text(offer.policy_version)
    || offer.input_origin !== saved.contract.input_origin || !text(offer.expires_at) || !Number.isFinite(Date.parse(offer.expires_at))
    || offer.max_attempts !== 1 || offer.reserved_output_tokens !== saved.contract.prepared.provider_request.max_output_tokens) return null;
  return value as PaperApprovalPreview;
}
const endpoint = (root: string, id: string) => `${root}/${encodeURIComponent(id)}/codex-approval`;
export const getPaperApproval = (root: string, id: string) => fetchJson<unknown>(endpoint(root, id));
export const registerPaperApproval = (root: string, id: string, body: PaperApprovalRequest) =>
  fetchJson<unknown>(endpoint(root, id), { method: "POST", body: JSON.stringify(body) });
