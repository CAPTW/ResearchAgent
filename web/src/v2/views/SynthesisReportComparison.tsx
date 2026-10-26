import { useEffect, useState } from "react";
import { fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";

type Summary={job_id:string;version_id:string;created_at:string;paper_count:number;input_digest:string;source_snapshot_id:string;content_sha256:string;latest_decision:null|{state:"accepted"|"held"|"rejected"}};
type Listing={items:Summary[];page:number;limit:number;total:number};
type Field={field:string;before:unknown;after:unknown};
type Comparison={before:Summary;after:Summary;counts:Record<"added"|"removed"|"changed"|"unchanged",number>;input_context:Field[];
 coverage:{metric:string;before:number;after:number;delta:number}[];
 papers:{paper_version_id:string;title:string;status:"added"|"removed"|"changed"|"unchanged";fields:Field[]}[]};
const reviews={accepted:"채택",held:"보류",rejected:"검토 제외"};
const statuses={added:"추가",removed:"제외",changed:"기록 변경",unchanged:"같음"};
const fields:Record<string,string>={abstract:"초록",evidence:"리뷰 발췌와 근거 위치",authors:"저자",title:"제목",metadata_digest:"메타데이터 해시",source_identity:"출처 식별자",review_state:"기존 검토 상태",rights:"권리 상태",review_source_run_id:"리뷰 실행",source_text_digest:"원문 해시",member:"스냅샷 구성 기록",canonical_paper_id:"문헌 식별자",paper_version_id:"논문 버전",topic_label:"주제 이름",source_snapshot_id:"주제 스냅샷",plan:"처리 규칙"};
const metrics:Record<string,string>={selected_papers:"선택 논문",papers_with_abstract:"초록 보존",papers_with_stored_excerpt:"리뷰 발췌 보존",recorded_full_text_reviewed:"기존 전문 검토 기록"};

function Values({changes}:{changes:Field[]}) {
 return <>{changes.map(change=><details key={change.field}><summary>{fields[change.field]||change.field}</summary>
  <div className="local-report-pair">{(["before","after"] as const).map(side=><div key={side}><strong>{side==="before"?"비교 기준":"현재 선택"}</strong><pre>{change[side]===null?"기록 없음 (null)":typeof change[side]==="string"?(change[side] as string)||"빈 문자열":JSON.stringify(change[side],null,2)}</pre></div>)}</div>
 </details>)}</>;
}

export function SynthesisReportComparison({topicId,jobId}:{topicId:string;jobId:string}) {
 const {preferences}=useOptionalAppearance();
 const root=`/api/topics/${encodeURIComponent(topicId)}/local-synthesis`;
 const [page,setPage]=useState(1);
 const [revision,setRevision]=useState(0);
 const [listing,setListing]=useState<Listing|null>(null);
 const [listError,setListError]=useState("");
 const [baseline,setBaseline]=useState("");
 const [comparison,setComparison]=useState<Comparison|null>(null);
 const [error,setError]=useState("");
 useEffect(()=>{
  let cancelled=false;setListing(null);setListError("");
  void fetchJson<Listing>(`${root}/reports?page=${page}&limit=10`).then(data=>{if(!cancelled)setListing(data);},e=>{if(!cancelled)setListError(String(e));});
  return()=>{cancelled=true;};
 },[root,page,revision]);
 useEffect(()=>{
  let cancelled=false;setComparison(null);setError("");
  if(baseline)void fetchJson<Comparison>(`${root}/${encodeURIComponent(jobId)}/report/comparison?${new URLSearchParams({from_job_id:baseline})}`).then(data=>{if(!cancelled)setComparison(data);},e=>{if(!cancelled)setError(String(e));});
  return()=>{cancelled=true;};
 },[root,jobId,baseline,revision]);
 return <section aria-label="보존 보고서 비교">
  <h4>보존 보고서 비교</h4>
  <p>같은 주제의 보고서를 기준으로 선택하세요. 논문 버전 ID가 같은 기록만 대응하며, 다른 버전은 추가·제외로 표시합니다. 현재 원본을 다시 조회하지 않습니다.</p>
  <button type="button" onClick={()=>setRevision(v=>v+1)}>비교 목록·결과 새로고침</button>
  {listError?<p role="alert">비교 목록 오류: {listError}</p>:!listing?<p role="status">보존 보고서 목록 불러오는 중</p>:<>
   <p>{listing.total}개 보존 보고서 · {page}페이지</p>
   <ul>{listing.items.map(item=><li key={item.job_id}><button type="button" disabled={item.job_id===jobId} aria-pressed={baseline===item.job_id} onClick={()=>setBaseline(item.job_id)}>
    {item.job_id===jobId?"현재 선택":"비교 기준으로 선택"} · {item.paper_count}편 · {formatTimestamp(item.created_at,preferences.timeFormat,"ko-KR")} · {item.latest_decision?reviews[item.latest_decision.state]:"판단 기록 없음"}
    <small className="local-report-id">{item.version_id}</small>
   </button></li>)}</ul>
   <button type="button" disabled={page===1} onClick={()=>setPage(v=>v-1)}>보존 보고서 이전 페이지</button>
   <button type="button" disabled={page*listing.limit>=listing.total} onClick={()=>setPage(v=>v+1)}>보존 보고서 다음 페이지</button>
  </>}
  {!baseline?<p>비교 기준을 선택하기 전에는 변화량을 계산하지 않습니다.</p>:<>
   <p>선택한 비교 기준 작업: {baseline}</p>
   {error?<p role="alert">보고서 비교 실패: {error}. 변화가 없는 것으로 해석할 수 없습니다.</p>:!comparison?<p role="status">두 보존 보고서 확인 중</p>:<>
    <div className="local-report-pair">{(["before","after"] as const).map(side=><div key={side}><h5>{side==="before"?"비교 기준":"현재 선택"}</h5><p>{comparison[side].version_id}</p><p>확인 시 판단: {comparison[side].latest_decision?reviews[comparison[side].latest_decision.state]:"기록 없음"}</p><details><summary>보고서 입력·본문 식별자</summary><p>입력: {comparison[side].input_digest}</p><p>스냅샷: {comparison[side].source_snapshot_id}</p><p>본문 SHA-256: {comparison[side].content_sha256}</p></details></div>)}</div>
    <p>논문 버전: 추가 {comparison.counts.added} · 제외 {comparison.counts.removed} · 기록 변경 {comparison.counts.changed} · 같음 {comparison.counts.unchanged}</p>
    <p>아래 값은 보존 기록의 편수입니다. 연구의 성장·중요도·합의·모순은 미평가이며, 변화량으로 추론하지 않습니다.</p>
    <table className="local-report-coverage"><caption>보존 범위 비교 (편)</caption><thead><tr><th scope="col">기록</th><th scope="col">기준</th><th scope="col">선택</th><th scope="col">차이</th></tr></thead><tbody>{comparison.coverage.map(row=><tr key={row.metric}><th scope="row">{metrics[row.metric]||row.metric}</th><td>{row.before}</td><td>{row.after}</td><td>{row.delta>0?"+":""}{row.delta}</td></tr>)}</tbody></table>
    <h5>입력 배경의 차이</h5>{comparison.input_context.length?<Values changes={comparison.input_context}/>:<p>주제 이름·스냅샷·처리 규칙을 포함한 입력 배경이 같습니다.</p>}
    <h5>논문 버전별 기록</h5>{comparison.papers.map(p=><details key={p.paper_version_id}><summary>{statuses[p.status]} · {p.title}</summary><p>{p.paper_version_id}</p>{p.fields.length?<Values changes={p.fields}/>:<p>고정한 논문 기록이 같습니다.</p>}</details>)}
   </>}
  </>}
 </section>;
}
