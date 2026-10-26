import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { SavedCandidate } from "./JournalExchangePanel";
import { candidateTimestamp, candidateOriginLabel, type CandidateSummary } from "../../lib/journalCandidates";

type Item = CandidateSummary & {
  job_id: string;
  latest_review: null | { review_id: string; state: "reviewing" | "accepted" | "held" | "rejected"; created_at: string };
  last_acceptance: null | { review_id: string; version_id: string; created_at: string };
};
type Listing = { items: Item[]; total: number; page: number; limit: number };
const states = { reviewing: "검토 중", accepted: "저널로 채택", held: "보류", rejected: "검토 대상에서 제외" };

function Workspace({ topicId }: { topicId: string }) {
  const { preferences } = useOptionalAppearance();
  const root = `/api/topics/${encodeURIComponent(topicId)}/local-synthesis`;
  const [listing, setListing] = useState<Listing | null>(null), [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [selected, setSelected] = useState<{ item: Item; reviewId?: string } | null>(null);
  const opener = useRef<HTMLButtonElement | null>(null), heading = useRef<HTMLHeadingElement | null>(null), listHeading = useRef<HTMLHeadingElement | null>(null);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    void fetchJson<Listing>(`${root}/journals?page=${page}&limit=10`).then(data => {
      if (!cancelled) { setListing(data); setLoading(false); }
    }, e => { if (!cancelled) { setError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [root, page, revision]);
  useEffect(() => { if (selected) heading.current?.focus(); }, [selected]);
  const ready = !loading && !error;
  const date = (value: string) => formatTimestamp(value, preferences.timeFormat, "ko-KR");
  return <section className="v2-panel local-journal journal-exchange journal-library" aria-label="주제 저널 보관함">
    <h2 ref={listHeading} tabIndex={-1}>주제 저널 보관함</h2>
    <p>저널 후보를 최근 보존한 순서로 표시합니다. 파일로 가져온 후보와 저장된 생성 결과는 출처를 구분합니다. 과거 채택본과 현재 사람의 검토 상태는 구분되며, 보류·제외 후에도 보존 기록을 읽을 수 있습니다.</p>
    <V2Button type="button" disabled={loading} onClick={() => setRevision(v => v + 1)}>주제 저널 새로고침</V2Button>
    {loading ? <p role="status">주제 저널 확인 중</p> : error ? <p role="alert">주제 저널 조회 실패: {error}. 현재 목록·검토 상태를 확인하지 못했습니다. 새로고침으로 재시도하세요.</p> : null}
    {ready && listing ? <>
      <p>보존된 저널 {listing.total}개 · 현재 페이지 {listing.items.length}개</p>
      {!listing.total ? <p>보존된 저널이 없습니다. 아래 선택 문헌 근거 초안에서 결과 파일을 가져오거나 저장된 생성 결과를 후보로 보존하면 이곳에서 찾을 수 있습니다.</p> : !listing.items.length ? <p>이 페이지에 저널이 없습니다. 이전 페이지로 이동하세요.</p> : null}
      <ul className="journal-library-list">{listing.items.map(item => <li key={item.candidate_id}>
        <p className="journal-current-review"><strong>현재 검토: {item.latest_review ? states[item.latest_review.state] : "미검토"}</strong>{item.latest_review ? ` · ${date(item.latest_review.created_at)}` : ""}</p>
        <h3><span className="journal-title-label">보존된 원고 제목</span>{" "}<span className="journal-preserved-text">{item.title}</span></h3>
        <p>제목은 후보를 보존한 당시의 원문이며, 현재 검토 상태를 뜻하지 않습니다.</p>
        <p>{candidateOriginLabel(item)} · 보존 날짜: {date(candidateTimestamp(item))}</p>
        {item.last_acceptance ? <p>최근 보존 채택본: {date(item.last_acceptance.created_at)}{item.latest_review?.review_id !== item.last_acceptance.review_id ? " · 현재 채택 상태가 아닌 과거 기록" : ""}</p> : <p>보존된 채택본 없음</p>}
        <V2Button type="button" aria-pressed={selected?.item.candidate_id === item.candidate_id} onClick={e => { opener.current = e.currentTarget; setSelected({ item, reviewId: item.last_acceptance?.review_id ?? item.latest_review?.review_id }); }}>{item.last_acceptance ? "보존 채택본·검토 열기" : "후보·검토 열기"}</V2Button>
      </li>)}</ul>
      <V2Button type="button" disabled={page === 1} onClick={() => setPage(v => v - 1)}>이전 주제 저널 페이지</V2Button><span> {page} / {Math.max(1, Math.ceil(listing.total / listing.limit))} </span><V2Button type="button" disabled={page * listing.limit >= listing.total} onClick={() => setPage(v => v + 1)}>다음 주제 저널 페이지</V2Button>
    </> : null}
    {selected ? <section aria-label="보관함에서 연 저널">
      <h3 ref={heading} tabIndex={-1}>선택한 저널 · 보존된 제목: {selected.item.title}</h3>
      <V2Button type="button" onClick={() => { setSelected(null); if (opener.current?.isConnected) opener.current.focus(); else listHeading.current?.focus(); }}>보관함 저널 닫기</V2Button>
      <SavedCandidate key={`${selected.item.candidate_id}/${selected.reviewId || "unreviewed"}`} root={`${root}/${encodeURIComponent(selected.item.job_id)}/journal-candidates`} candidateId={selected.item.candidate_id} initialReviewId={selected.reviewId} showReview onReviewSaved={() => setRevision(v => v + 1)}/>
    </section> : null}
  </section>;
}

export function JournalLibraryPanel({ topicId }: { topicId: string }) {
  return <Workspace key={topicId} topicId={topicId}/>;
}
