import { useCallback, useId, useState } from "react";
import { fetchJson, type ReportVersionDetailResponse } from "../../lib/api";
import type { DecisionInput } from "../../lib/temporalAnalysis";
import { ReviewDecisionPanel, type ReviewHistory } from "./ReviewDecisionPanel";

const reasons = {
 NOT_RECORDED:"이 버전은 인용 내용을 별도로 보존하지 않았습니다. 현재 인용으로 복원하지 않습니다.",
 UNBOUND_CONTENT:"저장 본문과 인용 개요의 일치를 확인하지 못해 이 버전에 인용을 연결하지 않았습니다.",
 OUTLINE_UNAVAILABLE:"저장 시 연결할 인용 개요가 없었습니다.",
 UNREADABLE:"보존된 인용 파일이 없거나 읽을 수 없습니다.",
 DIGEST_MISMATCH:"보존된 인용의 해시 또는 버전 연결이 일치하지 않습니다. 인용 내용을 표시하지 않습니다."
};

export function ReportEvidencePanel({detail}:{detail:ReportVersionDetailResponse}) {
 const id=useId();
 const [selected,setSelected]=useState(0);
 const citations=detail.citations;
 const status=citations?.status??"NOT_RECORDED";
 const items=status==="VERIFIED"?citations?.items??[]:[];
 const item=items[selected];
 const root=`/runs/${encodeURIComponent(detail.run_id)}/report/versions/${encodeURIComponent(detail.version!.version_id)}/decisions`;
 const loadHistory=useCallback((page:number)=>fetchJson<ReviewHistory>(`${root}?page=${page}&limit=10`),[root]);
 const saveDecision=useCallback((body:DecisionInput)=>fetchJson(root,{method:"POST",body:JSON.stringify({...body,expected_binding:detail.review_binding})}),[root,detail.review_binding]);
 return <>
  <details className="report-citations"><summary>이 버전에 보존된 인용</summary>
   <p>저장 시점의 주장·인용·발췌입니다. 원문 전체의 보관이나 연구 결론의 검증을 뜻하지 않습니다. 현재 원문으로 이동하는 링크는 제공하지 않습니다.</p>
   {status!=="VERIFIED"?<p role="note">{reasons[status]}</p>:<>
    <p>{items.length?`${items.length}개 보존된 인용` : "연결된 개요에 인용 항목이 없습니다."}</p>
    {citations?.warnings.map((w,n)=><p role="note" key={n}>{w}</p>)}
    {items.length?<label htmlFor={`${id}-citation`}>보존된 인용 선택<select id={`${id}-citation`} value={selected} onChange={e=>setSelected(Number(e.target.value))}>{items.map((c,n)=><option value={n} key={`${c.citation_id}:${n}`}>{c.section_title} · {c.paper_title||c.citation_text||c.citation_id}</option>)}</select></label>:null}
    {item?<article aria-label="보존된 인용 상세">
     <h4>{item.section_title}</h4><p>주장: {item.claim_text||"연결된 주장 없음"}</p>
     <p>인용: {item.citation_text||"인용 문구 없음"}</p><blockquote>{item.snippet||"발췌문이 보존되지 않았습니다."}</blockquote>
     <p>논문: {item.paper_title||item.paper_id||"기록 없음"}</p>
     <p>근거 ID: {item.evidence_id||"기록 없음"} · 출처 ID: {item.source_id||"기록 없음"}</p>
     <p>당시 출처 위치: {item.artifact_path||"기록 없음"}</p><small>{item.citation_id}</small>
    </article>:null}
   </>}
  </details>
  {detail.review_binding?<ReviewDecisionPanel loadHistory={loadHistory} saveDecision={saveDecision} eligible={Boolean(detail.review_eligible)}
   conflictHint="판단 또는 보고서 상태가 바뀌었습니다. ‘보고서 버전 새로고침’으로 본문·인용과 판단 이력을 확인한 뒤 다시 판단하세요."
   acceptedLabel="이 보고서 버전 채택" ineligibleReason="본문 또는 인용 무결성을 확인할 수 없어 채택할 수 없습니다. 보류·제외와 과거 판단 조회는 가능합니다."
   explanation="현재 선택한 저장 버전에 대한 사람의 검토 판단입니다. 인용 미보존 여부를 확인한 뒤 판단하세요. 채택은 과학적 타당성의 보증이나 공개·배포, Provider 후보의 승인을 뜻하지 않습니다."/>:null}
 </>;
}
