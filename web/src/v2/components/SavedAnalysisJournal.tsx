import { useEffect, useRef, useState } from 'react';
import { ApiError, fetchJson } from '../../lib/api';
import { analysisRoot, type SavedAnalysis } from '../savedAnalyses';
import { JournalReviewPanel } from '../views/JournalReviewPanel';
import { useOptionalAppearance } from '../AppearanceProvider';
import { formatTimestamp } from '../appearance';
import { V2Button } from '../V2Primitives';

type Component = { component_id: string; label: string; text: string; eligible: boolean };
type Command = { expected_decision_id: string; component_ids: string[] };
type Candidate = { candidate_id: string; created_at: string; selected_component_ids: string[];
  source: { decision: { decision_id: string; note: string } };
  proposal: { title: string; sections: { kind: string; entries: { kind: string; text: string; citation_ids: string[] }[] }[] };
  citations: { citation_id: string; title: string; text: string; locator: unknown }[] };
type Listing = { items: { candidate_id: string; created_at: string; title: string }[]; total: number; limit: number };
const notice = '새 모델 호출 없이 저장된 분석 문장을 선택해 후보로 만듭니다. 분석은 모델의 해석이며, 인용은 응답 전체의 근거 집합입니다. 문장별 뒷받침·전체 논문 검토·과학적 검증은 확인되지 않았습니다.';
const prose = { whiteSpace: 'pre-wrap' as const, overflowWrap: 'anywhere' as const };

function Workspace({ topicId, record }: { topicId: string; record: SavedAnalysis }) {
  const root = `${analysisRoot(topicId)}/${encodeURIComponent(record.analysis_id)}/journals`;
  const [components, setComponents] = useState<Component[]>([]), [selected, setSelected] = useState<string[]>([]);
  const [list, setList] = useState<Listing | null>(null), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [candidateId, setCandidateId] = useState(''), [candidate, setCandidate] = useState<Candidate | null>(null);
  const [attempt, setAttempt] = useState<Command | null>(null), [busy, setBusy] = useState(false), [loading, setLoading] = useState(true);
  const [error, setError] = useState(''), [readError, setReadError] = useState(''), [detailError, setDetailError] = useState('');
  const alive = useRef(true), locked = useRef(false);
  const { preferences } = useOptionalAppearance();
  const time = (value: string) => formatTimestamp(value, preferences.timeFormat, 'ko-KR');
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setReadError('');
    void Promise.all([fetchJson<{ items: Component[] }>(root + '/components'), fetchJson<Listing>(`${root}?page=${page}`)]).then(([choices, rows]) => {
      if (!cancelled) { setComponents(choices.items); setList(rows); setLoading(false); }
    }, () => { if (!cancelled) { setReadError('후보와 선택 항목을 불러오지 못했습니다. 다시 확인하세요.'); setLoading(false); } });
    return () => { cancelled = true; };
  }, [root, page, revision]);
  useEffect(() => {
    let cancelled = false; setCandidate(null); setDetailError('');
    if (candidateId) void fetchJson<Candidate>(`${root}/${encodeURIComponent(candidateId)}`).then(value => {
      if (!cancelled) setCandidate(value);
    }, () => { if (!cancelled) setDetailError('후보 상세를 불러오지 못했습니다. 이력 새로고침으로 다시 확인하세요.'); });
    return () => { cancelled = true; };
  }, [root, candidateId, revision]);
  const reviewed = record.latest_decision?.state === 'reviewed';
  const currentSource = reviewed && candidate?.source.decision.decision_id === record.latest_decision?.decision_id;
  const ready = !loading && !readError;
  async function create() {
    if (locked.current || !ready || (!attempt && (!reviewed || !selected.length || selected.length > 30))) return;
    const body = attempt ?? { expected_decision_id: record.latest_decision!.decision_id, component_ids: [...selected] };
    locked.current = true; setBusy(true); setAttempt(body); setError('');
    try {
      const value = await fetchJson<Candidate>(root, { method: 'POST', body: JSON.stringify(body) });
      if (alive.current) { setAttempt(null); setCandidateId(value.candidate_id); setPage(1); setRevision(v => v + 1); }
    } catch (e) {
      if (alive.current) {
        const refused = e instanceof ApiError && (e.status === 409 || e.status === 422);
        if (refused) setAttempt(null);
        setError(refused ? '후보 저장이 거부되었습니다. 분석 검토 이력과 선택 범위를 확인한 뒤 다시 저장하세요.' : '저장 결과를 확인하지 못했습니다. 같은 후보 저장을 재시도하거나 이력을 새로고침하세요. 중복 후보는 생성되지 않습니다.');
      }
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  return <section className="saved-analysis-journal" aria-label="분석 Journal 후보 작업">
    <p>{notice}</p><p>개인 분석 보관함 안에 저장되며 주제 공용 Journal 목록과는 별개입니다. 후보 생성은 채택이 아닙니다.</p>
    {!reviewed ? <p>먼저 분석의 근거와 한계를 읽고 사람의 검토 상태를 ‘직접 확인함’으로 저장하세요.</p> : null}
    <fieldset disabled={busy || !!attempt || !ready}><legend>원고에 포함할 분석 항목 · 최대 30개</legend>
      {components.map(item => <div key={item.component_id}><label className="analysis-journal-choice"><input type="checkbox" aria-label={`${item.label} 선택`} disabled={busy || !!attempt || !ready || !item.eligible} checked={selected.includes(item.component_id)} onChange={event => setSelected(old => event.target.checked ? [...old, item.component_id] : old.filter(id => id !== item.component_id))}/>{item.label}</label><p style={prose}>{item.text}</p>{!item.eligible ? <p>비어 있거나 항목당 3,000자를 초과해 이 후보 형식으로 포함할 수 없습니다. 원본 분석은 보존됩니다.</p> : null}</div>)}
    </fieldset>
    <V2Button type="button" disabled={busy || !ready || (!attempt && (!reviewed || !selected.length || selected.length > 30))} onClick={() => void create()}>{attempt ? '같은 후보 저장 재시도' : '선택 분석으로 후보 저장'}</V2Button>
    <V2Button type="button" disabled={busy || loading} onClick={() => setRevision(v => v + 1)}>분석 후보 이력 새로고침</V2Button>
    {attempt && !busy ? <V2Button type="button" onClick={() => { setAttempt(null); setError('재시도 입력을 해제했습니다. 서버에 저장된 후보는 삭제되지 않습니다. 이력에서 확인하세요.'); setRevision(v => v + 1); }}>후보 저장 재시도 취소</V2Button> : null}
    {loading ? <p role="status">분석 후보 이력 확인 중</p> : null}
    {error || readError || detailError ? <p role="alert">{error || readError || detailError}</p> : null}
    {list ? <><h5>이 분석의 후보 이력 · {list.total}개</h5><ul>{list.items.map(item => <li key={item.candidate_id}><V2Button type="button" disabled={busy} aria-pressed={candidateId === item.candidate_id} onClick={() => setCandidateId(item.candidate_id)}>후보 열기 · {time(item.created_at)}</V2Button><small style={prose}>{item.candidate_id}</small></li>)}</ul>
      <V2Button type="button" disabled={loading || page <= 1} onClick={() => setPage(v => v - 1)}>이전 분석 후보 페이지</V2Button><span>{page} 페이지</span><V2Button type="button" disabled={loading || page * list.limit >= list.total} onClick={() => setPage(v => v + 1)}>다음 분석 후보 페이지</V2Button></> : null}
    {candidate ? <article aria-label="분석에서 만든 Journal 후보">
      <h5>{candidate.proposal.title}</h5><p>{time(candidate.created_at)} · 선택한 분석 항목 {candidate.selected_component_ids.length}개</p>
      <p style={prose}>기준 분석 판단: {candidate.source.decision.decision_id}</p><p style={prose}>{candidate.source.decision.note}</p>
      {!currentSource ? <p role="status">분석의 검토 기준이 바뀌어 이 후보의 새 검토·채택·내보내기는 중지되었습니다. 과거 기록은 읽을 수 있습니다. 최신 분석을 확인하고 새 후보를 만드세요.</p> : null}
      <JournalReviewPanel root={`${root}/${encodeURIComponent(candidate.candidate_id)}/reviews`} citations={candidate.citations} enabled={!!currentSource && ready} initiallyOpen/>
      <details><summary>후보 원문과 보존 근거</summary>{candidate.proposal.sections.flatMap(section => section.entries).map((entry, index) => <p key={index} style={prose}>{entry.text}</p>)}
        {candidate.citations.map(citation => <section key={citation.citation_id}><h6>{citation.title}</h6><blockquote style={prose}>{citation.text}</blockquote><p style={prose}>위치: {JSON.stringify(citation.locator)}</p></section>)}
      </details>
    </article> : null}
  </section>;
}

export function SavedAnalysisJournal(props: { topicId: string; record: SavedAnalysis }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open) setOpen(true); }}><summary>분석으로 Journal 후보 만들기</summary>{open ? <Workspace key={`${props.topicId}:${props.record.analysis_id}`} {...props}/> : null}</details>;
}
