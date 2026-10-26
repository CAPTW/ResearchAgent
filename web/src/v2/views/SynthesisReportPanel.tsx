import { saveExport } from "../../lib/saveExport";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import type { DecisionInput } from "../../lib/temporalAnalysis";
import { ReviewDecisionPanel, type ReviewHistory } from "./ReviewDecisionPanel";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { LocalReportReader } from "./LocalReportReader";
import { SynthesisReportComparison } from "./SynthesisReportComparison";

type ReportDetail = {
 candidate_digest:string;
 version:null|{version_id:string;created_at:string;content:string;content_sha256:string;acceptance_decision_id:string};
 latest_decision:null|{decision_id:string;state:"accepted"|"held"|"rejected"};
 export_eligible:boolean;
};
const labels={accepted:"근거 보고서로 채택",held:"보류",rejected:"검토 대상에서 제외"};

export function SynthesisReportPanel({topicId,jobId,onChange}:{topicId:string;jobId:string;onChange?:()=>void}) {
 const {preferences}=useOptionalAppearance();
 const root=`/api/topics/${encodeURIComponent(topicId)}/local-synthesis/${encodeURIComponent(jobId)}`;
 const [report,setReport]=useState<ReportDetail|null>(null);
 const [error,setError]=useState("");
 const [exportError,setExportError]=useState("");
 const [exporting,setExporting]=useState(false);
 const [revision,setRevision]=useState(0);
 const [loading,setLoading]=useState(true);
 const [comparing,setComparing]=useState(false);
 const alive=useRef(true);
 const locked=useRef(false);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{
  let cancelled=false;setLoading(true);setError("");setExportError("");
  void fetchJson<ReportDetail>(`${root}/report`).then(value=>{if(!cancelled){setReport(value);setLoading(false);}},e=>{if(!cancelled){setError(String(e));setLoading(false);}});
  return()=>{cancelled=true;};
 },[root,revision]);
 const loadHistory=useCallback((page:number)=>fetchJson<ReviewHistory>(`${root}/decisions?page=${page}&limit=10`),[root]);
 const binding=report?.candidate_digest;
 const saveDecision=useCallback(async(body:DecisionInput)=>{
  const result=await fetchJson(`${root}/decisions`,{method:"POST",body:JSON.stringify({...body,expected_binding:binding})});
  if(alive.current){setRevision(v=>v+1);onChange?.();}
  return result;
 },[root,binding,onChange]);
 async function download(format:"markdown"|"json") {
  if(loading||error||!report?.export_eligible||!report.version||!report.latest_decision||locked.current)return;
  locked.current=true;setExporting(true);setExportError("");
  try {
   const query=new URLSearchParams({format,version_id:report.version.version_id,decision_id:report.latest_decision.decision_id});
   const result=await fetchJson<{content:string;filename:string;media_type:string}>(`${root}/report/export?${query}`);
   if(!alive.current)return;
   await saveExport(result.content, result.filename, result.media_type);
  }catch(e){if(alive.current)setExportError(`내보내기 실패: ${String(e)}. 보고서·판단을 새로고침하여 현재 채택 상태를 확인하세요.`);}
  finally{locked.current=false;if(alive.current)setExporting(false);}
 }
 return <section aria-label="로컬 근거 보고서 채택과 내보내기">
  <h4>근거 보고서 보존과 내보내기</h4>
  <p>검토 판단에서 채택하면 이 초안을 불변 로컬 보고서 버전으로 보존합니다. 입력·발췌와 과학적 미평가 상태는 그대로 유지됩니다.</p>
  <button type="button" disabled={exporting} onClick={()=>setRevision(v=>v+1)}>보고서·판단 새로고침</button>
  {error?<p role="alert">보고서 상태 오류: {error}. 아래의 이전 표시는 현재 상태로 확인되지 않았습니다.</p>:loading?<p role="status">보고서 상태 불러오는 중 · 이전 표시는 갱신 중입니다.</p>:null}
  {report?<>
   <p>현재 판단: {report.latest_decision?labels[report.latest_decision.state]:"판단 전"}</p>
   {report.version?<>
    <p>보존 버전: {report.version.version_id} · {formatTimestamp(report.version.created_at,preferences.timeFormat,"ko-KR")}</p>
    <details><summary>보존 보고서 읽기</summary><p>보존 원문의 제목·목록·인용을 구분해 표시합니다. 출처에 포함된 HTML·링크·이미지는 텍스트로 표시합니다.</p><LocalReportReader content={report.version.content}/></details>
    <details><summary>보존한 보고서 본문과 식별자</summary><p>본문 SHA-256: {report.version.content_sha256}</p><p>최초 채택: {report.version.acceptance_decision_id}</p><pre aria-label="보존한 로컬 근거 보고서">{report.version.content}</pre></details>
    <button type="button" aria-expanded={comparing} onClick={()=>setComparing(v=>!v)}>{comparing?"보고서 비교 닫기":"다른 보존 보고서와 비교"}</button>
    {comparing?<SynthesisReportComparison key={`${topicId}:${jobId}:${revision}`} topicId={topicId} jobId={jobId}/>:null}
   </>:<p>아직 채택하여 보존한 보고서 버전이 없습니다.</p>}
   <button type="button" disabled={loading||Boolean(error)||!report.export_eligible||exporting} onClick={()=>void download("markdown")}>Markdown 내보내기</button>
   <button type="button" disabled={loading||Boolean(error)||!report.export_eligible||exporting} onClick={()=>void download("json")}>근거 JSON 내보내기</button>
   <p>현재 채택된 버전만 내보냅니다. JSON에는 고정 입력·후보·참조 검증·판단 이력이 포함됩니다. 이미 내려받은 파일은 이후 판단을 바꾸어도 회수되지 않습니다.</p>
  </>:null}
  {exportError?<p role="alert">{exportError}</p>:null}
  {binding?<ReviewDecisionPanel loadHistory={loadHistory} saveDecision={saveDecision} eligible={Boolean(report)&&!loading&&!error} ineligibleReason="보고서 상태를 확인한 뒤 채택할 수 있습니다. 새로고침하세요."
   acceptedLabel="근거 보고서로 채택" explanation="이 고정 초안의 보존·활용에 대한 사람의 판단입니다. 채택은 연구 결론 검증, Provider 실행 승인 또는 공개·배포를 뜻하지 않습니다. 보류·제외로 바꾸어도 이미 보존된 버전과 이력은 남습니다."/>:null}
 </section>;
}
