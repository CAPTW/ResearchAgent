import { useEffect, useId, useRef, useState } from "react";
import { getReportVersions, getReportVersionDetail, getReportVersionDiff, type ApiReportHubItem, type ReportVersionsResponse, type ReportVersionDetailResponse, type ReportVersionDiffResponse } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { ReportEvidencePanel } from "./ReportEvidencePanel";

const sectionLabels={added:"추가",removed:"삭제",changed:"변경",unchanged:"동일"};
export function ReportVersionPanel({report,onClose}:{report:ApiReportHubItem;onClose:()=>void}) {
 const id=useId();
 const panel=useRef<HTMLElement>(null);
 useEffect(()=>{panel.current?.focus();},[]);
 const {preferences}=useOptionalAppearance();
 const [versions,setVersions]=useState<ReportVersionsResponse|null>(null);
 const [selected,setSelected]=useState("");
 const [baseline,setBaseline]=useState("");
 const [detail,setDetail]=useState<ReportVersionDetailResponse|null>(null);
 const [diff,setDiff]=useState<ReportVersionDiffResponse|null>(null);
 const [revision,setRevision]=useState(0);
 const [listError,setListError]=useState("");
 const [detailError,setDetailError]=useState("");
 const [diffError,setDiffError]=useState("");
 useEffect(()=>{
  let cancelled=false;setVersions(null);setListError("");
  void getReportVersions(report.runId).then(data=>{if(!cancelled){setVersions(data);setSelected(current=>data.items.some(v=>v.version_id===current)?current:data.items.at(-1)?.version_id||"");}},e=>{if(!cancelled)setListError(String(e));});
  return()=>{cancelled=true;};
 },[report.runId,revision]);
 useEffect(()=>{
  let cancelled=false;setDetail(null);setDetailError("");
  if(selected)void getReportVersionDetail(report.runId,selected).then(data=>{if(!cancelled)setDetail(data);},e=>{if(!cancelled)setDetailError(String(e));});
  return()=>{cancelled=true;};
 },[report.runId,selected,revision]);
 useEffect(()=>{
  let cancelled=false;setDiff(null);setDiffError("");
  if(baseline&&selected&&baseline!==selected)void getReportVersionDiff(report.runId,{from_version_id:baseline,to_version_id:selected}).then(data=>{if(!cancelled)setDiff(data);},e=>{if(!cancelled)setDiffError(String(e));});
  return()=>{cancelled=true;};
 },[report.runId,selected,baseline,revision]);
 const version=detail?.version;
 return <section ref={panel} tabIndex={-1} className="report-version-panel" aria-label="보고서 저장 버전">
  <h3>{report.title} · 저장 버전</h3>
  <button type="button" onClick={onClose}>보고서 닫기</button>
  <button type="button" onClick={()=>setRevision(v=>v+1)}>보고서 버전 새로고침</button>
  {listError?<p role="alert">버전 목록 오류: {listError}</p>:!versions?<p role="status">버전 목록 불러오는 중</p>:null}
  {versions?.warnings.map((w,n)=><p role="note" key={n}>{w}</p>)}
  {versions&&!versions.items.length?<p>저장된 완료 버전이 없습니다. 현재 산출물 다운로드와 완료 버전 보존은 별도입니다.</p>:null}
  {versions?.items.length?<>
   <label htmlFor={`${id}-version`}>읽을 보고서 버전</label>
   <select id={`${id}-version`} value={selected} onChange={e=>setSelected(e.target.value)}>{versions.items.map(v=><option key={v.version_id} value={v.version_id}>v{v.version_number} · {formatTimestamp(v.created_at,preferences.timeFormat,"ko-KR")}</option>)}</select>
   {detailError?<p role="alert">본문 오류: {detailError}</p>:!detail?<p role="status">저장 본문 불러오는 중</p>:null}
   {detail?.warnings.map((w,n)=><p role="alert" key={n}>{w}</p>)}
   {version?<details><summary>저장 버전과 근거 출처</summary>
    <p>버전: {version.version_id}</p><p>생성 경로: {version.source}</p>
    <p>본문 확인: {detail?.content_integrity==="SHA256_VERIFIED"?"SHA-256 일치":detail?.content_integrity==="LEGACY_HASH_VERIFIED"?"이전 형식의 짧은 해시 일치":detail?.content_integrity||"검증 정보 없음"}</p>
    <p>본문 해시: {version.content_sha256||version.content_hash||"기록 없음"}</p>
    <p>논문 집합 스냅샷: {version.corpus_snapshot_id||"기록 없음"}</p>
    <p>근거 집합 해시: {version.evidence_set_digest||"기록 없음"}</p>
    <p>출처 목록 해시: {version.source_manifest_digest||"기록 없음"}</p>
    <p>생성 단계 채택 기록 해시: {version.acceptance_record_digest||"별도 채택 기록 없음"}</p>
    <p>인용 개요 참조: {typeof version.citation_provenance?.source_outline_path==="string"?version.citation_provenance.source_outline_path:"기록 없음"}</p>
    <p>이 참조는 저장 당시 메타데이터입니다. 아래의 보존된 인용에서 버전 연결 상태를 확인하세요.</p>
   </details>:null}
   {detail?.content!=null?<pre className="report-saved-content" aria-label="저장된 Markdown 본문">{detail.content}</pre>:detail?<p>이 버전의 본문을 표시할 수 없습니다.</p>:null}
   {detail?.version?<ReportEvidencePanel key={detail.version.version_id} detail={detail}/>:null}
   <label htmlFor={`${id}-baseline`}>비교 기준 버전</label>
   <select id={`${id}-baseline`} value={baseline} onChange={e=>setBaseline(e.target.value)}><option value="">비교 기준 선택</option>{versions.items.map(v=><option key={v.version_id} value={v.version_id}>v{v.version_number} · {formatTimestamp(v.created_at,preferences.timeFormat,"ko-KR")}</option>)}</select>
   {versions.items.length<2?<p>비교하려면 저장 버전이 두 개 이상 필요합니다.</p>:null}
   {baseline===selected&&baseline?<p>서로 다른 두 버전을 선택하세요.</p>:null}
   {baseline&&baseline!==selected?<div aria-label="보고서 버전 비교">
    <p>선택한 두 본문의 절 단위 텍스트 비교입니다. 연구 결론의 타당성이나 의미 변화 평가는 아닙니다.</p>
    {diffError?<p role="alert">비교 오류: {diffError}</p>:!diff?<p role="status">버전 비교 중</p>:null}
    {diff?.warnings.map((w,n)=><p role="alert" key={n}>{w}</p>)}
    {diff&&!diff.ready?<p>{diff.empty_reason||"비교 결과를 사용할 수 없습니다."}</p>:null}
    {diff?.ready?<><p>추가 {diff.summary.added_sections} · 삭제 {diff.summary.removed_sections} · 변경 {diff.summary.changed_sections} · 동일 {diff.summary.unchanged_sections}</p>
     {diff.section_diffs.map((section,n)=><details key={`${section.section_id}:${n}`}><summary>{section.title} · {sectionLabels[section.status]}</summary><p>기준 버전</p><pre>{section.from_excerpt??"이 절이 없습니다."}</pre><p>읽는 버전</p><pre>{section.to_excerpt??"이 절이 없습니다."}</pre></details>)}</>:null}
   </div>:null}
  </>:null}
 </section>;
}
