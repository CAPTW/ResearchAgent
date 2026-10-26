import { useEffect, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";

export type WorkingContent = { base_version_id: string | null; title: string; entries: { kind: "observation" | "interpretation" | "question"; text: string; citation_ids: string[] }[] };
type Saved = { head_id: string | null; copy: null | { copy_id: string; created_at: string; content: WorkingContent } };
type Request = { request_id: string; expected_copy_id: string | null; expected_input_digest: string; content: WorkingContent | null };
type Props = { url: string; inputDigest: string; content: WorkingContent; disabled: boolean; onRestore: (content: WorkingContent) => void; onBusy: (busy: boolean) => void };

function Workspace({ url, inputDigest, content, disabled, onRestore, onBusy }: Props) {
  const { preferences } = useOptionalAppearance();
  const [saved, setSaved] = useState<Saved | null>(null), [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [readError, setReadError] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [pending, setPending] = useState<Request | null>(null);
  const alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setReadError("");
    void fetchJson<Saved>(url).then(value => {
      if (!cancelled) { setSaved(value); setLoading(false); setPending(null); setError(""); }
    }, reason => { if (!cancelled) { setReadError(String(reason)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [url, revision]);
  const ready = !disabled && !loading && !readError && saved !== null;
  async function save(value: WorkingContent | null) {
    if (!ready || locked.current || !saved) return;
    const body = pending ?? { request_id: crypto.randomUUID(), expected_copy_id: saved.head_id, expected_input_digest: inputDigest, content: value };
    locked.current = true; setBusy(true); onBusy(true); setPending(body); setError(""); setNotice("");
    try {
      const result = await fetchJson<{ working_copy: Saved; created: boolean }>(url, { method: "POST", body: JSON.stringify(body) });
      if (alive.current) {
        setSaved(result.working_copy); setPending(null);
        setNotice(body.content ? "요청 당시 입력을 보관했습니다. 이후 편집은 다시 보관해야 복구됩니다." : "불러올 보관본을 해제했습니다. 현재 입력과 초안 버전, 과거 보관 기록은 유지됩니다.");
      }
    } catch (reason) {
      if (alive.current) {
        const definitive = reason instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(reason.status);
        if (definitive) { setPending(null); setReadError("보관 상태를 새로 확인해야 합니다."); }
        setError(definitive ? "보관 요청이 거절되었습니다. 현재 입력은 유지됩니다. 보관 상태를 새로고침하고 다시 확인하세요."
          : `보관 응답 확인 실패: ${String(reason)}. 재시도는 요청 당시 입력을 그대로 보냅니다. 현재 입력은 자동 저장되지 않습니다.`);
      }
    } finally { locked.current = false; if (alive.current) { setBusy(false); onBusy(false); } }
  }
  return <section aria-label="작성 중 입력 보관과 복구">
    <p>제목이나 인용이 미완성이어도 현재 사용자·고정 입력에 연결해 보관합니다. 자동 저장이 아니며 초안 버전·검토·채택 기록을 만들지 않습니다.</p>
    <button type="button" disabled={disabled || busy || loading} onClick={() => { setNotice(""); setRevision(n => n + 1); }}>보관 상태 새로고침</button>
    {loading ? <p role="status">보관 상태 확인 중</p> : readError ? <p role="alert">보관 조회 오류: {readError}. 보관·복구를 할 수 없습니다.</p> : saved?.copy ?
      <p>마지막 확인한 보관 시각: {formatTimestamp(saved.copy.created_at, preferences.timeFormat, "ko-KR")} · 편집 기준: <span className="local-report-id">{saved.copy.content.base_version_id || "첫 버전"}</span></p>
      : <p>현재 사용자에게 불러올 보관본이 없습니다.</p>}
    <button type="button" disabled={!ready || busy || pending !== null} onClick={() => void save(content)}>현재 작성 중 입력 보관</button>
    <button type="button" disabled={!ready || busy || pending !== null || !saved?.copy} onClick={() => {
      if (saved?.copy) { onRestore(saved.copy.content); setNotice("현재 입력을 보관본으로 교체했습니다. 편집 기준과 인용을 확인한 뒤 초안 버전으로 저장하세요."); }
    }}>현재 입력을 보관본으로 교체</button>
    <button type="button" disabled={!ready || busy || pending !== null || !saved?.copy} onClick={() => void save(null)}>불러올 보관본 해제</button>
    {pending ? <button type="button" disabled={!ready || busy} onClick={() => void save(pending.content)}>같은 보관 요청 다시 보내기</button> : null}
    <p>불러오기와 해제는 명시적으로 선택합니다. 해제해도 과거 보관 기록은 삭제하지 않습니다. 최신 초안 버전과 편집 기준이 다르면 입력을 유지한 채 기준을 다시 선택해야 합니다.</p>
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
  </section>;
}

export function JournalWorkingCopy(props: Props) {
  const [active, setActive] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open) setActive(true); }}><summary>작성 중 입력 보관·복구</summary>{active ? <Workspace key={props.url} {...props}/> : null}</details>;
}
