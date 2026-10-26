import { useEffect, useId, useRef, useState } from "react";
import { advanceSynthesis, getSynthesis, listSynthesis, pauseSynthesis, type SynthesisJob } from "../../lib/localSynthesis";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { SynthesisReportPanel } from "./SynthesisReportPanel";
import { LocalJournalPanel } from "./LocalJournalPanel";
import { JournalExchangePanel } from "./JournalExchangePanel";
import { JournalExecutionResults } from "./JournalExecutionResults";
import { FrozenReviewInputView } from "../components/ReviewInputHistory";

const states={READY:"수동 진행 가능",PAUSED:"일시 중지",CANDIDATE_READY:"근거 초안 준비됨"};
const stages:Record<string,string>={EXTRACT:"발췌 묶음",ASSEMBLE:"초안 조립",VALIDATE:"고정 입력 참조 확인"};
const reviews={accepted:"근거 보고서 채택",held:"보류",rejected:"검토 제외"};

function SynthesisDetail({topicId,jobId,onChange}:{topicId:string;jobId:string;onChange:()=>void}) {
 const id=useId();
 const {preferences}=useOptionalAppearance();
 const [job,setJob]=useState<SynthesisJob|null>(null);
 const [error,setError]=useState("");
 const [busy,setBusy]=useState(false);
 const [refresh,setRefresh]=useState(0);
 const alive=useRef(true);
 const locked=useRef(false);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{
  let cancelled=false;setJob(null);setError("");
  void getSynthesis(topicId,jobId).then(data=>{if(!cancelled)setJob(data);},e=>{if(!cancelled)setError(String(e));});
  return()=>{cancelled=true;};
 },[topicId,jobId,refresh]);
 async function act(operation:"step"|"pause"|"resume") {
  if(!job||locked.current)return;
  locked.current=true;setBusy(true);setError("");
  try {
   const next=operation==="step"?(await advanceSynthesis(topicId,jobId,job.completed_steps)).job:await pauseSynthesis(topicId,jobId,operation==="pause");
   if(alive.current){setJob(next);onChange();}
  }catch(e){if(alive.current)setError(`${String(e)} · 저장된 단계는 유지됩니다. 새로고침하거나 같은 단계를 다시 요청하세요.`);}
  finally{if(alive.current){locked.current=false;setBusy(false);}}
 }
 return <article className="synthesis-detail" aria-label="고정 입력과 진행 상태">
  <button type="button" disabled={busy} onClick={()=>setRefresh(v=>v+1)}>고정 초안 새로고침</button>
  {error?<p role="alert">{error}</p>:null}
  {!job&&!error?<p role="status">고정 입력 불러오는 중</p>:null}
  {job?<>
   <h4>{states[job.state]} · {job.completed_steps} / {job.total_steps} 단계</h4>
   <p>한 번에 최대 {job.inputs.plan.shard_size}편씩 처리하고 완료 단계를 저장합니다. 창을 닫으면 다음 단계는 실행되지 않습니다.</p>
   <p>이 초안은 로컬 초록·리뷰 발췌 정리입니다. AI 해석, 원문 전체 검토와 모순 평가는 수행하지 않습니다.</p>
   {job.state==="READY"?<><button type="button" disabled={busy} onClick={()=>void act("step")}>{busy?"단계 실행 중":"다음 한 단계 실행"}</button><button type="button" disabled={busy} onClick={()=>void act("pause")}>진행 일시 중지</button></>:null}
   {job.state==="PAUSED"?<button type="button" disabled={busy} onClick={()=>void act("resume")}>수동 진행 재개</button>:null}
   <details><summary>고정 입력 {job.inputs.papers.length}편과 근거</summary>
    <p>작업: {job.job_id}</p><p>입력 SHA-256: {job.input_digest}</p><p>주제 스냅샷: {job.inputs.source_snapshot_id}</p>
    <p>처리 규칙: {job.inputs.plan.pipeline_version}</p><p>저장: {formatTimestamp(job.created_at,preferences.timeFormat,"ko-KR")}</p>
    {job.inputs.papers.map(p=><details key={p.paper_version_id}><summary>{p.title}</summary>
     <p>버전: {p.paper_version_id}</p><p>당시 검토 상태: {p.review_state} · 권리 상태: {p.rights.label||p.rights.state}</p>
     <p>저자: {p.authors.join(", ")||"기록 없음"}</p><p>출처 식별자: {JSON.stringify(p.source_identity)}</p>
     <p>리뷰 실행: {p.review_source_run_id||"기록 없음"} · 원문 해시: {p.source_text_digest||"기록 없음"}</p>
     <p>메타데이터 해시: {p.metadata_digest}</p><h5>저장된 초록</h5><blockquote>{p.abstract||"보존된 초록 없음"}</blockquote>
     <h5>저장된 리뷰 근거 {p.evidence.length}개</h5>{p.evidence.map(e=><div key={e.excerpt_id}><blockquote>{e.text||"발췌문 없음"}</blockquote><p>고정 근거: {e.excerpt_id}</p><p>원래 근거 ID: {e.source_evidence_id||"기록 없음"} · 위치: {e.locator?JSON.stringify(e.locator):"기록 없음"}</p></div>)}
     {p.review_input?.schema_version==="FROZEN_REVIEW_INPUT_V1"?<FrozenReviewInputView value={p.review_input}/>:null}
    </details>)}
   </details>
   <details><summary>완료 단계와 체크포인트</summary><ol>{job.steps.map(s=><li key={s.step_index}>{stages[s.output.kind]||s.output.kind} · {formatTimestamp(s.completed_at,preferences.timeFormat,"ko-KR")}<p>{s.output_digest}</p></li>)}</ol></details>
   {job.candidate?<section aria-labelledby={`${id}-draft`}><h4 id={`${id}-draft`}>검토할 근거 초안</h4>
    <p>선택 {job.candidate.coverage.selected_papers}편 · 초록 보존 {job.candidate.coverage.papers_with_abstract}편 · 리뷰 발췌 보존 {job.candidate.coverage.papers_with_stored_excerpt}편</p>
    <p>모순 평가: 미평가. 검토 판단에서 채택하여 로컬 근거 보고서로 보존할 수 있습니다.</p>
    <SynthesisReportPanel key={job.job_id} topicId={topicId} jobId={job.job_id} onChange={onChange}/>
    <LocalJournalPanel key={`journal:${job.job_id}`} topicId={topicId} jobId={job.job_id}/>
    <JournalExchangePanel key={`exchange:${job.job_id}`} topicId={topicId} jobId={job.job_id}/>
    <JournalExecutionResults key={`execution:${job.job_id}`} topicId={topicId} jobId={job.job_id} inputDigest={job.input_digest}/>
    <pre aria-label="고정 입력으로 만든 근거 초안">{job.candidate.markdown}</pre>
   </section>:null}
  </>:null}
 </article>;
}

export function LocalSynthesisPanel({topicId,initialJobId,onSelectJob}:{topicId:string;initialJobId?:string;onSelectJob?:(id:string)=>void}) {
 const {preferences}=useOptionalAppearance();
 const [listing,setListing]=useState<Awaited<ReturnType<typeof listSynthesis>>|null>(null);
 const [page,setPage]=useState(1);
 const [selected,setSelected]=useState(initialJobId||"");
 const [error,setError]=useState("");
 const [revision,setRevision]=useState(0);
 useEffect(()=>{if(initialJobId)setSelected(initialJobId);},[initialJobId]);
 useEffect(()=>{
  let cancelled=false;setListing(null);setError("");
  void listSynthesis(topicId,page).then(data=>{if(!cancelled)setListing(data);},e=>{if(!cancelled)setError(String(e));});
  return()=>{cancelled=true;};
 },[topicId,page,revision]);
 return <section className="report-version-panel" aria-label="선택 문헌 근거 초안">
  <h3>선택 문헌 근거 초안</h3><p>논문 탭에서 문헌을 선택해 입력을 고정하면 여기서 이어서 처리할 수 있습니다. 최대 100편, 고정 입력 2 MB를 지원합니다.</p>
  <button type="button" onClick={()=>setRevision(v=>v+1)}>근거 초안 목록 새로고침</button>
  {error?<p role="alert">초안 목록 오류: {error}</p>:!listing?<p role="status">초안 목록 불러오는 중</p>:null}
  {listing?.total===0?<p>고정된 보고서 입력이 없습니다.</p>:null}
  {listing?<><ul>{listing.items.map(j=><li key={j.job_id}><button type="button" aria-pressed={selected===j.job_id} onClick={()=>{setSelected(j.job_id);onSelectJob?.(j.job_id);}}>{j.paper_count}편 · {formatTimestamp(j.created_at,preferences.timeFormat,"ko-KR")} · {states[j.state]}{j.review_state?` · ${reviews[j.review_state]}`:""}{j.report_version_id?" · 보존 버전 있음":""}</button></li>)}</ul>
   <button type="button" disabled={page<=1} onClick={()=>setPage(v=>v-1)}>이전 초안 목록</button><span> {page} / {Math.max(1,Math.ceil(listing.total/listing.limit))} </span><button type="button" disabled={page*listing.limit>=listing.total} onClick={()=>setPage(v=>v+1)}>다음 초안 목록</button>
  </>:null}
  {selected?<SynthesisDetail key={`${topicId}:${selected}`} topicId={topicId} jobId={selected} onChange={()=>setRevision(v=>v+1)}/>:null}
 </section>;
}
