import { saveExport } from "../../lib/saveExport";
import { useEffect, useId, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { LocalReportReader } from "./LocalReportReader";
import { JournalWorkingCopy } from "./JournalWorkingCopy";
import { frozenCitationLabel } from "../../lib/localSynthesis";

type Entry={kind:"observation"|"interpretation"|"question";text:string;citation_ids:string[]};
type Reference={citation_id:string;paper_version_id:string;title:string;kind:"abstract"|"review_excerpt"|"review_input_excerpt";text:string;locator:unknown;source_identity:unknown;review_state:string;rights:unknown;review_receipt_id?:string;input_origin?:string};
type Version={version_id:string;previous_id:string|null;actor_id:string;created_at:string;title:string;entries:Entry[];citations:Reference[];markdown:string;content_sha256:string;input_digest:string};
type History={items:Pick<Version,"version_id"|"title"|"created_at"|"actor_id">[];total:number;page:number;limit:number;latest_version:Version|null;input_digest:string;references:Reference[]};
type Save={request_id:string;expected_previous_id:string|null;expected_input_digest:string;title:string;entries:Entry[]};
const kinds={observation:"사용자 관찰",interpretation:"사용자 해석",question:"후속 질문"};
const blank=():Entry=>({kind:"interpretation",text:"",citation_ids:[]});

function Citation({reference}:{reference:Reference}) {
 return <details><summary>{reference.kind==="review_input_excerpt"?frozenCitationLabel(reference.kind):`저장된 ${frozenCitationLabel(reference.kind)}`} · {reference.title}</summary>
  <blockquote className="journal-preserved-text">{reference.text}</blockquote><p>논문 버전: {reference.paper_version_id}</p><p>근거: {reference.citation_id}</p>
  <p>위치: {reference.locator?JSON.stringify(reference.locator):"기록 없음"}</p><p>출처: {JSON.stringify(reference.source_identity)}</p><p>당시 검토: {reference.review_state} · 권리: {JSON.stringify(reference.rights)}</p>
  {reference.review_receipt_id?<p>선택한 검토 기록: {reference.review_receipt_id} · 입력 출처: {reference.input_origin||"미확인"}</p>:null}
 </details>;
}

function Editor({root,history,enabled,onSaved}:{root:string;history:History;enabled:boolean;onSaved:(v:Version)=>void}) {
 const id=useId();
 const [title,setTitle]=useState(history.latest_version?.title||"");
 const [entries,setEntries]=useState<Entry[]>(history.latest_version?.entries||[blank()]);
 const [base,setBase]=useState(history.latest_version?.version_id??null);
 const [busy,setBusy]=useState(false);
 const [copyBusy,setCopyBusy]=useState(false);
 const [error,setError]=useState("");
 const [conflict,setConflict]=useState(false);
 const [message,setMessage]=useState("");
 const alive=useRef(true),locked=useRef(false);
 const attempt=useRef<Save|null>(null);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 function changed(){attempt.current=null;setMessage("");}
 function edit(index:number,changes:Partial<Entry>){changed();setEntries(items=>items.map((entry,n)=>n===index?{...entry,...changes}:entry));}
 const valid=title.trim()&&entries.length>0&&entries.every(entry=>entry.text.trim()&&entry.citation_ids.length>0&&entry.citation_ids.length<=20);
 async function save(){
  if(!enabled||!valid||conflict||copyBusy||locked.current)return;
  locked.current=true;setBusy(true);setError("");setMessage("");
  const body=attempt.current??{request_id:crypto.randomUUID(),expected_previous_id:base,expected_input_digest:history.input_digest,title,entries};
  attempt.current=body;
  try{
   const result=await fetchJson<{version:Version;created:boolean}>(root,{method:"POST",body:JSON.stringify(body)});
   if(alive.current){attempt.current=null;setBase(result.version.version_id);setMessage("검토 초안을 새 버전으로 보존했습니다.");onSaved(result.version);}
  }catch(e){if(alive.current){const stale=e instanceof ApiError&&e.status===409;setConflict(stale);setError(stale?"다른 버전이 먼저 저장되었습니다. 입력은 유지됩니다. 이력을 새로고침해 최신본을 확인한 뒤 기준을 선택하세요.":`저장 실패: ${String(e)}. 같은 입력으로 재시도할 수 있습니다.`);}}
  finally{locked.current=false;if(alive.current)setBusy(false);}
 }
 function useLatest(keepInput:boolean){
  changed();setBase(history.latest_version?.version_id??null);setConflict(false);setError("");
  if(!keepInput){setTitle(history.latest_version?.title||"");setEntries(history.latest_version?.entries||[blank()]);}
 }
 return <section className="local-journal-editor" aria-label="근거 연결 초안 편집">
  <p>편집 기준: {base||"첫 버전"}</p>
  <p>저장은 전체 초안의 새 버전을 만듭니다. 문장을 빼거나 수정해도 과거 버전은 남습니다. 미완성 입력은 아래 보관 기능으로 남길 수 있으며, 보관하지 않은 편집은 화면 이동·재접속 시 복구되지 않습니다.</p>
  <JournalWorkingCopy url={`${root}-working-copy`} inputDigest={history.input_digest} content={{base_version_id:base,title,entries}} disabled={!enabled||busy} onBusy={setCopyBusy} onRestore={content=>{
   changed();setTitle(content.title);setEntries(content.entries);setBase(content.base_version_id);setError("");setConflict(content.base_version_id!==(history.latest_version?.version_id??null));
  }}/>
  <button type="button" disabled={!enabled||busy||copyBusy} onClick={()=>useLatest(false)}>현재 입력을 최신 저장본으로 교체</button>
  {(history.latest_version?.version_id??null)!==base?<><p>최신본이 편집 기준과 다릅니다. 최신본은 아래 이력에서 열 수 있습니다.</p><button type="button" disabled={!enabled||busy||copyBusy} onClick={()=>useLatest(true)}>최신본을 기준으로 현재 입력 유지</button></>:null}
  <fieldset disabled={!enabled||busy||copyBusy}>
   <legend>사용자 검토 문장 · 최대 30개</legend>
   <label htmlFor={`${id}-title`}>검토 초안 제목</label><input id={`${id}-title`} value={title} maxLength={200} onChange={e=>{changed();setTitle(e.target.value);}}/>
   {entries.map((entry,index)=><fieldset key={index}>
    <legend>문장 {index+1}</legend>
    <label htmlFor={`${id}-kind-${index}`}>문장 {index+1} 분류</label><select id={`${id}-kind-${index}`} value={entry.kind} onChange={e=>edit(index,{kind:e.target.value as Entry["kind"]})}>{Object.entries(kinds).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select>
    <label htmlFor={`${id}-text-${index}`}>문장 {index+1} 내용</label><textarea id={`${id}-text-${index}`} value={entry.text} maxLength={3000} onChange={e=>edit(index,{text:e.target.value})}/>
    <p>연결할 보존 근거 {entry.citation_ids.length} / 20개 · 한 개 이상 선택</p>
    <details><summary>문장 {index+1} 인용 선택</summary>{history.references.map(ref=><div key={ref.citation_id}>
     <label><input type="checkbox" checked={entry.citation_ids.includes(ref.citation_id)} disabled={!entry.citation_ids.includes(ref.citation_id)&&entry.citation_ids.length>=20} onChange={e=>edit(index,{citation_ids:e.target.checked?[...entry.citation_ids,ref.citation_id]:entry.citation_ids.filter(v=>v!==ref.citation_id)})}/>{ref.title} · {frozenCitationLabel(ref.kind)}</label>
     <Citation reference={ref}/>
    </div>)}</details>
    <button type="button" disabled={entries.length===1} onClick={()=>{changed();setEntries(items=>items.filter((_,n)=>n!==index));}}>문장 {index+1} 빼기</button>
   </fieldset>)}
   <button type="button" disabled={entries.length>=30} onClick={()=>{changed();setEntries(items=>[...items,blank()]);}}>검토 문장 추가</button>
   <button type="button" disabled={!valid||conflict} onClick={()=>void save()}>{busy?"검토 초안 저장 중":"검토 초안 새 버전 저장"}</button>
  </fieldset>
  {error?<p role="alert">{error}</p>:null}{message?<p role="status">{message}</p>:null}
 </section>;
}

function SavedVersion({root,versionId}:{root:string;versionId:string}) {
 const {preferences}=useOptionalAppearance();
 const [version,setVersion]=useState<Version|null>(null);
 const [error,setError]=useState("");
 const [revision,setRevision]=useState(0);
 const [exportError,setExportError]=useState("");
 const [busy,setBusy]=useState(false);
 const alive=useRef(true),locked=useRef(false);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{let cancelled=false;setVersion(null);setError("");setExportError("");
  void fetchJson<Version>(`${root}/${encodeURIComponent(versionId)}`).then(v=>{if(!cancelled)setVersion(v);},e=>{if(!cancelled)setError(String(e));});
  return()=>{cancelled=true;};
 },[root,versionId,revision]);
 async function download(format:"markdown"|"json"){
  if(!version||locked.current)return;locked.current=true;setBusy(true);setExportError("");
  try{const output=await fetchJson<{content:string;filename:string;media_type:string}>(`${root}/${encodeURIComponent(versionId)}/export?format=${format}`);
   if(!alive.current)return;
   await saveExport(output.content, output.filename, output.media_type);
  }catch(e){if(alive.current)setExportError(String(e));}finally{locked.current=false;if(alive.current)setBusy(false);}
 }
 return <article aria-label="보존된 근거 연결 초안">
  <button type="button" disabled={busy} onClick={()=>setRevision(v=>v+1)}>선택 초안 다시 확인</button>
  {error?<p role="alert">보존 초안 오류: {error}</p>:!version?<p role="status">보존 초안 확인 중</p>:<>
   <h4>{version.title}</h4><p>작성자: {version.actor_id} · {formatTimestamp(version.created_at,preferences.timeFormat,"ko-KR")}</p><p>버전: {version.version_id}</p><p>이전 버전: {version.previous_id||"없음"}</p>
   <p>인용은 저장된 원문과 연결됨 · 문장의 타당성·함의·모순은 미평가</p>
   {version.entries.map((entry,n)=><section key={n} aria-label={`보존 문장 ${n+1}`}><h5>{n+1}. {kinds[entry.kind]}</h5><p className="journal-preserved-text">{entry.text}</p>{entry.citation_ids.map(ref=><Citation key={ref} reference={version.citations.find(c=>c.citation_id===ref)!}/>)}</section>)}
   <details><summary>초안 Markdown 원문과 식별자</summary><p>입력: {version.input_digest}</p><p>본문 SHA-256: {version.content_sha256}</p><pre>{version.markdown}</pre></details>
   <details><summary>인용 번호가 포함된 초안 읽기</summary><LocalReportReader content={version.markdown}/></details>
   <button type="button" disabled={busy} onClick={()=>void download("markdown")}>검토 초안 Markdown 내려받기</button>
   <button type="button" disabled={busy} onClick={()=>void download("json")}>검토 초안 근거 JSON 내려받기</button>
   <p>선택한 사용자 초안과 인용을 내보냅니다. 기존 보고서의 채택 상태와는 별개입니다.</p>
  </>}
  {exportError?<p role="alert">초안 내보내기 실패: {exportError}</p>:null}
 </article>;
}

function Workspace({root}:{root:string}) {
 const {preferences}=useOptionalAppearance();
 const [history,setHistory]=useState<History|null>(null);
 const [error,setError]=useState("");
 const [loading,setLoading]=useState(true);
 const [page,setPage]=useState(1);
 const [revision,setRevision]=useState(0);
 const [selected,setSelected]=useState("");
 useEffect(()=>{let cancelled=false;setLoading(true);setError("");
  void fetchJson<History>(`${root}?page=${page}&limit=10`).then(data=>{if(!cancelled){setHistory(data);setLoading(false);}},e=>{if(!cancelled){setError(String(e));setLoading(false);}});
  return()=>{cancelled=true;};
 },[root,page,revision]);
 const ready=!loading&&!error;
 return <>
  <p>보존된 초록·리뷰 발췌를 근거로 관찰·해석·후속 질문을 직접 작성합니다. 인용 연결을 검사하며 과학적 검증, AI 실행 또는 보고서 채택을 수행하지 않습니다.</p>
  <button type="button" disabled={loading} onClick={()=>setRevision(v=>v+1)}>검토 초안 이력 새로고침</button>
  {error?<p role="alert">초안 이력 오류: {error}. 이전 목록은 현재 상태로 확인되지 않았습니다.</p>:loading?<p role="status">검토 초안 이력 확인 중</p>:null}
  {history?<>
   {history.references.length?<Editor root={root} history={history} enabled={ready} onSaved={version=>{setSelected(version.version_id);setPage(1);setRevision(v=>v+1);}}/>:<p>이 고정 입력에는 인용할 초록·발췌문이 없습니다. 근거가 포함된 입력을 새로 고정하세요.</p>}
   <h4>검토 초안 이력 · {history.total}개 버전</h4>
   <ul>{history.items.map(v=><li key={v.version_id}><button type="button" disabled={!ready} aria-pressed={selected===v.version_id} onClick={()=>setSelected(v.version_id)}>{v.title} · {formatTimestamp(v.created_at,preferences.timeFormat,"ko-KR")}<small className="local-report-id">{v.version_id}</small></button></li>)}</ul>
   <button type="button" disabled={!ready||page===1} onClick={()=>setPage(v=>v-1)}>이전 초안 페이지</button><span> {page} / {Math.max(1,Math.ceil(history.total/history.limit))} </span><button type="button" disabled={!ready||page*history.limit>=history.total} onClick={()=>setPage(v=>v+1)}>다음 초안 페이지</button>
  </>:null}
  {selected?<SavedVersion key={selected} root={root} versionId={selected}/>:null}
 </>;
}

export function LocalJournalPanel({topicId,jobId}:{topicId:string;jobId:string}) {
 const [activated,setActivated]=useState(false);
 const root=`/api/topics/${encodeURIComponent(topicId)}/local-synthesis/${encodeURIComponent(jobId)}/journal`;
 return <details className="local-journal" onToggle={e=>{if(e.currentTarget.open)setActivated(true);}}><summary>근거 연결 검토 초안</summary>{activated?<Workspace key={root} root={root}/>:null}</details>;
}
