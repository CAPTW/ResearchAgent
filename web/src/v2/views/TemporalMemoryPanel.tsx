import { Link } from "react-router-dom";
import { eventTypes, type ChangeEvent } from "../../lib/temporalMemory";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { useTemporalMemory } from "../useTemporalMemory";
import { V2Button } from "../V2Primitives";

const labels = ["논문 추가", "논문 제거", "버전 변경", "컬렉션 변경", "검토 상태 변경", "전문 상태 변경", "근거 변경", "권리 상태 변경"];
function provenanceAvailable(event: ChangeEvent) {
  return Boolean(event.source_snapshot_id && event.from_snapshot_id && event.to_snapshot_id && event.algorithm_version
    && event.acceptance_state && event.source_entity_ids?.length && Array.isArray(event.source_evidence_ids));
}

export function TemporalMemoryPanel({ topicId }: { topicId: string }) {
  const state = useTemporalMemory(topicId);
  return <TemporalMemoryView state={state} />;
}

export function TemporalMemoryView({ state }: { state: ReturnType<typeof useTemporalMemory> }) {
  const { preferences } = useOptionalAppearance();
  const format = (value: string) => formatTimestamp(value, preferences.timeFormat ?? "locale", "ko-KR");
  const latest = state.selected;
  const delta = state.delta.data;
  return <section className="v2-panel v2-temporal-panel" aria-labelledby="temporal-memory-heading">
    <h2 id="temporal-memory-heading">이전 스냅샷 이후 변경</h2>
    <p>로컬에서 직접 캡처한 주제 상태를 비교합니다. 자동 모니터링이나 연구 분야 전체의 변화를 뜻하지 않습니다.</p>
    <V2Button busy={state.busy} disabled={state.busy} aria-busy={state.busy} onClick={() => void state.capture()}>현재 상태 스냅샷 만들기</V2Button>
    <V2Button disabled={state.busy} onClick={() => void state.refresh()}>스냅샷 이력 새로고침</V2Button>
    <p role="status" aria-live="polite">{state.message}</p>
    {state.captureError ? <p role="alert">스냅샷 생성 실패: {state.captureError}</p> : null}
    {state.list.phase === "loading" ? <p role="status">스냅샷 목록을 불러오는 중입니다.</p> : null}
    {state.list.phase === "error" || state.list.phase === "unavailable" ? <div role="alert"><p>{state.list.phase === "unavailable" ? "스냅샷 목록 사용 불가" : "스냅샷 목록 오류"}: {state.list.message}</p><V2Button onClick={() => void state.refresh()}>목록 다시 불러오기</V2Button></div> : null}
    {state.list.phase === "ready" && !latest ? <p>아직 이 주제의 스냅샷을 캡처하지 않았습니다.</p> : null}
    {latest ? <>
      <label>스냅샷 이력 <select className="v2-input" style={{maxWidth:"100%"}} disabled={state.busy} value={latest.snapshot_id} onChange={e=>state.select(e.target.value)}>
        {!state.list.data?.items.some(item=>item.snapshot_id===latest.snapshot_id) ? <option value={latest.snapshot_id}>캡처가 반환한 기존 기록 · {format(latest.created_at)}</option> : null}
        {state.list.data?.items.map(item=><option key={item.snapshot_id} value={item.snapshot_id}>{format(item.created_at)} · {item.paper_count}개 버전 · {item.snapshot_id.slice(-8)}</option>)}
      </select></label>
      <p>선택 스냅샷: <time dateTime={latest.created_at}>{format(latest.created_at)}</time> · 논문 버전 {latest.paper_count}개</p>
      {!latest.parent_snapshot_id ? <p>{state.list.data?.total===1 ? "스냅샷이 하나 있습니다. 나중 상태를 캡처하면 변경을 비교할 수 있습니다." : "선택한 첫 스냅샷에는 이전 비교 기준이 없습니다."}</p> : <>
        {state.delta.phase === "loading" ? <p role="status">스냅샷 변경을 불러오는 중입니다.</p> : null}
        {state.delta.phase === "error" || state.delta.phase === "unavailable" ? <div role="alert"><p>{state.delta.phase === "unavailable" ? "변경 비교 사용 불가" : "변경 비교 오류"}: {state.delta.message}</p><V2Button onClick={() => void state.refresh()}>비교 다시 불러오기</V2Button></div> : null}
        {delta ? <>
          <p>이전 스냅샷: <time dateTime={delta.from_snapshot.created_at}>{format(delta.from_snapshot.created_at)}</time></p>
          <dl>{eventTypes.map((type, index) => <div key={type}><dt>{labels[index]}</dt><dd data-event-count={type}>{delta.counts[type]}</dd></div>)}</dl>
          {delta.membership_only_changed_paper_count > 0 ? <p>추가로 출처 멤버십만 변경된 논문이 {delta.membership_only_changed_paper_count}개 있습니다.</p> : null}
          {!delta.events.length ? <p>이 두 스냅샷 사이에서 비교 대상 8종 상태의 변경이 감지되지 않았습니다.</p> : <ol aria-label="스냅샷 변경 이벤트">{delta.events.map(event => <li key={event.event_id}>
            {provenanceAvailable(event) ? <>
              <strong>{event.event_type}</strong> · <span>{event.canonical_paper_id}</span>
              {event.paper_destination ? <Link to={event.paper_destination}>논문 버전 {event.paper_version_id}</Link> : <span>이전 논문 버전 {event.paper_version_id} · 현재 목적지 사용 불가</span>}
              {event.evidence_destinations?.map(item => <Link key={item.id} to={item.destination}>근거 {item.id}</Link>)}
              {event.destination_reason === "CURRENT_DESTINATIONS_UNAVAILABLE" ? <p>현재 논문·근거 목록을 확인할 수 없습니다. 저장된 변경 기록은 유지됩니다. 비교 다시 불러오기로 재시도하세요.</p> : null}
              {event.unavailable_evidence_ids?.length ? <p>현재 원문이 없거나 캡처 당시 내용과 달라 열 수 없는 근거: {event.unavailable_evidence_ids.join(", ")}. 저장된 변경 내용과 근거 식별자는 유지됩니다.</p> : null}
              <details><summary>변경 근거와 해시</summary><p>{event.before_digest} → {event.after_digest}</p><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{event.before_state}{"\n→\n"}{event.after_state}</pre></details>
            </> : <span>출처가 불완전하여 이 변경 항목을 사용할 수 없습니다.</span>}
          </li>)}</ol>}
          {delta.events.some(event => event.destination_reason === "CURRENT_DESTINATIONS_UNAVAILABLE") ? <V2Button onClick={() => void state.refresh()}>비교 다시 불러오기</V2Button> : null}
        </> : null}
      </>}
      <details><summary>스냅샷 출처와 알고리즘</summary><dl style={{ overflowWrap: "anywhere" }}>
        <dt>현재 스냅샷</dt><dd>{latest.snapshot_id}</dd><dt>이전 스냅샷</dt><dd>{latest.parent_snapshot_id ?? "해당 없음"}</dd>
        <dt>Corpus digest</dt><dd>{latest.corpus_digest}</dd><dt>Source revision</dt><dd>{latest.source_revision}</dd>
        <dt>Algorithm</dt><dd>{delta?.algorithm_version ?? latest.algorithm_version}</dd><dt>Acceptance</dt><dd>{latest.acceptance_state}</dd>
      </dl></details>
    </> : null}
    {state.list.data && state.list.data.total>25 ? <nav aria-label="스냅샷 이력 페이지">
      <V2Button disabled={state.busy||state.page<=1} onClick={()=>state.changePage(state.page-1)}>이전 스냅샷 페이지</V2Button>
      <span> {state.page} / {Math.ceil(state.list.data.total/25)} </span>
      <V2Button disabled={state.busy||state.page*25>=state.list.data.total} onClick={()=>state.changePage(state.page+1)}>다음 스냅샷 페이지</V2Button>
    </nav> : null}
  </section>;
}
