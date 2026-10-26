import { fetchJson } from "./api";

export type ReviewExecutionState = "READY" | "APPROVED" | "LEASED" | "RUNNING" | "RESULT_RECEIVED" | "RESULT_SAVED" | "FAILED" | "INTERRUPTED" | "CANCELLED" | "REVOKED";
export type ReviewExecutionEvent = {
  sequence: number; state: ReviewExecutionState; at: string; deadline: string | null;
  outcome_unknown: boolean; reason: string | null; receipt_id: string | null;
};
export type PrepareReviewExecution = { request_id: string; expected_preparation_digest: string; acknowledge_unknown: boolean };
export type ReviewExecutionDetail = {
  contract: {
    execution_id: string; preparation_id: string; actor_id: string; created_at: string; mode: "OFFLINE_SYNTHETIC";
    specification: PrepareReviewExecution;
    prepared: { provider_name: string; model: string; provider_request: Record<string, unknown> };
  };
  events: ReviewExecutionEvent[]; current: ReviewExecutionEvent;
};
export type ReviewExecutionList = {
  enabled: boolean; mode: "OFFLINE_SYNTHETIC"; reason: string | null; preparation_digest: string; outcome_unknown_pending: boolean;
  items: ReviewExecutionDetail[]; page: number; pageSize: number; hasMore: boolean;
};
export type ReviewExecutionAction = "approve" | "cancel" | "revoke" | "recover" | "save_result";
export type ReviewExecutionControl = { request_id: string; expected_sequence: number; action: ReviewExecutionAction };
export type ReviewExecutionDispatch = { request_id: string; expected_sequence: number };
export const executionRoot = (root: string, preparation: string) => `${root}/${encodeURIComponent(preparation)}/executions`;
export const listReviewExecutions = (root: string, page: number) => fetchJson<ReviewExecutionList>(`${root}?page=${page}&pageSize=10`);
export const getReviewExecution = (root: string, id: string) => fetchJson<ReviewExecutionDetail>(`${root}/${encodeURIComponent(id)}`);
export const prepareReviewExecution = (root: string, body: PrepareReviewExecution) => fetchJson<ReviewExecutionDetail>(root, { method: "POST", body: JSON.stringify(body) });
export const controlReviewExecution = (root: string, id: string, body: ReviewExecutionControl) => fetchJson<ReviewExecutionDetail>(`${root}/${encodeURIComponent(id)}/controls`, { method: "POST", body: JSON.stringify(body) });
export const dispatchReviewExecution = (root: string, id: string, body: ReviewExecutionDispatch) => fetchJson<ReviewExecutionDetail>(`${root}/${encodeURIComponent(id)}/dispatch`, { method: "POST", body: JSON.stringify(body) });
