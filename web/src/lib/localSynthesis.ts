import { fetchJson } from "./api";

export type ReviewInputSelection = {receipt_id:string;component_ids:string[]};
export type SynthesisSelection = {paper_version_id:string;metadata_digest:string;review_input?:ReviewInputSelection};
export type FrozenReviewReceipt = {
 receipt_id:string;recorded_at:string;input_origin:string;run_id:string;paper_id:string;review_level:string;
 source_text_sha256?:string|null;validation?:Record<string,unknown>;status?:string;
 packet:Record<string,unknown> & {source_preparation?:import("./reviewPreparation").ReviewPreparationBinding};draft:Record<string,unknown>;
};
export type FrozenReviewInput = {schema_version:"FROZEN_REVIEW_INPUT_V1";receipt:FrozenReviewReceipt;selected_component_ids:string[]};
export type JournalReviewInput = {
 schema_version:"JOURNAL_REVIEW_INPUT_V1"|"JOURNAL_REVIEW_INPUT_V2";paper_version_id:string;receipt_id:string;input_origin:string;run_id:string;paper_id:string;
 review_level:string;status:string;validation:Record<string,unknown>;selected_component_ids:string[];input_scope:unknown;source_extraction:unknown;review_result:Record<string,unknown>;
 source_preparation?:import("./reviewPreparation").ReviewPreparationBinding|null;
};
export const frozenCitationLabel=(kind:string)=>kind==="abstract"?"초록":kind==="review_excerpt"?"리뷰 발췌":kind==="review_input_excerpt"?"선택한 검토 입력 발췌":"유형 미확인";
export type FrozenExcerpt = {excerpt_id:string;source_evidence_id:string|null;recorded_content_digest:string|null;text:string;locator:unknown};
export type FrozenPaper = {paper_version_id:string;canonical_paper_id:string;metadata_digest:string;title:string;authors:string[];abstract:string;review_state:string;evidence:FrozenExcerpt[];rights:{state:string;label:string|null};source_identity:unknown;review_source_run_id:string|null;source_text_digest:string|null;review_input?:FrozenReviewInput};
export type SynthesisSummary = {job_id:string;state:"READY"|"PAUSED"|"CANDIDATE_READY";created_at:string;actor_id:string;completed_steps:number;total_steps:number;paper_count:number;source_snapshot_id:string;report_version_id?:string|null;review_state?:"accepted"|"held"|"rejected"|null};
export type SynthesisJob = Omit<SynthesisSummary,"paper_count"|"source_snapshot_id"> & {
 input_digest:string;
 inputs:{schema_version:string;topic_id:string;topic_label:string;source_snapshot_id:string;papers:FrozenPaper[];plan:{pipeline_version:string;shard_size:number;provider_mode:string}};
 steps:Array<{step_index:number;input_digest:string;output_digest:string;completed_at:string;output:{kind:string}}>;
 candidate:null|{schema_version:string;mode:string;input_digest:string;markdown:string;references:Array<FrozenExcerpt & {paper_version_id:string}>;coverage:{selected_papers:number;papers_with_abstract:number;papers_with_stored_excerpt:number;recorded_full_text_reviewed:number;contradiction_assessment:string;contradiction_count:null}};
};
const root=(topic:string)=>`/api/topics/${encodeURIComponent(topic)}/local-synthesis`;
export const captureSynthesis=(topic:string,selection:SynthesisSelection[],request_id:string)=>fetchJson<{job:SynthesisJob;created:boolean}>(root(topic),{method:"POST",body:JSON.stringify({selection,request_id})});
export const listSynthesis=(topic:string,page=1)=>fetchJson<{items:SynthesisSummary[];total:number;page:number;limit:number}>(`${root(topic)}?page=${page}&limit=10`);
export const getSynthesis=(topic:string,id:string)=>fetchJson<SynthesisJob>(`${root(topic)}/${encodeURIComponent(id)}`);
export const advanceSynthesis=(topic:string,id:string,expected_step:number)=>fetchJson<{job:SynthesisJob;advanced:boolean}>(`${root(topic)}/${encodeURIComponent(id)}/step`,{method:"POST",body:JSON.stringify({expected_step})});
export const pauseSynthesis=(topic:string,id:string,paused:boolean)=>fetchJson<SynthesisJob>(`${root(topic)}/${encodeURIComponent(id)}/pause`,{method:"POST",body:JSON.stringify({paused})});
