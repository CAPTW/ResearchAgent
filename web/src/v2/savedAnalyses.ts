import { fetchJson } from "../lib/api";

export type AnalysisState = "needs_review" | "reviewed" | "rejected";
export type AnalysisCommand = { request_id: string; expected_previous_id: string | null; state: AnalysisState; note: string };
export type AnalysisDecision = { decision_id: string; state: AnalysisState; note: string; created_at: string; actor_id: string; command: AnalysisCommand };
export type SavedAnalysis = {
  analysis_id: string; topic_id: string; saved_at: string;
  bundle: { schema_version?: string; binding_sha256?: string; response_sha256: string; request_body_sha256: string; captured_at: string; plan: { contract: { model: string }; grant?: { trial_id: string; actor_id: string; origin: string; source: unknown } } };
  receipt: { receipt_id: string; topic_id: string; paper_id: string; run_id: string; input_origin: string; status: string;
    packet: { input_scope: unknown; source_extraction?: unknown; source_preparation?: unknown; evidence_spans: { span_id: string; text: string; locator?: unknown }[] };
    draft: Record<string, unknown>; validation: Record<string, unknown> };
  latest_decision: AnalysisDecision | null; decisions: AnalysisDecision[];
};
export type AnalysisList = { items: { analysis_id: string; paper_id: string; model: string; saved_at: string; latest_decision: AnalysisDecision | null }[]; total: number; page: number; limit: number };
export const analysisRoot = (topic: string) => `/api/topics/${encodeURIComponent(topic)}/saved-analyses`;
export const getAnalyses = (topic: string, page: number) => fetchJson<AnalysisList>(`${analysisRoot(topic)}?page=${page}`);
export const getAnalysis = (topic: string, id: string) => fetchJson<SavedAnalysis>(`${analysisRoot(topic)}/${encodeURIComponent(id)}`);
// Reserializing with JavaScript can turn 300.0 into 300 or round large integers,
// changing the frozen plan's hash. The server validates the original JSON text.
export const importAnalysis = (topic: string, bundleText: string) => fetchJson<SavedAnalysis>(analysisRoot(topic), { method: "POST", body: bundleText });
export const saveAnalysisReview = (topic: string, id: string, command: AnalysisCommand) => fetchJson<SavedAnalysis>(`${analysisRoot(topic)}/${encodeURIComponent(id)}/decisions`, { method: "POST", body: JSON.stringify(command) });

export type CapturedAnalysisSource = {
  trial_id: string; binding_sha256: string; response_sha256: string; model: string;
  origin: "PUBLIC_EXCERPT" | "FIXTURE_SYNTHETIC"; title: string; page: number;
  start: number; end: number; excerpt: string; captured_at: string;
};
export type CapturedAnalysisAvailability = { available: true; reason: null; source: CapturedAnalysisSource }
  | { available: false; reason: "anthropic_trial_disabled" | "anthropic_trial_analysis_unavailable" };
export type CapturedAnalysisCommand = { expected_binding_sha256: string; expected_response_sha256: string };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const nonempty = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const integer = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const timestamp = (value: unknown): value is string => nonempty(value) && Number.isFinite(Date.parse(value));
export function readCapturedAnalysisSource(value: unknown): CapturedAnalysisAvailability {
  if (object(value) && value.available === false && (value.reason === "anthropic_trial_disabled" || value.reason === "anthropic_trial_analysis_unavailable")) {
    return { available: false, reason: value.reason };
  }
  const source = object(value) && value.available === true && value.reason === null && object(value.source) ? value.source : null;
  if (!source || !nonempty(source.trial_id) || !hash(source.binding_sha256) || !hash(source.response_sha256)
    || !nonempty(source.model) || (source.origin !== "PUBLIC_EXCERPT" && source.origin !== "FIXTURE_SYNTHETIC")
    || !nonempty(source.title) || !integer(source.page) || source.page < 1 || !integer(source.start)
    || !integer(source.end) || source.end <= source.start || !nonempty(source.excerpt) || !timestamp(source.captured_at)) {
    throw new Error("Invalid captured analysis source");
  }
  return { available: true, reason: null, source: source as CapturedAnalysisSource };
}
const captureRoot = (topic: string) => `${analysisRoot(topic)}/sources/anthropic-trial`;
export const getCapturedAnalysisSource = async (topic: string) => readCapturedAnalysisSource(await fetchJson<unknown>(captureRoot(topic)));
export const saveCapturedAnalysis = (topic: string, command: CapturedAnalysisCommand) => fetchJson<unknown>(captureRoot(topic), {
  method: "POST", headers: { "Content-Type": "application/json", "X-XRML-Analysis-Save": "1" }, body: JSON.stringify(command),
});
export function readCapturedAnalysisAcknowledgement(value: unknown, topic: string, source: CapturedAnalysisSource): SavedAnalysis {
  if (!object(value) || value.topic_id !== topic || !nonempty(value.analysis_id) || !timestamp(value.saved_at)
    || !object(value.bundle) || !object(value.bundle.plan) || !object(value.bundle.plan.contract) || !object(value.bundle.plan.grant)
    || value.bundle.schema_version !== "SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1" || value.bundle.binding_sha256 !== source.binding_sha256
    || value.bundle.response_sha256 !== source.response_sha256 || value.bundle.captured_at !== source.captured_at
    || !hash(value.bundle.request_body_sha256) || value.bundle.plan.contract.model !== source.model
    || value.bundle.plan.grant.trial_id !== source.trial_id || value.bundle.plan.grant.origin !== source.origin
    || !object(value.receipt) || !nonempty(value.receipt.receipt_id) || !nonempty(value.receipt.paper_id)
    || !nonempty(value.receipt.topic_id) || !nonempty(value.receipt.run_id) || !nonempty(value.receipt.status)
    || value.receipt.input_origin !== (source.origin === "FIXTURE_SYNTHETIC" ? "FIXTURE_SYNTHETIC_CAPTURE" : "LOCAL_APP_CAPTURE")
    || !object(value.receipt.packet) || !Array.isArray(value.receipt.packet.evidence_spans)
    || !value.receipt.packet.evidence_spans.every(span => object(span) && nonempty(span.span_id) && typeof span.text === "string")
    || !object(value.receipt.draft) || !object(value.receipt.validation) || !Array.isArray(value.decisions)
    || !(value.latest_decision === null || object(value.latest_decision))) {
    throw new Error("Captured analysis acknowledgement mismatch");
  }
  return value as SavedAnalysis;
}
