import { fetchJson } from "./api";
import type { PreparationSource, ReviewPreparationRecord } from "./reviewPreparation";
import type { LocalModelCapability } from "./modelConnections";

export type TokenBudget = { provider_name: string; model: string; context_tokens: number; input_tokens: number; max_output_tokens: number; safety_tokens: number; estimator: "UTF8_BYTES_CONSERVATIVE_V1" };
export type AnalysisUnit = { schema_version: "PAPER_ANALYSIS_UNIT_V1"; plan_id: string; plan_sha256: string; unit_index: number; unit_count: number; label: string; document_chars: number; outline: {label: string; start: number; end: number}[]; context_before: string; context_after: string; budget: TokenBudget };
export type DocumentPlanRequest = { request_id: string; run_id: string; paper_id: string; expected_source_digest: string; connector_id: string; model: string; context_tokens: number; input_tokens: number; max_output_tokens: number; safety_tokens: number };
type DocumentPlanFields = {
  plan_id: string; plan_sha256: string; created_at: string; source: PreparationSource;
  topic_id: string; paper_version_id: string; actor_id: string; request_id: string;
  budget: TokenBudget; budget_source: string; mode: "WHOLE_EXTRACTED_TEXT" | "SECTION_UNITS"; full_document_review: "NOT_ESTABLISHED";
  outline: { label: string; start: number; end: number }[];
  units: { index: number; start: number; end: number; label: string; estimated_input_tokens: number; preparation_id: string | null; expected_preparation_id: string; state: string;
    receipts: {receipt_id: string; input_origin: string; validated_response: boolean; status: string}[] }[];
};
export type DocumentPlan = DocumentPlanFields & (
  { schema_version: "PAPER_DOCUMENT_PLAN_V1"; local_capability?: never }
  | { schema_version: "PAPER_DOCUMENT_PLAN_V2"; local_capability: LocalModelCapability }
);
export type PlanConfiguration = { default_connector_id: string; max_output_tokens: number; safety_tokens: number;
  choices: {connector_id: string; display_name: string; provider_name: string; model: string; context_tokens: number | null; budget_source: string; docs_url: string;
    plan_schema_version?: "PAPER_DOCUMENT_PLAN_V1" | "PAPER_DOCUMENT_PLAN_V2"; local_capability?: LocalModelCapability | null; max_context_tokens?: number}[] };
export const planRoot = (root: string) => `${root}/document-plans`;
const object = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
function localCapability(value: unknown): value is LocalModelCapability {
  return object(value) && value.schema_version === "OLLAMA_LOCAL_CAPABILITY_V1" && value.connector_id === "ollama_local"
    && value.provider_name === "ollama" && value.protocol_schema === "OLLAMA_PREPARED_REVIEW_WIRE_V1"
    && value.synthetic_protocol_available === true && value.document_planning_available === true && value.execution_available === false
    && value.locality === "LOCALITY_NOT_VERIFIED" && value.runtime_presence === "NOT_CHECKED"
    && value.model_identity === "NOT_VERIFIED" && value.context_capacity === "NOT_VERIFIED" && value.context_request_limit_tokens === 1000000;
}
function scope(root: string) {
  const match = root.match(/^\/api\/topics\/([^/]+)\/paper-bank\/([^/]+)\/review-preparations$/);
  if (!match) throw new Error("document_plan_scope_invalid");
  return {topic: decodeURIComponent(match[1]), version: decodeURIComponent(match[2])};
}
function readPlan(value: unknown, root: string, expected?: DocumentPlanRequest, identity?: string): DocumentPlan {
  const target = scope(root);
  if (!object(value) || !["PAPER_DOCUMENT_PLAN_V1", "PAPER_DOCUMENT_PLAN_V2"].includes(value.schema_version as string) || value.topic_id !== target.topic || value.paper_version_id !== target.version
      || typeof value.actor_id !== "string" || typeof value.created_at !== "string" || !Number.isFinite(Date.parse(value.created_at))
      || typeof value.plan_id !== "string" || !/^rplan_[a-f0-9]{64}$/.test(value.plan_id) || (identity && value.plan_id !== identity)
      || typeof value.plan_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.plan_sha256)
      || !object(value.source) || !integer(value.source.source_chars) || !object(value.budget)
      || value.budget.estimator !== "UTF8_BYTES_CONSERVATIVE_V1" || value.full_document_review !== "NOT_ESTABLISHED"
      || !Array.isArray(value.units) || value.units.length < 1 || value.units.length > 256 || !Array.isArray(value.outline)
      || value.mode !== (value.units.length === 1 ? "WHOLE_EXTRACTED_TEXT" : "SECTION_UNITS")) throw new Error("document_plan_response_invalid");
  const budget = value.budget;
  if (typeof budget.model !== "string" || typeof budget.provider_name !== "string"
      || !integer(budget.context_tokens) || !integer(budget.input_tokens) || !integer(budget.max_output_tokens) || !integer(budget.safety_tokens)
      || budget.input_tokens + budget.max_output_tokens + budget.safety_tokens > budget.context_tokens) throw new Error("document_plan_budget_invalid");
  if (value.schema_version === "PAPER_DOCUMENT_PLAN_V2") {
    if (!localCapability(value.local_capability) || budget.provider_name !== "ollama" || budget.context_tokens > value.local_capability.context_request_limit_tokens)
      throw new Error("document_plan_local_capability_invalid");
  } else if (budget.provider_name === "ollama" || value.local_capability !== undefined) throw new Error("document_plan_local_capability_invalid");
  let cursor = 0;
  for (const [index, unit] of value.units.entries()) {
    if (!object(unit) || unit.index !== index || unit.start !== cursor || !integer(unit.end) || unit.end <= cursor
        || !integer(unit.estimated_input_tokens) || unit.estimated_input_tokens > budget.input_tokens
        || typeof unit.label !== "string" || typeof unit.expected_preparation_id !== "string" || !/^rprep_[a-f0-9]{64}$/.test(unit.expected_preparation_id)
        || (unit.preparation_id !== null && unit.preparation_id !== unit.expected_preparation_id)
        || !Array.isArray(unit.receipts) || !unit.receipts.every(r => object(r) && typeof r.receipt_id === "string" && typeof r.validated_response === "boolean")) throw new Error("document_plan_units_invalid");
    cursor = unit.end;
  }
  if (cursor !== value.source.source_chars) throw new Error("document_plan_coverage_invalid");
  if (expected && (value.request_id !== expected.request_id || value.source.run_id !== expected.run_id || value.source.paper_id !== expected.paper_id || value.source.source_digest !== expected.expected_source_digest
      || ["model", "context_tokens", "input_tokens", "max_output_tokens", "safety_tokens"].some(k => budget[k] !== expected[k as keyof DocumentPlanRequest]))) throw new Error("document_plan_response_mismatch");
  if (expected && (expected.connector_id === "ollama_local") !== (value.schema_version === "PAPER_DOCUMENT_PLAN_V2")) throw new Error("document_plan_response_mismatch");
  return value as DocumentPlan;
}
export async function getPlanConfiguration(root: string) {
  const value = await fetchJson<PlanConfiguration>(`${planRoot(root)}/configuration`);
  for (const choice of value.choices) {
    if (choice.connector_id === "ollama_local" && (choice.provider_name !== "ollama" || choice.plan_schema_version !== "PAPER_DOCUMENT_PLAN_V2"
        || !localCapability(choice.local_capability) || choice.context_tokens !== null || choice.budget_source !== "USER_REQUIRED" || choice.max_context_tokens !== 1000000))
      throw new Error("document_plan_local_configuration_invalid");
  }
  return value;
}
export async function listDocumentPlans(root: string, page: number) {
  const value = await fetchJson<unknown>(`${planRoot(root)}?page=${page}`);
  if (!object(value) || !Array.isArray(value.items) || typeof value.hasMore !== "boolean") throw new Error("document_plan_history_invalid");
  return {items: value.items.map(p => readPlan(p, root)), hasMore: value.hasMore};
}
export const getDocumentPlan = async (root: string, id: string) => readPlan(await fetchJson<unknown>(`${planRoot(root)}/${encodeURIComponent(id)}`), root, undefined, id);
export const saveDocumentPlan = async (root: string, body: DocumentPlanRequest) => readPlan(await fetchJson<unknown>(planRoot(root), { method: "POST", body: JSON.stringify(body) }), root, body);
export async function prepareDocumentUnit(root: string, plan: DocumentPlan, index: number) {
  const value = await fetchJson<ReviewPreparationRecord>(`${planRoot(root)}/${encodeURIComponent(plan.plan_id)}/units/${index}/prepare`, {method: "POST"});
  const unit = plan.units[index];
  if (!object(value) || value.schema_version !== "PAPER_REVIEW_PREPARATION_V3" || !unit || value.topic_id !== plan.topic_id || value.paper_version_id !== plan.paper_version_id
      || value.actor_id !== plan.actor_id || value.state !== "PREPARED_NOT_EXECUTED" || !object(value.execution) || value.execution.started !== false
      || !object(value.source) || value.source.source_digest !== plan.source.source_digest || value.source.run_id !== plan.source.run_id || value.source.paper_id !== plan.source.paper_id
      || !object(value.selection) || value.selection.start !== unit.start || value.selection.end !== unit.end || typeof value.selection.text !== "string"
      || Array.from(value.selection.text).length !== unit.end - unit.start || !object(value.analysis_unit) || value.analysis_unit.plan_id !== plan.plan_id
      || value.analysis_unit.plan_sha256 !== plan.plan_sha256 || value.analysis_unit.unit_index !== index || value.analysis_unit.unit_count !== plan.units.length
      || !object(value.analysis_unit.budget) || Object.entries(plan.budget).some(([k, v]) => (value.analysis_unit.budget as unknown as Record<string, unknown>)[k] !== v)
      || value.preparation_id !== unit.expected_preparation_id) throw new Error("document_plan_unit_response_mismatch");
  return value;
}
