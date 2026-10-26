import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { savePreparation, type DocumentCoverageRecord, type PreparationRequest, type SourceRange } from "../../lib/reviewPreparation";
import { V2Button } from "../V2Primitives";
import type { GapPreparationControls } from "./ReviewSourceCoverage";
import { ReviewSourcePreview } from "./ReviewSourcePreview";

type Props = {
  root: string; coverage: DocumentCoverageRecord; controls: GapPreparationControls;
  refresh: () => Promise<DocumentCoverageRecord>; onOpenPreparation?: (id: string) => void;
};
const integer = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : -1;
const key = (range: SourceRange) => `${range.start}:${range.end}`;
const overlaps = (a: SourceRange, b: SourceRange) => a.start < b.end && b.start < a.end;
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

function RangeForm({ root, coverage, controls, refresh, onOpenPreparation, range, onClose }: Props & { range: SourceRange; onClose: () => void }) {
  const id = useId(), alive = useRef(true), locked = useRef(false);
  const limit = Math.min(12000, controls.selectionLimit);
  const [start, setStart] = useState(String(range.start)), [end, setEnd] = useState(String(Math.min(range.end, range.start + Math.min(4000, limit))));
  const [previewBusy, setPreviewBusy] = useState(false), [invalidSource, setInvalidSource] = useState(false);
  const [saving, setSaving] = useState(false), [pending, setPending] = useState<PreparationRequest | null>(null);
  const [error, setError] = useState(""), [saved, setSaved] = useState("");
  const [latest, setLatest] = useState(coverage);
  const first = integer(start), last = integer(end), selected = { start: first, end: last };
  const valid = first >= range.start && last > first && last <= range.end && last - first <= limit;
  const source = coverage.extraction.source;
  const busy = saving || pending !== null || previewBusy;
  const disabled = busy || !!controls.disabledReason;
  const prepared = valid ? latest.prepared.records.filter(record => overlaps(record, selected)) : [];
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { controls.onBusyChange(busy); return () => controls.onBusyChange(false); }, [busy, controls.onBusyChange]);
  function change(a: number, b: number) { setStart(String(a)); setEnd(String(b)); setError(""); setSaved(""); }
  async function prepare() {
    if (locked.current || (!pending && (disabled || !valid || invalidSource || !!saved))) return;
    locked.current = true; setSaving(true); setError("");
    let sent = false;
    try {
      let body = pending;
      if (!body) {
        // This is an observation for planning, not an execution reservation.
        const current = await refresh();
        if (!alive.current) return;
        setLatest(current);
        if (!current.results.body_grounded.gaps.some(gap => gap.start <= first && last <= gap.end)) {
          throw new Error("선택한 구간의 저장 결과 범위가 바뀌었습니다. PDF 결과 범위를 새로고침하고 구간을 다시 선택하세요.");
        }
        const previous = new Set(prepared.map(record => record.preparation_id));
        if (current.prepared.records.some(record => overlaps(record, selected) && !previous.has(record.preparation_id))) {
          throw new Error("겹치는 준비 기록이 새로 발견됐습니다. 아래 기록과 실행 상태를 확인하세요. 별도 요청이 필요하면 준비 버튼을 다시 누르세요.");
        }
        body = { request_id: crypto.randomUUID(), run_id: source.run_id, paper_id: source.paper_id,
          expected_source_digest: source.source_digest, start: first, end: last };
        setPending(body);
      }
      // A lost ACK replays exactly the original body, even if coverage changed.
      // The existing API authorizes, verifies both extraction artifacts, and saves idempotently.
      sent = true;
      const record = await savePreparation(root, body);
      if (!alive.current) return;
      if (record.state !== "PREPARED_NOT_EXECUTED" || record.topic_id !== coverage.extraction.topic_id
        || record.paper_version_id !== coverage.extraction.paper_version_id || record.source.source_digest !== body.expected_source_digest
        || record.source.run_id !== body.run_id || record.source.paper_id !== body.paper_id
        || record.selection.start !== body.start || record.selection.end !== body.end || record.selection.chars !== body.end - body.start
        || Array.from(record.selection.text).length !== record.selection.chars || !record.preparation_id || record.execution.started !== false) {
        throw new Error("저장 응답의 출처·범위를 확인하지 못했습니다. 같은 요청으로 다시 확인하세요.");
      }
      setPending(null); setSaved(record.preparation_id); onOpenPreparation?.(record.preparation_id);
    } catch (reason) {
      if (alive.current) {
        setError(errorText(reason));
        if (sent && reason instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(reason.status)) {
          setPending(null);
          if ([404, 409].includes(reason.status)) setInvalidSource(true);
        }
      }
    } finally { locked.current = false; if (alive.current) setSaving(false); }
  }
  return <section aria-label="저장된 본문 결과가 없는 구간 확인">
    <p>다른 입력란을 바꾸지 않고 이 구간의 분석 요청을 준비합니다. 준비 저장·실행 승인·모델 실행은 별도입니다.</p>
    <p>저장 결과가 없다는 것은 미실행을 뜻하지 않습니다. 실패·취소·결과 불명 시도는 이 범위에 포함되지 않으므로 기존 실행 이력을 먼저 확인하세요.</p>
    <V2Button disabled={saving || pending !== null} onClick={onClose}>본문 결과 없는 구간 닫기</V2Button>
    {controls.disabledReason ? <p role="status">{controls.disabledReason}</p> : null}
    <form aria-label="결과 없는 구간 분석 요청 준비" onSubmit={event => { event.preventDefault(); void prepare(); }}>
      <fieldset disabled={disabled}><legend>분석 요청으로 준비할 범위</legend>
        <p id={`${id}-note`}>결과 미보존 범위 {range.start}–{range.end} 안에서 한 번에 최대 {limit.toLocaleString("ko-KR")}자를 선택하세요. Unicode 코드 포인트 기준이며 끝 위치는 포함하지 않습니다.</p>
        <div className="review-preparation-range">
          <label htmlFor={`${id}-start`}>분석 요청 시작 위치<input id={`${id}-start`} type="number" min={range.start} max={range.end - 1} step="1" value={start} aria-describedby={`${id}-note`} onChange={event => { setStart(event.target.value); setSaved(""); setError(""); }}/></label>
          <label htmlFor={`${id}-end`}>분석 요청 끝 위치 (포함하지 않음)<input id={`${id}-end`} type="number" min={range.start + 1} max={range.end} step="1" value={end} aria-describedby={`${id}-note`} onChange={event => { setEnd(event.target.value); setSaved(""); setError(""); }}/></label>
        </div>
        <V2Button type="button" disabled={!valid || last >= range.end} onClick={() => change(last, Math.min(range.end, last + last - first))}>다음 같은 길이 구간 선택</V2Button>
      </fieldset>
      {valid ? <p>선택 {first}–{last} · {last - first}자 · 대응 페이지: {coverage.pages.filter(page => overlaps(page, selected)).map(page => page.page_number).join(", ") || "페이지 사이 공백"}</p>
        : <p role="alert">선택 범위는 위 결과 미보존 범위 안에 있어야 하며 {limit.toLocaleString("ko-KR")}자를 초과할 수 없습니다.</p>}
      {prepared.length ? <section aria-label="선택 구간과 겹치는 준비 기록"><p>겹치는 준비 기록 {prepared.length}건. 준비는 분석 완료가 아닙니다. 기존 기록을 열어 실행 상태를 확인하거나 별도 요청을 명시적으로 준비할 수 있습니다.</p>
        <ul>{prepared.map(record => <li key={record.preparation_id}>{record.start}–{record.end}
          {onOpenPreparation ? <V2Button type="button" disabled={disabled} onClick={() => onOpenPreparation(record.preparation_id)}>기존 준비 기록 열기 · {record.preparation_id}</V2Button> : null}
        </li>)}</ul></section> : null}
      <V2Button type="submit" disabled={disabled || !valid || invalidSource || !!saved} busy={saving}>선택 구간 분석 요청 준비</V2Button>
    </form>
    {error ? <p role="alert">분석 요청 준비 오류: {error}. 선택 위치는 유지됩니다.</p> : null}
    {invalidSource ? <p>현재 추출 출처를 확인할 수 없습니다. 이 구간을 닫고 PDF 결과 범위를 새로고침하세요. 과거 준비 기록은 그대로 남습니다.</p> : null}
    {pending && !saving ? <><p>저장 응답 미확정입니다. 이 화면을 떠나기 전 같은 요청을 확인하세요. 재접속한 경우 새 요청을 만들기 전에 준비 이력을 확인하세요.</p><V2Button onClick={() => void prepare()}>같은 분석 준비 요청 다시 확인</V2Button></> : null}
    {saved ? <p role="status">분석 요청 준비를 저장했습니다 · {saved}. 모델 실행·승인은 수행하지 않았습니다. 아래 보존된 준비 기록에서 본문과 실행 상태를 확인하세요.</p> : null}
    {valid ? <ReviewSourcePreview key={key(selected)} root={root} source={source} windowRange={selected}
      windowLabel="선택한 결과 미보존 범위" rangeInputLabel="분석 요청" disabled={saving || pending !== null || !!controls.disabledReason || invalidSource}
      selectionLimit={limit} onBusyChange={setPreviewBusy} onSourceInvalid={() => setInvalidSource(true)}
      applyLabel="선택한 본문을 분석 요청 범위로 사용" onUseRange={value => change(value.start, value.end)}/> : null}
  </section>;
}

export function ReviewGapPreparation(props: Props) {
  const id = useId(), [chosen, setChosen] = useState(""), [opened, setOpened] = useState<SourceRange | null>(null);
  const gaps = props.coverage.results.body_grounded.gaps, selected = gaps.find(range => key(range) === chosen) ?? gaps[0];
  if (!selected) return <p>이 추출 텍스트에는 저장된 본문 결과가 없는 구간이 없습니다. 전체 논문의 검토 완료를 뜻하지 않습니다.</p>;
  return <section aria-label="저장 결과 없는 구간 준비">
    <label htmlFor={id}>살펴볼 본문 결과 없는 구간</label>
    <select id={id} value={key(selected)} disabled={props.controls.busy || !!props.controls.disabledReason} onChange={event => setChosen(event.target.value)}>
      {gaps.map(range => <option key={key(range)} value={key(range)}>{range.start}–{range.end} · {range.end - range.start}자</option>)}
    </select>
    <V2Button disabled={props.controls.busy || !!props.controls.disabledReason} onClick={() => setOpened(selected)}>선택한 본문 결과 없는 구간 열기</V2Button>
    {props.controls.disabledReason && !opened ? <p>{props.controls.disabledReason}</p> : null}
    {opened ? <RangeForm key={key(opened)} {...props} range={opened} onClose={() => setOpened(null)}/> : null}
  </section>;
}
