import { useState } from "react";
import { InsightDecisionPanel } from "./InsightDecisionPanel";
import { Link } from "react-router-dom";
import { defaultPolicy, metricIds, insightKinds, type MetricPoint, type PolicyConfig, type Page } from "../../lib/temporalAnalysis";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { useTemporalAnalysis, type Resource } from "../useTemporalAnalysis";
import { V2Button } from "../V2Primitives";
import type { SnapshotList } from "../../lib/temporalMemory";

const metricLabels = ["논문 수 (Paper count)", "논문 버전 수", "검토 coverage", "전문 coverage", "근거 coverage", "권리 확인 coverage", "논문 추가", "논문 제거", "버전 변경", "컬렉션 변경", "검토 상태 변경", "전문 상태 변경", "근거 변경", "권리 상태 변경"];
const thresholds = [
 ["minimum_paper_count", "최소 논문 수", 1000000], ["minimum_denominator", "최소 분모", 1000000],
 ["minimum_absolute_count_delta", "최소 개수 변화", 1000000], ["minimum_percentage_point_delta_basis_points", "최소 비율 변화 (basis points)", 10000],
] as const;

function ResourceState({ resource, label }: {resource: Resource<unknown>; label:string}) {
 if (resource.phase === "loading") return <p role="status">{label} 불러오는 중입니다.</p>;
 if (resource.phase === "error" || resource.phase === "unavailable") return <p role="alert">{label} {resource.phase === "unavailable" ? "사용 불가" : "오류"}: {resource.message}</p>;
 return null;
}
function Pages({ label, data, page, setPage }: {label:string;data:Page<unknown>|null;page:number;setPage:(page:number)=>void}) {
 if (!data || data.total <= 25) return null;
 return <nav aria-label={label}>
  <V2Button disabled={page <= 1} onClick={()=>setPage(page-1)}>이전 {label}</V2Button>
  <span> {page} / {Math.ceil(data.total/25)} </span>
  <V2Button disabled={page*25 >= data.total} onClick={()=>setPage(page+1)}>다음 {label}</V2Button>
 </nav>;
}
function exact(point:MetricPoint) {
 if(point.state!=="AVAILABLE") return "—";
 if(point.unit==="COUNT")return String(point.count_value);
 return point.numerator!==null && point.denominator ? `${point.numerator}/${point.denominator} (${(100*point.numerator/point.denominator).toFixed(2)}%)` : "—";
}
function SeriesPlot({points,label}:{points:MetricPoint[];label:string}) {
 const values=points.map(p=>p.state==="AVAILABLE"?(p.unit==="COUNT"?p.count_value:p.denominator&&p.numerator!==null?p.numerator/p.denominator:null):null);
 const maximum=Math.max(1,...values.filter((v):v is number=>typeof v==="number"));
 return <svg role="img" aria-label={`${label} 스냅샷 측정점`} viewBox="0 0 640 180" style={{width:"100%",maxHeight:180,color:"var(--text-secondary)"}}>
  <title>{label} — 저장된 스냅샷 측정점</title>
  <desc>누락된 값을 연결하거나 보간하지 않습니다. 가로축은 이 정책 안에서의 스냅샷 순서입니다. 각 점의 시간, 상태, 정확한 값은 아래 표의 같은 정책에서 확인할 수 있습니다.</desc>
  <line x1="35" x2="615" y1="145" y2="145" stroke="currentColor"/>
  <text x="4" y="145" fill="currentColor" fontSize="11">0</text>
  <text x="4" y="20" fill="currentColor" fontSize="11">{maximum.toFixed(1)}</text>
  {points.map((p,index)=>{const x=45+index*550/Math.max(points.length-1,1);const v=values[index];return <g key={p.metric_point_id}>
   {typeof v==="number"?<circle cx={x} cy={145-v/maximum*120} r="5" fill="var(--accent-primary, var(--chart-series-1))"><title>{p.source_snapshot_id}: {exact(p)}</title></circle>:<text x={x} y="135" fill="currentColor" textAnchor="middle">×<title>{p.state}</title></text>}
   <text x={x} y="168" textAnchor="middle" fill="currentColor" fontSize="10">{index+1}</text>
  </g>;})}
 </svg>;
}

export function TemporalAnalysisPanel({topicId,snapshotResource}:{topicId:string;snapshotResource?:Resource<SnapshotList>}) {
 const s=useTemporalAnalysis(topicId,snapshotResource);
 const {preferences}=useOptionalAppearance();
 const format=(date:string)=>formatTimestamp(date,preferences.timeFormat??"locale","ko-KR");
 const [config,setConfig]=useState<PolicyConfig>(()=>({...defaultPolicy}));
 const [formError,setFormError]=useState("");
 const latest=s.snapshots.data?.items[0];
 const active=s.policies.data?.active_policy ?? s.policies.data?.items.find(p=>p.policy_id===s.policies.data?.active_policy_id);
 const noPolicy=s.policies.phase==="ready"&&!s.policies.data?.active_policy_id;
 const evaluateReason=s.snapshots.phase!=="ready"?"스냅샷 목록을 확인해야 합니다.":!latest?"분석할 스냅샷이 없습니다.":s.policies.phase!=="ready"?"정책 목록을 확인해야 합니다.":noPolicy?"활성 정책이 필요합니다.":"";
 const toggle=(key:"enabled_metric_ids"|"enabled_insight_kinds",id:string)=>setConfig(c=>({...c,[key]:c[key].includes(id)?c[key].filter(v=>v!==id):[...c[key],id]}));
 const submit=()=>{
  if(thresholds.some(([key,,maximum])=>!Number.isInteger(config[key])||config[key]<0||config[key]>maximum)){setFormError("임계값은 표시된 범위의 정수여야 합니다.");return;}
  setFormError("");void s.create(config);
 };
 return <section className="v2-panel v2-temporal-panel" aria-labelledby="temporal-analysis-heading" style={{minWidth:0,overflowWrap:"anywhere"}}>
  <h2 id="temporal-analysis-heading">스냅샷 기반 분석</h2>
  <p>직접 캡처한 로컬 상태의 개수와 coverage를 측정합니다. 분석은 아래 버튼을 누를 때만 실행됩니다.</p>
  <V2Button disabled={Boolean(s.busy)} onClick={s.refresh}>분석 자료 새로고침</V2Button>
  <p role="status" aria-live="polite">{s.busy ? `${s.busy === "create" ? "정책 생성" : s.busy === "activate" ? "정책 활성화" : "분석 저장"} 중입니다.` : s.message}</p>
  {s.error?<p role="alert">{s.error}</p>:null}
  <section aria-labelledby="analysis-policy-heading">
   <h3 id="analysis-policy-heading">분석 정책</h3>
   <ResourceState resource={s.policies} label="정책 목록"/>
   {noPolicy?<p>활성 정책 없음</p>:null}
   {active?<div><p>활성 정책: {active.policy_id}</p><p>공식: {active.formula_registry_version}</p>
    <p>최소 논문 {active.minimum_paper_count} · 최소 분모 {active.minimum_denominator} · 개수 변화 {active.minimum_absolute_count_delta} · 비율 변화 {active.minimum_percentage_point_delta_basis_points} bp</p>
    <details><summary>활성 정책의 지표와 기록 유형</summary><p>{active.enabled_metric_ids.join(", ")}</p><p>{active.enabled_insight_kinds.join(", ")}</p><p>{active.baseline_mode} · {active.factual_template_version}</p></details>
   </div>:null}
   {s.policies.phase==="ready"&&s.policies.data?.total===0?<V2Button disabled={Boolean(s.busy)} aria-busy={s.busy==="create"} onClick={()=>void s.create(defaultPolicy)}>권장 기본 정책 만들기</V2Button>:null}
   {s.policies.data?.items.length?<details open><summary>정책 버전 이력</summary><ul>{s.policies.data.items.map(p=><li key={p.policy_id}>
    <code>{p.policy_id}</code> · <time dateTime={p.created_at}>{format(p.created_at)}</time>
    <V2Button disabled={Boolean(s.busy)||p.policy_id===s.policies.data?.active_policy_id} aria-busy={s.busy==="activate"} onClick={()=>void s.activate(p.policy_id)}>{p.policy_id===s.policies.data?.active_policy_id?"활성 정책":"정책 활성화"}</V2Button>
    <details><summary>버전 설정</summary><p>{p.enabled_metric_ids.join(", ")}</p><p>{p.enabled_insight_kinds.join(", ")}</p><p>{p.minimum_paper_count} / {p.minimum_denominator} / {p.minimum_absolute_count_delta} / {p.minimum_percentage_point_delta_basis_points} bp</p><p>{p.formula_registry_version} · {p.baseline_mode}</p></details>
   </li>)}</ul></details>:null}
   <Pages label="정책 이력" data={s.policies.data} page={s.policyPage} setPage={s.setPolicyPage}/>
   <details><summary>새 불변 정책 버전 설정</summary>
    <form onSubmit={e=>{e.preventDefault();submit();}}>
     <p>기존 정책과 분석 기록은 유지됩니다. 새 버전 저장 후 별도로 활성화하세요. 100 bp는 1 percentage point입니다.</p>
     <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(200px,1fr))",gap:12}}>{thresholds.map(([key,label,maximum])=><label key={key}>{label}<input aria-label={label} className="v2-input" type="number" min={0} max={maximum} step={1} required value={Number.isNaN(config[key])?"":config[key]} onChange={e=>setConfig(c=>({...c,[key]:e.target.value===""?NaN:Number(e.target.value)}))}/></label>)}</div>
     <fieldset><legend>활성화할 지표</legend>{metricIds.map((id,i)=><label key={id} style={{display:"block"}}><input type="checkbox" checked={config.enabled_metric_ids.includes(id)} onChange={()=>toggle("enabled_metric_ids",id)}/> {metricLabels[i]} · {id}</label>)}</fieldset>
     <fieldset><legend>생성할 기록 유형</legend>{insightKinds.map(id=><label key={id} style={{display:"block"}}><input type="checkbox" checked={config.enabled_insight_kinds.includes(id)} onChange={()=>toggle("enabled_insight_kinds",id)}/> {id}</label>)}</fieldset>
     {formError?<p role="alert">{formError}</p>:null}
     <V2Button type="submit" disabled={Boolean(s.busy)} aria-busy={s.busy==="create"}>새 정책 버전 만들기</V2Button>
    </form>
   </details>
  </section>
  <section aria-labelledby="manual-analysis-heading">
   <h3 id="manual-analysis-heading">수동 스냅샷 평가</h3>
   <ResourceState resource={s.snapshots} label="스냅샷 목록"/>
   {latest?<><p>{!latest.parent_snapshot_id?"선택 스냅샷 — 비교 기준 부족":"선택 스냅샷을 캡처 당시 이전 스냅샷과 비교"} · <time dateTime={latest.created_at}>{format(latest.created_at)}</time></p><p>분석 대상: {latest.snapshot_id}</p></>:null}
   <p>화면에 표시된 활성 정책 버전으로 저장합니다. 이후 활성 정책이 바뀌어도 이번 요청과 과거 기록의 정책은 바뀌지 않습니다.</p>
   <p id="analysis-disabled-reason">{evaluateReason}</p>
   <V2Button disabled={Boolean(s.busy)||Boolean(evaluateReason)} aria-describedby="analysis-disabled-reason" aria-busy={s.busy==="evaluate"} onClick={()=>void s.evaluate()}>{snapshotResource?"선택 스냅샷 분석":"최신 스냅샷 분석"}</V2Button>
  </section>
  <section aria-labelledby="metric-series-heading">
   <h3 id="metric-series-heading">시간순 측정값</h3>
   <label><input type="checkbox" checked={s.selectedOnly} disabled={!latest&&!s.selectedOnly} onChange={e=>s.setSelectedOnly(e.target.checked)}/> 선택 스냅샷 기록만 보기</label>
   <p>{s.selectedOnly?"선택한 스냅샷의 측정값과 분석 기록입니다.":"모든 스냅샷의 저장된 측정값과 분석 기록입니다."}</p>
   <label>측정 지표 <select className="v2-input" value={s.metric} onChange={e=>s.setMetric(e.target.value)}>{metricIds.map((id,i)=><option key={id} value={id}>{metricLabels[i]}</option>)}</select></label>
   <p>{s.metric.startsWith("coverage.")?"분자: 해당 상태를 충족하는 고유 논문 수. 분모: 캡처된 고유 논문 수. 알 수 없는 상태는 사용 불가로 표시합니다.":s.metric.startsWith("delta.")?"캡처 당시 이전 스냅샷과의 정확한 변경 이벤트 수입니다. 첫 스냅샷은 기준 부족입니다.":s.metric.endsWith("paper_version_count")?"캡처된 고유 논문 버전 ID 수입니다.":"캡처된 고유 논문 ID 수입니다."}</p>
   <ResourceState resource={s.series} label="측정값"/>
   {s.series.phase==="ready"&&!s.series.data?.items.length?<p>저장된 측정값이 없습니다. 정책을 활성화하고 스냅샷을 분석하세요.</p>:null}
   {s.series.data?.items.length?<><p>정책 버전별로 측정점을 나눕니다. 활성 정책 변경은 이전 값을 다시 계산하지 않습니다.</p>{[...new Set(s.series.data.items.map(point=>point.policy_id))].map(policyId=><div key={policyId}><p>측정 정책: <code>{policyId}</code></p><SeriesPlot points={s.series.data!.items.filter(point=>point.policy_id===policyId)} label={metricLabels[metricIds.indexOf(s.metric)]}/></div>)}
    <div style={{overflowX:"auto"}}><table aria-label="정확한 측정값"><caption>정확한 측정값 — 소수 백분율은 표시용입니다.</caption><thead><tr><th scope="col">스냅샷 시간</th><th scope="col">값</th><th scope="col">상태</th><th scope="col">정책·공식·출처</th></tr></thead><tbody>{s.series.data.items.map(point=><tr key={point.metric_point_id}>
     <td><time dateTime={point.snapshot_created_at}>{format(point.snapshot_created_at)}</time></td><td>{exact(point)}</td><td>{point.state}<br/>{point.reason_code}</td>
     <td><p>정책: <code>{point.policy_id}</code></p><details><summary>측정 출처</summary><p>{point.source_snapshot_id} ← {point.baseline_snapshot_id??"이전 스냅샷 없음"}</p><p>{point.formula_registry_version} · {point.formula_version}</p><p>{point.metric_point_id}</p><p>Entities {point.source_entity_count}: {point.source_entity_digest}</p><p>Evidence {point.source_evidence_count}: {point.source_evidence_digest}</p></details></td>
    </tr>)}</tbody></table></div></>:null}
   <Pages label="측정값" data={s.series.data} page={s.seriesPage} setPage={s.setSeriesPage}/>
  </section>
  <section aria-labelledby="factual-insights-heading">
   <h3 id="factual-insights-heading">근거 기반 분석 기록</h3>
   <ResourceState resource={s.insights} label="분석 기록"/>
   {s.insights.phase==="ready"&&!s.insights.data?.items.length?<p>저장된 분석 기록이 없습니다.</p>:null}
   {s.insights.data?.items.length&&!s.insights.data.items.some(i=>i.state==="ACCEPTED")?<p>이 페이지에 승인된 기록이 없습니다. 기준 부족·사용 불가·임계값 미달 기록은 아래에 표시합니다.</p>:null}
   <ol>{s.insights.data?.items.map(i=><li key={i.insight_record_id}>
    <article><p><strong>{i.state}</strong> · {i.reason_code}</p><p>{i.rendered_factual_text}</p>
     <p>정확한 값: {i.before_numerator??"—"}/{i.before_denominator??"—"} → {i.after_numerator??"—"}/{i.after_denominator??"—"} · 개수 변화 {i.absolute_delta??"—"} · 비율 변화 {i.percentage_point_delta_basis_points??"—"} bp</p>
     <details><summary>분석 근거와 버전</summary><p>{i.source_snapshot_id} ← {i.baseline_snapshot_id??"이전 스냅샷 없음"}</p><p>{i.policy_id}</p><p>{i.formula_registry_version} · {i.formula_version} · {i.template_version}</p><p>{i.confidence_class} (통계적 확률이 아님)</p>
      <p>Metric IDs: {i.metric_point_ids.join(", ")}</p><p>Entities: {i.source_entity_ids.join(", ")}</p><p>Evidence: {i.source_evidence_ids.join(", ")||"없음"}</p><p>{i.source_entity_digest} · {i.source_evidence_digest}</p>
      {i.calculated_at?<time dateTime={i.calculated_at}>{format(i.calculated_at)}</time>:null}
      {i.destination_reason?<p>현재 논문·근거 목적지 사용 불가: {i.destination_reason}</p>:null}
      {i.unavailable_evidence_ids?.length?<p>현재 원문이 없거나 캡처 당시 내용과 달라 열 수 없는 근거: {i.unavailable_evidence_ids.join(", ")}. 저장된 측정값과 근거 식별자는 유지됩니다.</p>:null}
      <ul>{i.paper_destinations?.map(link=><li key={link.id}><Link to={link.destination}>논문 버전 {link.id}</Link></li>)}{i.evidence_destinations?.map(link=><li key={link.destination}><Link to={link.destination}>근거 {link.id}</Link></li>)}</ul>
     </details>
     <InsightDecisionPanel key={`${topicId}:${i.insight_record_id}`} topicId={topicId} insight={i}/>
    </article>
   </li>)}</ol>
   <Pages label="분석 기록" data={s.insights.data} page={s.insightPage} setPage={s.setInsightPage}/>
  </section>
 </section>;
}
