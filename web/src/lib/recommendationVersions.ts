import { fetchJson, type ApiTopicRecommendation } from "./api";
import type { DecisionInput, InsightDecision, Page } from "./temporalAnalysis";

export type RecommendationVersion = {
 version_id:string;topic_id:string;candidate_type:"gap"|"new_topic";candidate_id:string;created_at:string;actor_id:string;
 content:{schema_version:string;generator_version:string;coverage_warning:string|null;scope:string;global_novelty:string;
  candidate:Record<string,unknown>;display:ApiTopicRecommendation};
};
type RecommendationDecision=Omit<InsightDecision,"insight_record_id"> & {version_id:string};
const root=(topic:string)=>`/api/topics/${encodeURIComponent(topic)}/recommendation-versions`;
export const listRecommendationVersions=(topic:string,page=1)=>fetchJson<Page<RecommendationVersion>>(`${root(topic)}?limit=10&page=${page}`);
export const captureRecommendationVersion=(topic:string,item:ApiTopicRecommendation)=>fetchJson<{version:RecommendationVersion;created:boolean}>(root(topic),{
 method:"POST",body:JSON.stringify({candidate_type:item.type,candidate_id:item.id,expected_version_id:item.versionId}),
});
export const listRecommendationDecisions=(topic:string,id:string,page=1)=>fetchJson<Page<RecommendationDecision>&{latest_id:string|null}>(`${root(topic)}/${encodeURIComponent(id)}/decisions?limit=10&page=${page}`);
export const decideRecommendation=(topic:string,id:string,body:DecisionInput)=>fetchJson<{decision:RecommendationDecision;created:boolean}>(`${root(topic)}/${encodeURIComponent(id)}/decisions`,{method:"POST",body:JSON.stringify(body)});
