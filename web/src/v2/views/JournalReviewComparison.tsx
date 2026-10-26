import { useEffect, useState } from "react";
import { fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";

type Judgment = { assessment: string; note: string };
type Summary = { review_id: string; state: string; note: string; actor_id: string; created_at: string; report_version_id: string | null };
type Comparison = {
  candidate_id: string; candidate_digest: string; input_digest: string; packet_id: string;
  from: Summary; to: Summary; overall_changes: { state: boolean; note: boolean };
  counts: { total: number; changed: number; unchanged: number; assessment_changed: number; note_changed: number };
  citations: { citation_id: string; title: string; text: string; locator: unknown; source_identity: unknown; paper_version_id?: string; receipt_id?: string }[];
  entries: { entry_id: string; kind: string; text: string; citation_ids: string[]; changed: boolean; changes: { assessment: boolean; note: boolean }; before: Judgment; after: Judgment }[];
};
const states: Record<string, string> = { reviewing: "검토 중", accepted: "저널로 채택", held: "보류", rejected: "검토 대상에서 제외" };
const assessments: Record<string, string> = { not_assessed: "미검토", supported: "근거와 부합", limited: "제한적으로 부합", unsupported: "근거 부족" };
const kinds: Record<string, string> = { observation: "관찰 문장", interpretation: "해석 문장", question: "질문" };

function Result({ value }: { value: Comparison }) {
  const { preferences } = useOptionalAppearance();
  const [showAll, setShowAll] = useState(false);
  const citations = new Map(value.citations.map(citation => [citation.citation_id, citation]));
  return <>
    <p>같은 후보의 보존된 사람 판단·메모만 비교합니다. 논문 내용의 변화·정확성·과학적 합의나 판단의 개선 여부를 평가하지 않습니다.</p>
    <div className="local-report-pair">{(["from", "to"] as const).map(side => <div key={side}>
      <h6>{side === "from" ? "비교 기준 기록" : "선택한 검토 기록"}</h6>
      <p>{states[value[side].state]} · {formatTimestamp(value[side].created_at, preferences.timeFormat, "ko-KR")}</p>
      <p>검토자: {value[side].actor_id}</p><p className="journal-preserved-text">{value[side].note || "전체 검토 메모 없음"}</p>
      <details><summary>{side === "from" ? "기준" : "선택"} 기록 식별자</summary><p>{value[side].review_id}</p><p>채택본: {value[side].report_version_id || "이 기록에는 없음"}</p></details>
    </div>)}</div>
    <p>전체 상태: {value.overall_changes.state ? "변경" : "동일"} · 전체 메모: {value.overall_changes.note ? "변경" : "동일"}</p>
    <p>판단·메모 변경 {value.counts.changed} / {value.counts.total}문장 · 동일 {value.counts.unchanged}문장</p>
    <p>근거 판단 변경 {value.counts.assessment_changed}문장 · 문장 메모 변경 {value.counts.note_changed}문장 (같은 문장이 양쪽에 포함될 수 있습니다.)</p>
    {!value.counts.changed ? <p>문장별 판단·메모가 동일합니다. 전체 상태와 전체 메모는 위에서 별도로 확인하세요.</p> : null}
    <V2Button type="button" aria-pressed={showAll} onClick={() => setShowAll(v => !v)}>{showAll ? "변경 문장만 보기" : "동일 문장도 보기"}</V2Button>
    {value.entries.map((entry, index) => showAll || entry.changed ? <section className="journal-imported-entry" key={entry.entry_id} aria-label={`비교 문장 ${index + 1}`}>
      <h6>문장 {index + 1} · {kinds[entry.kind]} · {entry.changed ? "변경" : "동일"}</h6>
      <p className="journal-preserved-text">{entry.text}</p>
      <p>근거 판단: {entry.changes.assessment ? "변경" : "동일"} · 문장 메모: {entry.changes.note ? "변경" : "동일"}</p>
      <div className="local-report-pair">{(["before", "after"] as const).map(side => <div key={side}>
        <h6>{side === "before" ? "기준 기록의 판단" : "선택 기록의 판단"}</h6><p>{assessments[entry[side].assessment]}</p>
        <p className="journal-preserved-text">{entry[side].note || "문장 메모 없음"}</p>
      </div>)}</div>
      {entry.citation_ids.map(id => { const citation = citations.get(id)!; return <details key={id}><summary>비교 문장의 보존 근거 · {citation.title}</summary>
        <blockquote className="journal-preserved-text">{citation.text}</blockquote>
        <p>위치: {citation.locator ? JSON.stringify(citation.locator) : "기록 없음"}</p><p>출처: {JSON.stringify(citation.source_identity)}</p>
        {citation.paper_version_id ? <p>논문 버전: {citation.paper_version_id}</p> : <p>보존 분석 입력: {citation.receipt_id}</p>}<p>근거: {citation.citation_id}</p>
      </details>; })}
    </section> : null)}
    <details><summary>비교 대상과 고정 입력 식별자</summary><p>후보: {value.candidate_id}</p><p>후보 해시: {value.candidate_digest}</p><p>고정 입력: {value.input_digest}</p><p>입력 묶음: {value.packet_id}</p></details>
  </>;
}

export function JournalReviewComparison({ root, reviewId, baselineId }: { root: string; reviewId: string; baselineId: string }) {
  const url = reviewId && baselineId && reviewId !== baselineId ? `${root}/${encodeURIComponent(reviewId)}/comparison?from_review_id=${encodeURIComponent(baselineId)}` : "";
  const [state, setState] = useState<{ url: string; value?: Comparison; error?: string } | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setState(null);
    if (url) void fetchJson<Comparison>(url).then(value => { if (!cancelled) setState({ url, value }); }, e => { if (!cancelled) setState({ url, error: String(e) }); });
    return () => { cancelled = true; };
  }, [url, revision]);
  const current = state?.url === url ? state : null;
  return <section className="journal-review-comparison" aria-label="저널 검토 비교">
    <h5>저널 검토 기록 비교</h5>
    {!baselineId ? <p>이력에서 비교 기준을 선택하세요. 기준이 없으면 변화량을 계산하지 않습니다.</p> : !reviewId ? <p>기준과 비교할 검토 기록을 이력에서 여세요.</p> : reviewId === baselineId ? <p>같은 기록을 선택했습니다. 기준과 다른 검토 기록을 여세요.</p> : <>
      <V2Button type="button" disabled={!current} onClick={() => setRevision(v => v + 1)}>검토 비교 다시 확인</V2Button>
      {current?.error ? <p role="alert">검토 비교를 확인하지 못했습니다: {current.error}. 변화량은 표시하지 않습니다.</p> : current?.value ? <Result key={url} value={current.value}/> : <p role="status">보존된 두 검토 기록 확인 중</p>}
    </>}
  </section>;
}
