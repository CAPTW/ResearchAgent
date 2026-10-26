import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { previewPreparationSource, type PreparationPreview, type PreparationSource } from "../../lib/reviewPreparation";
import { V2Button } from "../V2Primitives";

const WINDOW_CHARS = 4000;
type Range = { start: number; end: number };
type Props = {
  root: string; source: PreparationSource; disabled: boolean; selectionLimit: number;
  windowRange?: Range; applyLabel?: string;
  windowLabel?: string;
  rangeInputLabel?: string;
  onBusyChange: (value: boolean) => void; onSourceInvalid: () => void; onUseRange: (range: Range) => void | string;
};

// DOM selection positions are UTF-16 offsets; the saved source uses code points.
function selectionRange(text: string, start: number, end: number): Range | null {
  const insidePair = (offset: number) => offset > 0 && offset < text.length
    && text.charCodeAt(offset - 1) >= 0xd800 && text.charCodeAt(offset - 1) <= 0xdbff
    && text.charCodeAt(offset) >= 0xdc00 && text.charCodeAt(offset) <= 0xdfff;
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > text.length || start >= end || insidePair(start) || insidePair(end)) return null;
  return { start: Array.from(text.slice(0, start)).length, end: Array.from(text.slice(0, end)).length };
}

export function ReviewSourcePreview({ root, source, disabled, selectionLimit, windowRange, windowLabel, rangeInputLabel = "본문 구간 고정", applyLabel, onBusyChange, onSourceInvalid, onUseRange }: Props) {
  const minimum = windowRange?.start ?? 0, maximum = windowRange?.end ?? source.source_chars;
  const id = useId(), sequence = useRef(0), active = useRef<AbortController | null>(null);
  const [value, setValue] = useState<PreparationPreview | null>(null), [loading, setLoading] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [requestedStart, setRequestedStart] = useState<number | null>(null), [jump, setJump] = useState(String(minimum));
  const [selection, setSelection] = useState<Range | null>(null), [selectionError, setSelectionError] = useState("");
  useEffect(() => () => { sequence.current += 1; active.current?.abort(); onBusyChange(false); }, [onBusyChange]);
  const jumpStart = /^\d+$/.test(jump) && Number.isSafeInteger(Number(jump)) ? Number(jump) : -1;
  const validJump = jumpStart >= minimum && jumpStart < maximum;
  async function read(start: number) {
    if (disabled || active.current || !Number.isSafeInteger(start) || start < minimum || start >= maximum) return;
    const length = windowRange ? Math.min(WINDOW_CHARS, maximum - start) : WINDOW_CHARS;
    const controller = new AbortController(), generation = ++sequence.current;
    active.current = controller; setLoading(true); onBusyChange(true);
    setValue(null); setSelection(null); setSelectionError(""); setError(""); setNotice(""); setRequestedStart(start); setJump(String(start));
    try {
      const result = await previewPreparationSource(root, { run_id: source.run_id, paper_id: source.paper_id, expected_source_digest: source.source_digest, start, length }, controller.signal);
      if (generation !== sequence.current || controller.signal.aborted) return;
      if (result.schema_version !== "PAPER_REVIEW_PREVIEW_V1" || result.state !== "NOT_EXECUTED"
        || result.normalization !== "XRML_WHITESPACE_V1" || !["UTF8_STRICT", "UTF8_SIG_STRICT"].includes(result.decoding)
        || result.source.run_id !== source.run_id || result.source.paper_id !== source.paper_id || result.source.source_digest !== source.source_digest
        || result.source.source_chars !== source.source_chars || result.window.start !== start
        || result.window.end !== Math.min(start + length, maximum)
        || result.window.chars !== result.window.end - start || Array.from(result.window.text).length !== result.window.chars) {
        throw new Error("요청한 출처·구간과 미리보기 응답이 일치하지 않습니다. 출처를 다시 확인하세요.");
      }
      setValue(result);
    } catch (reason) {
      if (generation !== sequence.current || controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      if (reason instanceof ApiError && [404, 409].includes(reason.status)) onSourceInvalid();
    } finally {
      if (generation === sequence.current) { active.current = null; setLoading(false); onBusyChange(false); }
    }
  }
  function cancel() {
    sequence.current += 1; active.current?.abort(); active.current = null; setLoading(false); onBusyChange(false);
    setValue(null); setSelection(null); setSelectionError(""); setError(""); setRequestedStart(null); setNotice("미리보기 읽기를 취소했습니다. 고정할 입력 위치는 유지됩니다.");
  }
  function select(textarea: HTMLTextAreaElement) {
    if (!value) return;
    const range = selectionRange(value.window.text, textarea.selectionStart, textarea.selectionEnd);
    setSelection(range ? { start: value.window.start + range.start, end: value.window.start + range.end } : null);
    setSelectionError(!range && textarea.selectionStart !== textarea.selectionEnd ? "문자 경계가 맞지 않습니다. 이모지를 포함한 문자를 온전히 선택하세요." : "");
    setNotice("");
  }
  return <section className="review-source-preview" aria-label="저장 본문 구간 미리보기">
    <h4>저장 본문 미리보기</h4>
    <p>창마다 최대 4,000자를 읽습니다. 선택은 현재 창 안에서만 가능합니다. 창 경계나 더 긴 구간은 {rangeInputLabel}의 시작·끝 위치를 직접 입력하세요(최대 {selectionLimit.toLocaleString("ko-KR")}자).</p>
    <p>키보드로 현재 창 전체를 선택하려면 본문에 포커스를 두고 Ctrl+A(맥에서는 ⌘+A)를 누르세요. 읽기 전용 본문에서 방향키 선택이 지원되지 않으면 시작·끝 위치를 직접 입력할 수 있습니다.</p>
    <p>미리보기는 모델 실행·구간 저장을 하지 않습니다. 공백 정규화(XRML_WHITESPACE_V1) 후 본문이며 원본 파일의 문자·바이트 위치와 다를 수 있습니다.</p>
    {windowRange ? <p>{windowLabel ?? "선택한 미준비 범위"} {minimum}–{maximum} 안에서만 읽습니다. 아래에서 선택한 텍스트만 입력에 반영하며, 이 범위 전체를 저장하거나 검토하지 않습니다.</p> : null}
    <V2Button type="button" disabled={disabled || loading} onClick={() => void read(minimum)}>본문 미리보기</V2Button>
    <div className="review-source-preview-jump">
      <label htmlFor={`${id}-jump`}>미리보기 시작 위치<input id={`${id}-jump`} type="number" min={minimum} step="1" max={Math.max(minimum, maximum - 1)} value={jump} disabled={disabled || loading} onChange={event => setJump(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); if (validJump) void read(jumpStart); } }}/></label>
      <V2Button type="button" disabled={disabled || loading || !validJump} onClick={() => void read(jumpStart)}>이 위치 미리보기</V2Button>
    </div>
    {loading ? <><p role="status">본문 미리보기 읽는 중</p><V2Button type="button" onClick={cancel}>미리보기 요청 취소</V2Button></> : null}
    {error ? <><p role="alert">본문 미리보기 오류: {error}</p><V2Button type="button" disabled={disabled || requestedStart === null} onClick={() => { if (requestedStart !== null) void read(requestedStart); }}>본문 미리보기 다시 시도</V2Button></> : null}
    {value ? <>
      <p id={`${id}-coordinates`}>미리보기 구간: {value.window.start}–{value.window.end} · {value.window.chars}자 / 저장 본문 {source.source_chars}자. Unicode 코드 포인트 기준, 0부터 시작하며 끝 위치는 포함하지 않습니다.</p>
      <label htmlFor={`${id}-body`}>저장 본문 미리보기</label>
      <textarea key={value.window.start} id={`${id}-body`} readOnly rows={12} spellCheck={false} value={value.window.text} aria-describedby={`${id}-coordinates`} onSelect={event => select(event.currentTarget)}/>
      <nav aria-label="본문 미리보기 페이지">
        <V2Button type="button" disabled={disabled || value.window.start === minimum} onClick={() => void read(Math.max(minimum, value.window.start - WINDOW_CHARS))}>이전 본문 구간</V2Button>
        <V2Button type="button" disabled={disabled || value.window.end >= maximum} onClick={() => void read(value.window.end)}>다음 본문 구간</V2Button>
      </nav>
      {selectionError ? <p role="alert">{selectionError}</p> : selection ? <p>선택 위치: {selection.start}–{selection.end} · {selection.end - selection.start}자</p> : <p>본문에서 마우스나 키보드로 사용할 구간을 선택하세요.</p>}
      <V2Button type="button" disabled={disabled || !selection || selection.end - selection.start > selectionLimit} onClick={() => { if (selection) {
        const problem = onUseRange(selection);
        if (problem) { setSelectionError(problem); setNotice(""); }
        else { setSelectionError(""); setNotice("고정할 입력 위치에 반영했습니다. 저장하려면 선택 구간 고정을 누르세요."); }
      } }}>{applyLabel ?? "선택한 본문을 고정 범위로 사용"}</V2Button>
    </> : null}
    {notice ? <p role="status">{notice}</p> : null}
  </section>;
}
