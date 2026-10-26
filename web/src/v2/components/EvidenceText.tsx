import { useId, useMemo, useRef, useState } from "react";
import "./EvidenceText.css";

type Match = { start: number; end: number };
const boundary = (text: string, index: number) => !(index > 0
  && text.charCodeAt(index - 1) >= 0xd800 && text.charCodeAt(index - 1) <= 0xdbff
  && text.charCodeAt(index) >= 0xdc00 && text.charCodeAt(index) <= 0xdfff);

/** Literal, overlapping matches in Unicode code points. Never normalize frozen evidence. */
export function findEvidenceMatches(text: string, query: string): Match[] {
  if (!query) return [];
  const matches: Match[] = [], length = Array.from(query).length;
  let previous = 0, offset = 0;
  for (let index = text.indexOf(query); index !== -1; index = text.indexOf(query, index + 1)) {
    if (!boundary(text, index) || !boundary(text, index + query.length)) continue;
    offset += Array.from(text.slice(previous, index)).length;
    previous = index;
    matches.push({ start: offset, end: offset + length });
  }
  return matches;
}

function Viewer({ text }: { text: string }) {
  const id = useId(), input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState(""), [selected, setSelected] = useState(0);
  const points = useMemo(() => Array.from(text), [text]);
  const matches = useMemo(() => findEvidenceMatches(text, query), [text, query]);
  const match = matches[selected], before = match ? Math.max(0, match.start - 160) : 0;
  const after = match ? Math.min(points.length, match.end + 160) : 0;
  function move(step: number) { if (matches.length) setSelected(current => (current + step + matches.length) % matches.length); }
  const original = <pre className="evidence-text-original" aria-label="보존된 근거 전체">{text}</pre>;
  return <div className="evidence-text">
    <label htmlFor={id}>근거 안에서 문구 찾기</label>
    <input ref={input} id={id} type="text" value={query} aria-describedby={`${id}-hint`} onChange={event => { setQuery(event.target.value); setSelected(0); }} onKeyDown={event => {
      if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); move(event.shiftKey ? -1 : 1); }
    }}/>
    <p id={`${id}-hint`}>대소문자·공백을 구분하는 정확한 문구 찾기입니다. 현재 보존된 근거 안에서만 찾으며, 문장의 과학적 뒷받침을 판정하지 않습니다.</p>
    {query ? <>
      <p role="status">{match ? `일치 ${selected + 1} / ${matches.length} · 근거 내부 문자 위치 ${match.start}–${match.end}` : "일치하는 문구가 없습니다. 표현이 다를 수 있으므로 근거 부족 판정과 구분하세요."}</p>
      <div className="evidence-text-actions">
        <button type="button" disabled={matches.length < 2} onClick={() => move(-1)}>이전 일치</button>
        <button type="button" disabled={matches.length < 2} onClick={() => move(1)}>다음 일치</button>
        <button type="button" onClick={() => { setQuery(""); setSelected(0); input.current?.focus(); }}>문구 찾기 지우기</button>
      </div>
      <p>위치는 이 근거 텍스트의 0부터 시작하는 유니코드 문자 기준이며 끝은 포함하지 않습니다. PDF 페이지·원래 본문 좌표와 구분합니다. Enter: 다음 · Shift+Enter: 이전.</p>
      {match ? <pre className="evidence-text-original" aria-label="일치 문구 주변 문맥">{before > 0 ? "…" : ""}{points.slice(before, match.start).join("")}<mark>{points.slice(match.start, match.end).join("")}</mark>{points.slice(match.end, after).join("")}{after < points.length ? "…" : ""}</pre> : null}
      <details><summary>보존된 근거 전체 보기</summary>{original}</details>
    </> : original}
  </div>;
}

export function EvidenceText({ text }: { text: string }) {
  // Changing the preserved source must not carry over a previous source's search or position.
  return <Viewer key={text} text={text}/>;
}
