import { fetchJson } from "./api";

export type TransportState = "READY" | "APPROVED" | "LEASED" | "RUNNING" | "RESULT_RECEIVED" | "RESULT_SAVED" | "FAILED" | "INTERRUPTED" | "CANCELLED" | "REVOKED";
export type TransportAuthority = {
  authorization_id: string; record_sha256: string; issuer_id: string; policy_version: string;
  authorization_kind?: "OWNER_FIRST_OUTPUT"; decision_sha256?: string;
  granted_at: string; expires_at: string; revoked_at: string | null;
  validity: "NOT_YET_VALID" | "WITHIN_VALIDITY_WINDOW" | "EXPIRED" | "REVOKED";
};
export type TransportEvent = {
  sequence: number; state: TransportState; at: string; deadline: string | null; outcome_unknown: boolean;
  reason: string | null; receipt_id: string | null; authority: Pick<TransportAuthority, "authorization_id" | "record_sha256"> | null;
  [key: string]: unknown;
};
export type TransportDetail = {
  contract: {
    execution_id: string; preparation_id: string; actor_id: string; created_at: string;
    mode: "TRUSTED_PAPER_CLI" | "TRUSTED_PAPER_HTTP" | "OFFLINE_SYNTHETIC_TRANSPORT"; input_origin: "FIXTURE_SYNTHETIC" | "PROVIDER_RESPONSE";
    prepared: {
      provider_name: string; model: string; request_sha256: string; contract_sha256: string;
      provider_request: { max_output_tokens: number; temperature: number; [key: string]: unknown };
      packet: {
        source_preparation: {
          preparation_id: string; selection: { start: number; end: number; chars: number; sha256: string };
          source: { run_id: string; paper_id: string; artifact_ref: string; source_chars: number }; normalization: string;
        };
        evidence_spans: { text: string; [key: string]: unknown }[];
      };
    };
    transport: {
      reasoning_effort?: string; timeout_seconds?: number; automatic_retry?: boolean;
      output_token_limit_enforced?: boolean; temperature_enforced?: boolean;
      endpoint?: string; body_bytes?: number; max_response_bytes?: number; max_review_output_bytes?: number;
      store?: boolean; background?: boolean; stream?: boolean; truncation?: string;
      redirects?: boolean; environment_proxy?: boolean;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  current: TransportEvent; events: TransportEvent[]; authority: TransportAuthority | null;
  offline_eligible?: boolean; contract_sha256?: string;
};
export type TransportCapabilities = {
  create: boolean; approve: boolean; dispatch: boolean; cancel: boolean; recover: boolean; save_result: boolean;
  reason: string; issuer_status: string; provider_status: string; live_execution: string;
};
export type TransportList = {
  preparation_digest: string; items: TransportDetail[]; page: number; pageSize: number; hasMore: boolean;
  outcome_unknown_pending: boolean; capabilities: TransportCapabilities;
  offline?: { enabled: boolean; mode: "OFFLINE_SYNTHETIC_TRANSPORT"; model: string; issuer_id: string; policy_version: string; reason: string | null };
};
export type TransportControl = { request_id: string; expected_sequence: number; action: "cancel" | "recover" | "save_result" };
export type OfflineTransportPrepare = { request_id: string; expected_preparation_digest: string; acknowledge_unknown: boolean };
export type OfflineTransportCommand = { request_id: string; expected_sequence: number };
export type OfflineTransportApproval = OfflineTransportCommand & { expected_contract_sha256: string };
export const transportRoot = (root: string, id: string) => `${root}/${encodeURIComponent(id)}/transport-executions`;
export const listTransportExecutions = (root: string, page: number) => fetchJson<TransportList>(`${root}?page=${page}&pageSize=10`);
export const getTransportExecution = (root: string, id: string) => fetchJson<TransportDetail>(`${root}/${encodeURIComponent(id)}`);
export const controlTransportExecution = (root: string, id: string, body: TransportControl) => fetchJson<TransportDetail>(`${root}/${encodeURIComponent(id)}/controls`, { method: "POST", body: JSON.stringify(body) });
export const prepareOfflineTransport = (root: string, body: OfflineTransportPrepare) => fetchJson<TransportDetail>(`${root}/offline-requests`, { method: "POST", body: JSON.stringify(body) });
export const approveOfflineTransport = (root: string, id: string, body: OfflineTransportApproval) => fetchJson<TransportDetail>(`${root}/${encodeURIComponent(id)}/offline-approval`, { method: "POST", body: JSON.stringify(body) });
export const dispatchOfflineTransport = (root: string, id: string, body: OfflineTransportCommand) => fetchJson<TransportDetail>(`${root}/${encodeURIComponent(id)}/offline-dispatch`, { method: "POST", body: JSON.stringify(body) });
export const revokeOfflineTransport = (root: string, id: string, body: OfflineTransportCommand) => fetchJson<TransportDetail>(`${root}/${encodeURIComponent(id)}/offline-revocation`, { method: "POST", body: JSON.stringify(body) });
