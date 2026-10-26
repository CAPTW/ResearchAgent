import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import * as api from "../lib/temporalAnalysis";
import { listSnapshots, type SnapshotList } from "../lib/temporalMemory";

export type Resource<T>={phase:"loading"|"ready"|"error"|"unavailable";data:T|null;message?:string};
function useResource<T>(key:string,fetcher:()=>Promise<T>, enabled=true):Resource<T>{
 const [state,setState]=useState<Resource<T>>({phase:"loading",data:null});
 const fetchRef=useRef(fetcher);fetchRef.current=fetcher;
 const currentKey=useRef(key);currentKey.current=key;
 const stateKey=useRef(key);
 useEffect(()=>{let cancelled=false;stateKey.current=key;setState({phase:"loading",data:null});
  if(!enabled)return;
  void fetchRef.current().then(data=>{if(!cancelled&&currentKey.current===key)setState({phase:"ready",data});},error=>{
   if(!cancelled&&currentKey.current===key)setState({phase:error instanceof ApiError && [409,503].includes(error.status)?"unavailable":"error",data:null,message:error instanceof Error?error.message:"요청 실패"});
  });return()=>{cancelled=true;};
 },[key,enabled]);
 return stateKey.current===key?state:{phase:"loading",data:null};
}
export function useTemporalAnalysis(topic:string,snapshotResource?:Resource<SnapshotList>){
 const [revision,setRevision]=useState(0);
 const [metric,setMetricState]=useState(api.metricIds[0]);
 const [seriesPage,setSeriesPage]=useState(1),[insightPage,setInsightPage]=useState(1),[policyPage,setPolicyPage]=useState(1);
 const key=`${topic}:${revision}`;
 const policies=useResource(`${key}:policies:${policyPage}`,()=>api.listPolicies(topic,policyPage));
 const fallbackSnapshots=useResource(`${key}:snapshots`,()=>listSnapshots(topic),!snapshotResource);
 const snapshots=snapshotResource??fallbackSnapshots;
 const [selectedOnly,setSelectedOnly]=useState(false);
 const selectedId=snapshots.data?.items[0]?.snapshot_id;
 const scope=selectedOnly?selectedId:undefined;
 const scopeKey=`${selectedOnly}:${scope??""}`;
 // Changing the selected snapshot must never retain a late page from another scope.
 const [pageScope,setPageScope]=useState(scopeKey);
 const currentSeriesPage=pageScope===scopeKey?seriesPage:1;
 const currentInsightPage=pageScope===scopeKey?insightPage:1;
 useEffect(()=>{setPageScope(scopeKey);setSeriesPage(1);setInsightPage(1);},[scopeKey]);
 const canRead=!selectedOnly||(snapshots.phase==="ready"&&Boolean(selectedId));
 const series=useResource(`${key}:series:${metric}:${scopeKey}:${currentSeriesPage}`,()=>api.listSeries(topic,metric,currentSeriesPage,scope),canRead);
 const insights=useResource(`${key}:insights:${scopeKey}:${currentInsightPage}`,()=>api.listInsights(topic,currentInsightPage,scope),canRead);
 const [busy,setBusy]=useState<"create"|"activate"|"evaluate"|null>(null);
 const [message,setMessage]=useState("");const [error,setError]=useState("");
 const locked=useRef(false);const generation=useRef(0);const topicRef=useRef(topic);topicRef.current=topic;
 useEffect(()=>{generation.current++;locked.current=false;setBusy(null);setError("");setMessage("");setSeriesPage(1);setInsightPage(1);setPolicyPage(1);return()=>{generation.current++;};},[topic]);
 const refresh=useCallback(()=>setRevision(r=>r+1),[]);
 const mutate=async(kind:NonNullable<typeof busy>,operation:()=>Promise<string>)=>{
  if(locked.current)return;locked.current=true;const g=generation.current;setBusy(kind);setMessage("");setError("");
  try{const result=await operation();if(g===generation.current&&topicRef.current===topic){setMessage(result);refresh();}}
  catch(e){if(g===generation.current&&topicRef.current===topic)setError(`${kind}: ${e instanceof Error?e.message:"요청 실패"}`);}
  finally{if(g===generation.current&&topicRef.current===topic){locked.current=false;setBusy(null);}}
 };
 const create=(config:api.PolicyConfig)=>mutate("create",async()=>{const r=await api.createPolicy(topic,config);return r.created?"새 정책 버전을 저장했습니다. 활성화는 별도로 선택하세요.":"동일한 기존 정책 버전을 반환했습니다.";});
 const activate=(id:string)=>mutate("activate",async()=>{await api.activatePolicy(topic,id);return "정책을 활성화했습니다.";});
 const evaluate=()=>mutate("evaluate",async()=>{const snapshot=snapshots.data?.items[0];const policy=policies.data?.active_policy_id;if(snapshots.phase!=="ready"||!snapshot||policies.phase!=="ready"||!policy)throw new Error("스냅샷과 활성 정책을 확인해야 합니다.");const r=await api.evaluateSnapshot(topic,snapshot.snapshot_id,policy);return r.created?"분석 기록을 저장했습니다.":"기존 분석 기록을 반환했습니다.";});
 const setMetric=(value:string)=>{setSeriesPage(1);setMetricState(value);};
 return {policies,snapshots,series,insights,metric,setMetric,seriesPage:currentSeriesPage,setSeriesPage,insightPage:currentInsightPage,setInsightPage,policyPage,setPolicyPage,selectedOnly,setSelectedOnly,busy,message,error,refresh,create,activate,evaluate};
}
