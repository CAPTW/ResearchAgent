import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getApiTopicRecommendations, type ApiTopicRecommendation, type ApiTopicRecommendationsResponse } from "../../lib/api";
import { captureRecommendationVersion, decideRecommendation, listRecommendationDecisions, listRecommendationVersions, type RecommendationVersion } from "../../lib/recommendationVersions";
import type { DecisionInput, Page } from "../../lib/temporalAnalysis";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { RecommendationDetails } from "./RecommendationDetails";
import { ReviewDecisionPanel } from "./ReviewDecisionPanel";

function SavedVersion({version}:{version:RecommendationVersion}) {
 const {preferences}=useOptionalAppearance();
 const loadHistory=useCallback((page:number)=>listRecommendationDecisions(version.topic_id,version.version_id,page),[version.topic_id,version.version_id]);
 const saveDecision=useCallback((body:DecisionInput)=>decideRecommendation(version.topic_id,version.version_id,body),[version.topic_id,version.version_id]);
 return <article className="saved-recommendation" aria-label={`저장된 추천: ${version.content.display.title}`}>
  <p>저장: <time dateTime={version.created_at}>{formatTimestamp(version.created_at,preferences.timeFormat,"ko-KR")}</time> · {version.actor_id}</p>
  <p>버전: {version.version_id}</p><p>생성 규칙: {version.content.generator_version}</p>
  <RecommendationDetails historical recommendations={{topicId:version.topic_id,items:[version.content.display],newTopicCount:0,gapCount:0,coverageWarning:version.content.coverage_warning}}/>
  <ReviewDecisionPanel loadHistory={loadHistory} saveDecision={saveDecision} eligible
   explanation="이 판단은 이 저장 버전의 후속 검토 여부입니다. 신규성 검증이나 새 주제 생성·연구 실행을 뜻하지 않습니다."/>
 </article>;
}

export function RecommendationHistoryPanel({topicId,recommendations,onRecommendationsChange}:{topicId:string;recommendations:ApiTopicRecommendationsResponse|null;onRecommendationsChange?:(data:ApiTopicRecommendationsResponse)=>void}) {
 const [current,setCurrent]=useState(recommendations?.topicId===topicId?recommendations:null);
 const [history,setHistory]=useState<Page<RecommendationVersion>|null>(null);
 const [page,setPage]=useState(1);
 const [revision,setRevision]=useState(0);
 const [loading,setLoading]=useState(false);
 const [loadError,setLoadError]=useState("");
 const [error,setError]=useState("");
 const [message,setMessage]=useState("");
 const [busy,setBusy]=useState(false);
 const locked=useRef(false);
 const alive=useRef(true);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{setCurrent(recommendations?.topicId===topicId?recommendations:null);},[recommendations,topicId]);
 useEffect(()=>{
  let cancelled=false;setLoading(true);setLoadError("");setHistory(null);
  void listRecommendationVersions(topicId,page).then(data=>{if(!cancelled){setHistory(data);setLoading(false);}},e=>{if(!cancelled){setLoadError(String(e));setLoading(false);}});
  return()=>{cancelled=true;};
 },[topicId,page,revision]);
 const capture=async(item:ApiTopicRecommendation)=>{
  if(locked.current)return;locked.current=true;setBusy(true);setError("");setMessage("");
  try{
   const result=await captureRecommendationVersion(topicId,item);
   if(alive.current){setMessage(result.created?"표시된 추천 버전을 저장했습니다.":"이미 저장된 버전입니다.");setPage(1);setRevision(v=>v+1);}
  }catch(e){if(alive.current)setError(e instanceof ApiError&&e.status===409?"추천 내용이 변경되었습니다. 현재 추천을 새로고침해 근거를 확인한 뒤 저장하세요.":`버전 저장 실패: ${String(e)}. 다시 시도할 수 있습니다.`);}
  finally{if(alive.current){locked.current=false;setBusy(false);}}
 };
 const refreshCurrent=async()=>{
  if(locked.current)return;locked.current=true;setBusy(true);setError("");
  try{const data=await getApiTopicRecommendations(topicId);if(alive.current){setCurrent(data);onRecommendationsChange?.(data);setMessage("현재 추천을 새로고침했습니다.");}}
  catch(e){if(alive.current)setError(`현재 추천 새로고침 실패: ${String(e)}. 화면에는 이전 응답이 남아 있습니다.`);}
  finally{if(alive.current){locked.current=false;setBusy(false);}}
 };
 return <section aria-label="추천 버전과 검토 이력" className="recommendation-history">
  <p>추천을 저장하면 당시 내용과 근거가 보존됩니다. 로컬 추천 출력의 버전이며, 전체 논문 상태의 스냅샷은 아닙니다.</p>
  <button type="button" disabled={busy} onClick={()=>void refreshCurrent()}>현재 추천 새로고침</button>
  <RecommendationDetails recommendations={current} actions={item=>item.versionId?<button type="button" disabled={busy} onClick={()=>void capture(item)}>이 버전 저장</button>:<p>버전 식별자가 없어 저장할 수 없습니다. 현재 추천을 새로고침하세요.</p>}/>
  {error?<p role="alert">{error}</p>:null}{message?<p role="status">{message}</p>:null}
  <h3>저장된 추천 이력</h3>
  <p>현재 추천에서 사라진 후보도 보존됩니다. 저장된 근거 인용과 해시를 확인하며, 현재 원문으로 자동 연결하지 않습니다.</p>
  <button type="button" disabled={busy||loading} onClick={()=>setRevision(v=>v+1)}>추천 이력 새로고침</button>
  {loading?<p role="status">추천 이력 불러오는 중</p>:null}
  {loadError?<p role="alert">추천 이력 오류: {loadError}</p>:null}
  {history?<>
   <p>{history.total?`${history.total}개 저장 버전`:"저장된 추천 버전이 없습니다."}</p>
   {history.items.map(version=><SavedVersion key={version.version_id} version={version}/>)}
   <button type="button" disabled={busy||page<=1} onClick={()=>setPage(p=>p-1)}>이전 추천 이력</button>
   <span> {page} / {Math.max(1,Math.ceil(history.total/history.limit))} </span>
   <button type="button" disabled={busy||page*history.limit>=history.total} onClick={()=>setPage(p=>p+1)}>다음 추천 이력</button>
  </>:null}
 </section>;
}
