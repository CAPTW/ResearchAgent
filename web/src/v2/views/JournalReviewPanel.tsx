import { saveExport } from "../../lib/saveExport";
import { useEffect, useId, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { LocalReportReader } from "./LocalReportReader";
import { JournalReviewComparison } from "./JournalReviewComparison";
import { V2Button } from "../V2Primitives";
import { EvidenceText } from "../components/EvidenceText";
import { JournalReviewDraftBoundary, needsReviewProtection, reviewInput, useJournalReviewDraft, type Assessment, type Judgment, type ReviewState as State } from "../JournalReviewDrafts";

type Entry = { entry_id: string; section: string; kind: string; text: string; citation_ids: string[]; source_component?: { label: string; component_id: string; support: string } };
type Citation = { citation_id: string; title: string; text: string; locator: unknown };
type Review = { review_id: string; previous_id: string | null; state: State; entries: Judgment[]; note: string; actor_id: string; created_at: string; report: null | { version_id: string; content?: string; content_sha256: string } };
type History = { items: Review[]; total: number; limit: number; latest: Review | null; candidate_digest: string; entries: Entry[] };
const assessments: Record<Assessment, string> = { not_assessed: "미검토", supported: "근거와 부합", limited: "제한적으로 부합", unsupported: "근거 부족" };
const states: Record<State, string> = { reviewing: "검토 중", accepted: "저널로 채택", held: "보류", rejected: "검토 대상에서 제외" };
const sections: Record<string, string> = { overview: "개요", synthesis: "문헌 간 해석", limitations: "한계", questions: "후속 질문" };
const kinds: Record<string, string> = { observation: "관찰 문장", interpretation: "해석 문장", question: "질문" };
const judgments = (h: History): Judgment[] => h.latest?.entries || h.entries.map(e => ({ entry_id: e.entry_id, assessment: "not_assessed", note: "" }));

function Editor({ root, history, citations, enabled, onSaved, onRefresh }: { root: string; history: History; citations: Citation[]; enabled: boolean; onSaved: (reviewId: string) => void; onRefresh: () => void }) {
  const id = useId();
  const { draft, get, update } = useJournalReviewDraft(JSON.stringify([root, history.candidate_digest]), {
    entries: judgments(history), note: history.latest?.note || "", base: history.latest?.review_id ?? null,
    baseline: reviewInput(judgments(history), history.latest?.note || ""), busy: false, conflict: false,
    error: "", message: "", attempt: null, savedReviewId: null,
  });
  const { entries, note, base, busy, conflict, error, message } = draft;
  const [replacing, setReplacing] = useState(false);
  const replaceButton = useRef<HTMLButtonElement>(null);
  const observedSave = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (observedSave.current === undefined) {
      observedSave.current = draft.savedReviewId;
      // A cached completion may predate this GET. Refresh without replacing an
      // explicitly opened historical review with our previous selection.
      if (draft.savedReviewId) onRefresh();
    } else if (draft.savedReviewId && draft.savedReviewId !== observedSave.current) {
      observedSave.current = draft.savedReviewId; onSaved(draft.savedReviewId);
    }
  }, [draft.savedReviewId, onSaved, onRefresh]);
  const valid = entries.every(e => !["limited", "unsupported"].includes(e.assessment) || !!e.note.trim());
  const acceptable = entries.every(e => e.assessment === "supported" || e.assessment === "limited");
  function edit(entryId: string, change: Partial<Judgment>) {
    update(d => d.busy ? d : { ...d, attempt: null, message: "", entries: d.entries.map(e => e.entry_id === entryId ? { ...e, ...change } : e) });
  }
  function useLatest(keep: boolean) {
    if (!enabled || get().busy) return;
    update(d => ({ ...d, attempt: null, message: "", base: history.latest?.review_id ?? null, conflict: false, error: "", baseline: reviewInput(judgments(history), history.latest?.note || ""),
      ...(!keep ? { entries: judgments(history), note: history.latest?.note || "" } : {}),
    }));
    setReplacing(false);
    if (replacing) replaceButton.current?.focus();
  }
  async function save(state: State) {
    const current = get();
    if (!enabled || current.busy || current.conflict || current.entries.some(e => ["limited", "unsupported"].includes(e.assessment) && !e.note.trim()) || (state === "accepted" && current.entries.some(e => e.assessment !== "supported" && e.assessment !== "limited"))) return;
    const body = current.attempt?.state === state ? current.attempt : { request_id: crypto.randomUUID(), expected_previous_id: current.base, expected_candidate_digest: history.candidate_digest, state, entries: current.entries, note: current.note };
    update(d => ({ ...d, busy: true, error: "", message: "", attempt: body }));
    setReplacing(false);
    try {
      const result = await fetchJson<{ review: Review; created: boolean }>(root, { method: "POST", body: JSON.stringify(body) });
      update(d => ({ ...d, busy: false, attempt: null, base: result.review.review_id, baseline: reviewInput(body.entries, body.note), savedReviewId: result.review.review_id,
        message: result.created ? "검토 판단을 새 기록으로 보존했습니다." : "같은 요청으로 보존된 검토 기록을 확인했습니다.",
      }));
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409;
      update(d => ({ ...d, busy: false, conflict: stale, error: stale ? "다른 검토 기록이 먼저 저장되었습니다. 입력은 유지됩니다. 이력을 새로고침하고 최신 기준을 선택하세요." : `검토 저장 실패: ${String(e)}. 같은 입력으로 재시도할 수 있습니다.` }));
    }
  }
  return <section className="local-journal-editor" aria-label="저널 근거 판단 편집">
    <p>보존된 문장·인용을 직접 확인한 사람의 판단입니다. 질문의 부합은 문맥에 대한 판단이며 답을 검증했다는 뜻이 아닙니다.</p>
    <p>제한적 부합·근거 부족에는 이유를 남기세요. 미검토·근거 부족 문장이 있으면 채택할 수 없습니다. 입력은 이 앱 세션의 같은 후보로 돌아오면 이어집니다. 재접속 후에도 보존하려면 검토 내용을 저장하세요.</p>
    <button ref={replaceButton} type="button" disabled={!enabled || busy} aria-expanded={replacing} onClick={() => needsReviewProtection(get()) ? setReplacing(true) : useLatest(false)}>검토 입력을 최신 기록으로 교체</button>
    {replacing ? <div role="group" aria-label="검토 입력 교체 확인"><p>미저장 입력과 재시도 요청을 버리고 마지막 조회된 검토 기록으로 교체합니다. 서버에 이미 저장된 판단은 삭제되지 않습니다.</p><button type="button" onClick={() => { setReplacing(false); replaceButton.current?.focus(); }}>입력 계속 편집</button><button type="button" disabled={!enabled || busy} onClick={() => useLatest(false)}>미저장 입력 버리고 교체</button></div> : null}
    {(history.latest?.review_id ?? null) !== base ? <><p>최신 검토와 편집 기준이 다릅니다. 새 판단을 확인한 뒤 이어가세요.</p><button type="button" disabled={!enabled || busy} onClick={() => useLatest(true)}>최신 검토를 기준으로 입력 유지</button></> : null}
    <fieldset disabled={!enabled || busy}>
      <legend>문장별 근거 검토</legend>
      {history.entries.map((entry, index) => {
        const judgment = entries.find(e => e.entry_id === entry.entry_id)!;
        return <fieldset key={entry.entry_id}><legend>검토 문장 {index + 1} · {sections[entry.section]} · {kinds[entry.kind]}</legend>
          {entry.source_component ? <p>원래 분석 항목: {entry.source_component.label} · {entry.source_component.component_id}{entry.source_component.support === 'NOT_ESTABLISHED' ? ' · 원 응답에서 근거 미확인으로 표시된 주장' : ''}</p> : null}
          <p className="journal-preserved-text">{entry.text}</p>
          {entry.citation_ids.map(ref => { const citation = citations.find(c => c.citation_id === ref)!; return <details key={ref}><summary>검토 근거 · {citation.title}</summary><EvidenceText text={citation.text}/><p>위치: {citation.locator ? JSON.stringify(citation.locator) : "기록 없음"}</p><small>{citation.citation_id}</small></details>; })}
          <label htmlFor={`${id}-assessment-${index}`}>문장 {index + 1} 근거 판단</label><select id={`${id}-assessment-${index}`} value={judgment.assessment} onChange={e => edit(entry.entry_id, { assessment: e.target.value as Assessment })}>{Object.entries(assessments).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
          <label htmlFor={`${id}-note-${index}`}>문장 {index + 1} 검토 메모</label><textarea id={`${id}-note-${index}`} maxLength={2000} value={judgment.note} onChange={e => edit(entry.entry_id, { note: e.target.value })}/>
        </fieldset>;
      })}
      <label htmlFor={`${id}-note`}>저널 전체 검토 메모</label><textarea id={`${id}-note`} value={note} maxLength={2000} onChange={e => { const value = e.target.value; update(d => d.busy ? d : { ...d, attempt: null, message: "", note: value }); }}/>
      <p>채택은 현재 문장별 판단과 한계를 포함한 별도 저널 버전을 보존합니다. 후보 원문·과거 검토는 그대로 남습니다.</p>
      {(["reviewing", "accepted", "held", "rejected"] as State[]).map(state => <button key={state} type="button" disabled={!valid || conflict || (state === "accepted" && !acceptable)} onClick={() => void save(state)}>{state === "reviewing" ? "검토 내용 저장" : states[state]}</button>)}
    </fieldset>
    {!valid ? <p role="status">제한적 부합·근거 부족으로 표시한 문장에 검토 이유를 입력하세요.</p> : null}
    {!acceptable ? <p>미검토 또는 근거 부족 문장이 남아 있습니다.</p> : null}
    {error ? <p role="alert">{error}</p> : null}{message ? <p role="status">{message}</p> : null}
  </section>;
}

function SavedReview({ root, reviewId, history, enabled, onRefresh, openManuscript }: { root: string; reviewId: string; history: History; enabled: boolean; onRefresh: () => void; openManuscript?: boolean }) {
  const { preferences } = useOptionalAppearance();
  const [review, setReview] = useState<Review | null>(null), [error, setError] = useState("");
  const [revision, setRevision] = useState(0), [busy, setBusy] = useState(false);
  const alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setReview(null); setError("");
    void fetchJson<Review>(`${root}/${encodeURIComponent(reviewId)}`).then(data => { if (!cancelled) setReview(data); }, e => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [root, reviewId, revision]);
  const eligible = enabled && review?.state === "accepted" && history.latest?.review_id === reviewId;
  async function download(format: "markdown" | "json") {
    if (!eligible || locked.current) return;
    locked.current = true; setBusy(true); setError("");
    try {
      const result = await fetchJson<{ content: string; filename: string; media_type: string }>(`${root}/${encodeURIComponent(reviewId)}/export?format=${format}`);
      if (!alive.current) return;
      await saveExport(result.content, result.filename, result.media_type);
    } catch (e) { if (alive.current) { setError(`내보내기 실패: ${String(e)}`); onRefresh(); } }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  return <article aria-label="보존된 저널 검토 기록">
    <button type="button" disabled={busy} onClick={() => setRevision(v => v + 1)}>선택 검토 다시 확인</button>
    {error ? <p role="alert">{error}</p> : null}
    {!review ? (!error ? <p role="status">검토 기록 확인 중</p> : null) : <>
      <h5>보존 판단: {states[review.state]}</h5><p>{review.actor_id} · {formatTimestamp(review.created_at, preferences.timeFormat, "ko-KR")}</p>
      <p className="journal-preserved-text">{review.note || "전체 검토 메모 없음"}</p>
      {history.entries.map((entry, n) => { const judgment = review.entries.find(e => e.entry_id === entry.entry_id)!; return <section key={entry.entry_id}><h6>문장 {n + 1} · {kinds[entry.kind]} · {assessments[judgment.assessment]}</h6><p className="journal-preserved-text">{entry.text}</p><p className="journal-preserved-text">{judgment.note || "문장 메모 없음"}</p></section>; })}
      <details><summary>검토 기록 식별자</summary><p>{review.review_id}</p><p>이전: {review.previous_id || "없음"}</p></details>
      {review.report ? <>
        <details open={openManuscript || undefined}><summary>보존된 채택 저널 읽기</summary><p>제목·본문은 채택 당시 보존된 원문입니다. 원문에 적힌 상태와 현재 검토 상태는 다를 수 있으며, 현재 상태는 최신 검토 이력을 기준으로 확인하세요.</p><LocalReportReader content={review.report.content || ""}/><p>버전: {review.report.version_id}</p><p>SHA-256: {review.report.content_sha256}</p></details>
        {!eligible ? <p>과거 채택본은 읽을 수 있습니다. 내보내기는 현재 최신 검토가 채택한 버전에만 허용됩니다.</p> : null}
        <button type="button" disabled={!eligible || busy || !!error} onClick={() => void download("markdown")}>채택 저널 Markdown 내려받기</button>
        <button type="button" disabled={!eligible || busy || !!error} onClick={() => void download("json")}>채택 저널 검토·근거 JSON 내려받기</button>
      </> : null}
    </>}
  </article>;
}

type PanelProps = { root: string; citations: Citation[]; enabled: boolean; initialReviewId?: string; initiallyOpen?: boolean; onSaved?: () => void };

function Workspace({ root, citations, enabled, initialReviewId, onSaved }: PanelProps) {
  const { preferences } = useOptionalAppearance();
  const [history, setHistory] = useState<History | null>(null), [error, setError] = useState("");
  const [page, setPage] = useState(1), [revision, setRevision] = useState(0), [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(initialReviewId || "");
  const [baseline, setBaseline] = useState<Review | null>(null);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    void fetchJson<History>(`${root}?page=${page}&limit=10`).then(data => { if (!cancelled) { setHistory(data); setLoading(false); } }, e => { if (!cancelled) { setError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [root, page, revision]);
  // Source eligibility controls new judgments/export, not access to saved history.
  const ready = !loading && !error;
  const writable = enabled && ready;
  return <>
    <button type="button" disabled={loading} onClick={() => setRevision(v => v + 1)}>저널 검토 이력 새로고침</button>
    {error ? <p role="alert">저널 검토 이력 오류: {error}. 이전 상태는 현재 상태로 확인되지 않았습니다.</p> : loading ? <p role="status">저널 검토 이력 확인 중</p> : null}
    {history ? <>
      <p>{ready ? "현재 사람의 검토 상태" : "마지막 조회된 검토 상태 (재확인 필요)"}: {history.latest ? states[history.latest.state] : "미검토"}</p>
      {selected && initialReviewId ? <SavedReview key={selected} root={root} reviewId={selected} history={history} enabled={!!writable} openManuscript onRefresh={() => setRevision(v => v + 1)}/> : null}
      <Editor key={history.candidate_digest} root={root} history={history} citations={citations} enabled={!!writable} onRefresh={() => setRevision(v => v + 1)} onSaved={reviewId => { setSelected(reviewId); setPage(1); setRevision(v => v + 1); onSaved?.(); }}/>
      <h5>저널 검토 이력 · {history.total}개</h5>
      <p>검토 기록을 연 뒤, 다른 기록을 비교 기준으로 선택하면 판단·메모의 차이를 볼 수 있습니다.</p>
      <ul>{history.items.map((item, index) => <li key={item.review_id}><button type="button" disabled={!ready} aria-pressed={selected === item.review_id} onClick={() => setSelected(item.review_id)}>{states[item.state]} · {formatTimestamp(item.created_at, preferences.timeFormat, "ko-KR")}<small className="local-report-id">{item.review_id}</small></button><V2Button type="button" disabled={!ready || selected === item.review_id} aria-pressed={baseline?.review_id === item.review_id} aria-label={`검토 ${(page - 1) * history.limit + index + 1}번을 비교 기준으로 선택`} onClick={() => setBaseline(item)}>비교 기준으로 선택</V2Button></li>)}</ul>
      <button type="button" disabled={!ready || page === 1} onClick={() => setPage(v => v - 1)}>이전 검토 페이지</button><span> {page} / {Math.max(1, Math.ceil(history.total / history.limit))} </span><button type="button" disabled={!ready || page * history.limit >= history.total} onClick={() => setPage(v => v + 1)}>다음 검토 페이지</button>
      {selected && !initialReviewId ? <SavedReview key={selected} root={root} reviewId={selected} history={history} enabled={!!writable} onRefresh={() => setRevision(v => v + 1)}/> : null}
      {baseline ? <><p>선택한 비교 기준: {states[baseline.state]} · {formatTimestamp(baseline.created_at, preferences.timeFormat, "ko-KR")}<small className="local-report-id">{baseline.review_id}</small></p><V2Button type="button" onClick={() => setBaseline(null)}>검토 비교 기준 지우기</V2Button></> : null}
      <JournalReviewComparison root={root} reviewId={selected} baselineId={baseline?.review_id || ""}/>
    </> : null}
  </>;
}

export function JournalReviewPanel(props: PanelProps) {
  const [activated, setActivated] = useState(!!props.initiallyOpen);
  return <JournalReviewDraftBoundary><details className="journal-review" open={props.initiallyOpen || undefined} onToggle={e => { if (e.currentTarget.open) setActivated(true); }}><summary>사람의 근거 검토·저널 채택</summary>{activated ? <Workspace key={props.root} {...props}/> : null}</details></JournalReviewDraftBoundary>;
}
