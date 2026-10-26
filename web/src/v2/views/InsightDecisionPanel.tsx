import { useCallback } from "react";
import { decideInsight, listDecisions, type DecisionInput, type Insight } from "../../lib/temporalAnalysis";
import { ReviewDecisionPanel } from "./ReviewDecisionPanel";

export function InsightDecisionPanel({topicId, insight}: {topicId:string;insight:Insight}) {
 const loadHistory=useCallback((page:number)=>listDecisions(topicId,insight.insight_record_id,page),[topicId,insight.insight_record_id]);
 const saveDecision=useCallback((body:DecisionInput)=>decideInsight(topicId,insight.insight_record_id,body),[topicId,insight.insight_record_id]);
 return <ReviewDecisionPanel key={`${topicId}:${insight.insight_record_id}`} loadHistory={loadHistory} saveDecision={saveDecision}
  eligible={insight.state==="ACCEPTED"} explanation="이 판단은 후속 검토 여부를 기록합니다. 측정값이나 연구 결과의 사실성을 바꾸지 않습니다."/>;
}
