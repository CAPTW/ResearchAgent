import type { ReactNode } from "react";
import type { ApiTopicRecommendation, ApiTopicRecommendationsResponse } from "../../lib/api";

const confidenceLabels: Record<string,string> = {high:"높음",medium:"중간",low:"낮음"};
export function RecommendationDetails({ recommendations, actions, historical=false }: { recommendations: ApiTopicRecommendationsResponse|null; actions?:(item:ApiTopicRecommendation)=>ReactNode; historical?:boolean }) {
 if (!recommendations) return <p role="status">추천 자료를 사용할 수 없습니다.</p>;
 return <div className="recommendation-details">
  <p>{historical?"저장 당시 로컬 자료에서 도출한 후보입니다.":"현재 로컬 자료에서 도출한 후보입니다."} 연구 분야 전체의 신규성이나 합의를 평가하지 않습니다.</p>
  {recommendations.coverageWarning ? <p role="note">검토 범위: {recommendations.coverageWarning}</p> : null}
  {!recommendations.items.length ? <p>현재 자료에서 생성된 연구 공백·주제 후보가 없습니다.</p> : null}
  {recommendations.items.map(item => <details key={`${item.type}:${item.id}:${item.versionId || "current"}`}>
   <summary>{item.title} · {item.type === "gap" ? "연구 공백 후보" : "후속 주제 후보"} · 근거 신뢰도 {confidenceLabels[item.confidence] ?? "미평가"}</summary>
   <p>{item.gapStatement || item.question || item.whyNow}</p>
   {item.problemStatement ? <p>문제 정의: {item.problemStatement}</p> : null}
   {item.question && item.whyNow ? <p>검토 배경: {item.whyNow}</p> : null}
   {item.methodDirection ? <p>방법 방향: {item.methodDirection}</p> : null}
   {item.expectedContribution ? <p>기대 기여: {item.expectedContribution}</p> : null}
   {item.noveltyRationale ? <p>후보 도출 이유: {item.noveltyRationale}</p> : null}
   {item.differentiationClaim ? <p>차별성 가설: {item.differentiationClaim}</p> : null}
   {item.nearestExistingWork?.length ? <p>관련 기존 논문: {item.nearestExistingWork.join(", ")}</p> : null}
   {item.sourceBasis ? <p>도출 근거 유형: {item.sourceBasis}</p> : null}
   {item.coverageWarning ? <p>검토 범위: {item.coverageWarning}</p> : null}
   <p>근거 논문: {item.supportingPaperIds?.join(", ") || "식별자 미제공"}</p>
   <ul aria-label="추천 근거">{item.evidenceRefs?.map((ref,index) => <li key={index}>
    <p>{ref.quoted_or_summarized_basis}</p>
    <p>논문 {ref.paper_id} · 검토 수준 {ref.review_level} · 근거 {ref.evidence_span_id || "구간 식별자 없음"}</p>
    <p>원문 해시: {ref.source_text_sha256 || "미제공"}</p>
   </li>)}</ul>
   {!item.evidenceRefs?.length ? <p>상세 근거가 제공되지 않았습니다.</p> : null}
   {item.limitationRefs?.length ? <div><p>한계를 뒷받침하는 근거</p><ul>{item.limitationRefs.map((ref,index)=><li key={index}>{ref.quoted_or_summarized_basis} · {ref.paper_id} · {ref.evidence_span_id || "구간 식별자 없음"} · {ref.review_level} · {ref.source_text_sha256 || "원문 해시 미제공"}</li>)}</ul></div> : null}
   {item.riskNotes?.length ? <ul aria-label="추천 한계">{item.riskNotes.map((note,index) => <li key={index}>{note}</li>)}</ul> : null}
   {item.requiredFollowUpEvidence?.length ? <div><p>추가로 필요한 근거</p><ul>{item.requiredFollowUpEvidence.map((note,index) => <li key={index}>{note}</li>)}</ul></div> : null}
   {actions?.(item)}
  </details>)}
 </div>;
}
