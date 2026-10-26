import { fetchJson } from "./api";
import { eventTypes } from "./temporalMemory";

export const metricIds = ["corpus.paper_count", "corpus.paper_version_count", "coverage.reviewed_paper_ratio", "coverage.full_text_available_ratio", "coverage.evidence_backed_paper_ratio", "coverage.rights_known_ratio", ...eventTypes.map(t => `delta.${t.toLowerCase()}_count`)];
export const insightKinds = ["CORPUS_SIZE_CHANGE", "REVIEW_COVERAGE_CHANGE", "FULL_TEXT_COVERAGE_CHANGE", "EVIDENCE_COVERAGE_CHANGE", "RIGHTS_KNOWN_COVERAGE_CHANGE", "PAPER_VERSION_ACTIVITY"];
export const defaultPolicy = {
 schema_version:"M3B_ANALYSIS_POLICY_V1",formula_registry_version:"M3B_METRIC_FORMULA_REGISTRY_V1",baseline_mode:"PREVIOUS_SNAPSHOT",
 enabled_metric_ids:metricIds,enabled_insight_kinds:insightKinds,minimum_paper_count:1,minimum_denominator:1,
 minimum_absolute_count_delta:1,minimum_percentage_point_delta_basis_points:100,factual_template_version:"M3B_FACTUAL_INSIGHT_TEMPLATE_V1",
};
export type PolicyConfig=typeof defaultPolicy;
export type Policy=PolicyConfig & {policy_id:string;topic_id:string;created_at:string;active?:boolean};
export type Page<T>={items:T[];total:number;page:number;limit:number};
export type Policies=Page<Policy> & {active_policy_id:string|null;active_policy?:Policy|null;activated_at:string|null};
export type MetricPoint={
 metric_point_id:string;metric_id:string;evaluation_id:string;topic_id:string;policy_id:string;source_snapshot_id:string;baseline_snapshot_id:string|null;
 state:"AVAILABLE"|"UNAVAILABLE"|"INSUFFICIENT_DENOMINATOR"|"INSUFFICIENT_BASELINE";reason_code:string;
 numerator:number|null;denominator:number|null;count_value:number|null;unit:"COUNT"|"RATIO";
 snapshot_created_at:string;calculated_at:string;formula_registry_version:string;formula_version:string;
 source_entity_count:number;source_entity_digest:string;source_evidence_count:number;source_evidence_digest:string;
};
export type Insight={
 insight_record_id:string;insight_kind:string;state:"ACCEPTED"|"UNAVAILABLE"|"INSUFFICIENT_DATA"|"BELOW_POLICY_THRESHOLD";
 reason_code:string;rendered_factual_text:string;source_snapshot_id:string;baseline_snapshot_id:string|null;
 policy_id:string;formula_registry_version:string;formula_version:string;template_version:string;
 metric_point_ids:string[];source_entity_ids:string[];source_evidence_ids:string[];source_entity_digest:string;source_evidence_digest:string;
 before_numerator:number|null;before_denominator:number|null;after_numerator:number|null;after_denominator:number|null;
 absolute_delta:number|null;percentage_point_delta_basis_points:number|null;calculated_at:string;confidence_class:string;
 paper_destinations:Array<{id:string;destination:string}>;evidence_destinations:Array<{id:string;destination:string}>;
 destination_reason?:string|null;
 unavailable_evidence_ids?:string[];
};
const root=(topic:string)=>`/api/topics/${encodeURIComponent(topic)}/analysis`;
export const listPolicies=(t:string,page=1)=>fetchJson<Policies>(`${root(t)}/policies?limit=25&page=${page}`);
export const createPolicy=(t:string,config:PolicyConfig)=>fetchJson<{policy:Policy;created:boolean}>(`${root(t)}/policies`,{method:"POST",body:JSON.stringify(config)});
export const activatePolicy=(t:string,policy_id:string)=>fetchJson<{policy:Policy}>(`${root(t)}/policies/active`,{method:"PUT",body:JSON.stringify({policy_id})});
export const evaluateSnapshot=(t:string,snapshot_id:string,policy_id?:string)=>fetchJson<{evaluation:{evaluation_id:string};created:boolean}>(`${root(t)}/evaluations`,{method:"POST",body:JSON.stringify({snapshot_id,policy_id})});
export const listSeries=(t:string,metric:string,page=1,snapshotId?:string)=>fetchJson<Page<MetricPoint>>(`${root(t)}/series?${new URLSearchParams({metric_id:metric,limit:"25",page:String(page),...(snapshotId?{snapshot_id:snapshotId}:{})})}`);
export const listInsights=(t:string,page=1,snapshotId?:string)=>fetchJson<Page<Insight>>(`${root(t)}/insights?${new URLSearchParams({limit:"25",page:String(page),...(snapshotId?{snapshot_id:snapshotId}:{})})}`);

export type InsightDecision = {
 decision_id:string;insight_record_id:string;topic_id:string;state:"accepted"|"held"|"rejected";
 note:string;previous_id:string|null;actor_id:string;created_at:string;schema_version:string;
};
export type DecisionHistory = Page<InsightDecision> & {latest_id:string|null};
export type DecisionInput = {request_id:string;state:InsightDecision["state"];note:string;expected_previous_id:string|null};
const decisionsRoot=(t:string,i:string)=>`${root(t)}/insights/${encodeURIComponent(i)}/decisions`;
export const listDecisions=(t:string,i:string,page=1)=>fetchJson<DecisionHistory>(`${decisionsRoot(t,i)}?limit=10&page=${page}`);
export const decideInsight=(t:string,i:string,body:DecisionInput)=>fetchJson<{decision:InsightDecision;created:boolean}>(decisionsRoot(t,i),{method:"POST",body:JSON.stringify(body)});
