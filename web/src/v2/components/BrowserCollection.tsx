import { useEffect, useRef, useState } from "react";
import { ApiError, fetchJson, getApiBase } from "../../lib/api";
import { V2Button } from "../V2Primitives";
import "./BrowserCollection.css";

export type Candidate = { arxiv_id: string; title: string; authors: string[]; published: string; abstract: string; source_url: string; pdf_url: string };
type Item = { item_id: string; candidate: Candidate; state: "queued" | "downloading" | "auth_required" | "interrupted" | "awaiting_confirmation" | "saved" | "failed";
  active_attempt_id: string | null; attempts: { attempt_id: string; download_id: number | null }[];
  pdf: { sha256: string; bytes: number; pages?: number } | null;
  saved: { paper_version_id: string; attachment_id: string } | null };
type Job = { job_id: string; topic_id: string; query: string; query_rejected?: boolean; items: Item[]; created_at: string;
  external_ai_state: string; connection: { state: string; expires_at: string | null } };
type Search = { query: string; candidates: Candidate[]; retrieved_at: string; source: string; search_url: string; search_receipt_id?: string; cached?: boolean };
type Props = { topicId: string; onSaved?: () => Promise<void>; onOpenPaper?: (version: string) => void };
const labels: Record<Item["state"], string> = { queued: "브라우저 다운로드 대기", downloading: "다운로드 진행 중", auth_required: "브라우저 로그인 필요",
  interrupted: "중단됨", awaiting_confirmation: "PDF 확인 후 저장 대기", saved: "Papers에 저장됨", failed: "파일 확인 실패" };
const drafts = new Map<string, { query: string; search: Search | null; selected: string[]; attempt: object | null; rejected: boolean }>();
const post = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const safeId = (value: string) => /^[a-zA-Z0-9_-]{1,100}$/.test(value);
const connectionHelp = "연결 코드나 인증 정보는 검색할 수 없습니다. Whale 오른쪽 위 확장 메뉴에서 XRML Paper Collector를 열고 ‘앱의 작업 연결 코드’ 칸에 붙여넣으세요.";
// A paste mistake must not enter a search request or the navigation draft cache.
function sensitiveQuery(value: string) {
  const fields = new Set([...value.matchAll(/(?<![\w])["']?(base_url|job_id|token)["']?\s*[:=]/gi)].map(match => match[1].toLowerCase()));
  return ["base_url", "job_id", "token"].every(field => fields.has(field))
    || /\bbearer\s+[a-z0-9._~+/=-]{16,}/i.test(value)
    || /(?<![\w])["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|password|secret|token)["']?\s*[:=]\s*["']?[a-z0-9._~+/=-]{16,}/i.test(value)
    || /\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,})/i.test(value)
    || /\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}/i.test(value);
}
const visibleQuery = (value: string) => sensitiveQuery(value) ? "[연결 정보가 포함된 검색식 숨김]" : value;
function readJob(value: Job, topic: string) {
  if (value.topic_id !== topic || !safeId(value.job_id) || !Array.isArray(value.items)
      || value.items.some(item => !safeId(item.item_id) || !(item.state in labels))) throw Error("수집 작업 응답을 확인할 수 없습니다.");
  return value;
}
function safeSource(url: string) { return /^https:\/\/arxiv\.org\/abs\/\d{4}\.\d{4,5}v[1-9]\d*$/.test(url) ? url : undefined; }

function Workspace({ topicId, onSaved, onOpenPaper }: Props) {
  const root = `/api/topics/${encodeURIComponent(topicId)}/browser-collections`;
  const remembered = drafts.get(topicId);
  const initial = remembered && !sensitiveQuery(remembered.query) && !sensitiveQuery(remembered.search?.query ?? "")
    ? remembered : { query: "", search: null, selected: [], attempt: null, rejected: false };
  const [query, setQuery] = useState(initial.query);
  const [search, setSearch] = useState<Search | null>(initial.search);
  const [selected, setSelected] = useState<string[]>(initial.selected);
  const attempt = useRef<object | null>(initial.attempt);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [active, setActive] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [ticket, setTicket] = useState<{ job: string; value: string; expires: string } | null>(null);
  const [confirmed, setConfirmed] = useState<Record<string, string>>({});
  const [createRejected, setCreateRejected] = useState(initial.rejected);
  const job = jobs.find(value => value.job_id === active);
  useEffect(() => { drafts.set(topicId, { query, search, selected, attempt: attempt.current, rejected: createRejected }); }, [topicId, query, search, selected, busy, createRejected]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function refresh() {
    const listing = await fetchJson<{ items: Job[] }>(root, { cache: "no-store" });
    if (!Array.isArray(listing.items)) throw Error("수집 작업 목록을 확인할 수 없습니다.");
    const values = listing.items.map(value => readJob(value, topicId));
    if (mounted.current) {
      setJobs(values); setActive(previous => values.some(value => value.job_id === previous) ? previous : values[0]?.job_id ?? ""); setFresh(true);
    }
    return values;
  }
  async function action(operation: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice("");
    try { await operation(); }
    catch (failure) { if (mounted.current) { setFresh(false); setError(failure instanceof Error ? failure.message : "요청 상태를 확인하지 못했습니다. 기록을 새로고침하세요."); } }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  }
  useEffect(() => { void action(async () => { await refresh(); }); }, [root]); // read only; no background collection

  async function find() {
    const requested = query.trim();
    if (sensitiveQuery(requested)) { setQuery(""); setSearch(null); setSelected([]); throw Error(connectionHelp); }
    const result = await fetchJson<Search>(root + "/search", post({ query: requested, max_results: 5 }));
    if (result.query !== requested || !Array.isArray(result.candidates)) throw Error("검색 응답 범위를 확인할 수 없습니다.");
    if (mounted.current) { setSearch(result); setSelected([]); attempt.current = null; setNotice("공식 arXiv 검색 결과입니다. 수집할 논문을 선택하세요."); }
  }
  async function create() {
    if (!search) return;
    if (sensitiveQuery(search.query)) throw Error(connectionHelp);
    const command = attempt.current ?? { request_id: crypto.randomUUID(), query: search.query,
      ...(search.search_receipt_id ? { search_receipt_id: search.search_receipt_id } : {}),
      allowed_hosts: ["arxiv.org", "export.arxiv.org"], candidates: search.candidates.filter(item => selected.includes(item.arxiv_id)) };
    attempt.current = command;
    setCreateRejected(false); // A new request can have an unknown outcome even after a prior rejection.
    let saved: Job;
    try { saved = readJob(await fetchJson<Job>(root, post(command)), topicId); }
    catch (failure) {
      if (mounted.current && failure instanceof ApiError && [400, 403, 404, 409, 422].includes(failure.status)) setCreateRejected(true);
      throw failure;
    }
    attempt.current = null;
    if (mounted.current) setCreateRejected(false);
    await refresh();
    if (mounted.current) { setActive(saved.job_id); setNotice("수집 작업을 저장했습니다. 브라우저를 연결해 선택한 논문을 다운로드하세요."); }
  }
  async function connect() {
    if (!job) return;
    if (job.query_rejected || sensitiveQuery(job.query)) throw Error(connectionHelp);
    setTicket(null); // A replacement may revoke the old code even if its reply is lost.
    const base = new URL(getApiBase() || window.location.origin, window.location.origin);
    if (base.hostname === "localhost") base.hostname = "127.0.0.1";
    if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !base.port || !["", "/"].includes(base.pathname)) throw Error("브라우저 연결은 로컬 데스크톱 주소에서 사용할 수 있습니다.");
    const value = await fetchJson<{ job_id: string; token: string; expires_at: string }>(`${root}/${job.job_id}/connection`, post({}));
    if (value.job_id !== job.job_id || !/^[a-zA-Z0-9_-]{43}$/.test(value.token)) throw Error("연결 응답을 확인할 수 없습니다.");
    if (mounted.current) setTicket({ job: job.job_id, expires: value.expires_at, value: JSON.stringify({ base_url: base.origin, job_id: job.job_id, token: value.token }) });
    await refresh();
  }
  async function control(item: Item, operation: "pause" | "resume") {
    if (!job) return;
    await fetchJson(`${root}/${job.job_id}/items/${item.item_id}/control`, post({ action: operation }));
    await refresh();
  }
  async function save(item: Item) {
    if (!job || !item.pdf || confirmed[item.item_id] !== item.pdf.sha256) return;
    await fetchJson(`${root}/${job.job_id}/items/${item.item_id}/confirm`, post({ expected_pdf_sha256: item.pdf.sha256, identity_confirmed: true }));
    await refresh();
    try { await onSaved?.(); }
    catch { if (mounted.current) setNotice("수집 기록은 저장되었습니다. Papers 목록을 새로고침해 주세요."); return; }
    if (mounted.current) setNotice("Papers에 저장했습니다. 논문을 열어 기존 PDF 추출·분석·Journal 흐름을 이어갈 수 있습니다.");
  }
  return <div className="browser-collection">
    <p>Whale 확장 프로그램으로 선택한 arXiv 논문을 가져옵니다. 검색어는 arXiv 공식 API에 전송되며, PDF 수집은 별도 동작입니다. AI 전송은 실행하지 않습니다.</p>
    <p>현재 개발 경로입니다. 실제 Whale 연결·로그인 필요 사이트의 검증은 아직 완료되지 않았습니다.</p>
    <form onSubmit={event => { event.preventDefault(); void action(find); }}>
      <label htmlFor="browser-collection-query">arXiv 검색식</label>
      <input id="browser-collection-query" value={query} maxLength={500} disabled={busy || !!attempt.current}
        onPaste={event => { if (sensitiveQuery(event.clipboardData.getData("text"))) { event.preventDefault(); setQuery(""); setSearch(null); setSelected([]); setError(connectionHelp); } }}
        onChange={event => { if (sensitiveQuery(event.target.value)) { setQuery(""); setSearch(null); setSelected([]); setError(connectionHelp); } else { setQuery(event.target.value); setError(""); } }}
        placeholder='예: all:"virtual reality" AND all:"gaze"' aria-describedby="browser-query-help"/>
      <V2Button type="submit" disabled={busy || !query.trim() || !!attempt.current}>공식 API로 검색</V2Button>
      <small id="browser-query-help" style={{ gridColumn: "1 / -1" }}>논문 주제나 검색식만 입력하세요. 브라우저 연결 코드는 확장 팝업에 붙여넣습니다.</small>
    </form>
    {search ? <section aria-label="수집 후보"><p>검색식: {visibleQuery(search.query)} · 조회: {search.retrieved_at}</p>
      {search.candidates.length ? search.candidates.map(candidate => <label className="collection-candidate" key={candidate.arxiv_id}>
        <input type="checkbox" checked={selected.includes(candidate.arxiv_id)} disabled={busy || !!attempt.current} onChange={event => setSelected(old => event.target.checked ? [...old, candidate.arxiv_id] : old.filter(id => id !== candidate.arxiv_id))}/>
        <span>{candidate.title}<small>{candidate.arxiv_id} · 출판일 {candidate.published}</small></span>
      </label>) : <p>검색 조건에 맞는 지원 버전의 논문이 없습니다.</p>}
      <V2Button disabled={busy || !selected.length} onClick={() => void action(create)}>{attempt.current ? "같은 수집 작업 저장 확인" : "선택한 논문 수집 작업 저장"}</V2Button>
      {createRejected ? <V2Button disabled={busy} onClick={() => { attempt.current = null; setCreateRejected(false); setError(""); setNotice("거절된 요청을 해제했습니다. 검색 조건과 선택을 수정할 수 있습니다."); }}>거절된 요청 해제하고 수정</V2Button> : null}
    </section> : null}
    <div className="collection-controls"><V2Button disabled={busy} onClick={() => void action(async () => { await refresh(); })}>수집 상태 새로고침</V2Button>
      <label>저장된 수집 작업<select aria-label="저장된 수집 작업" value={active} disabled={busy} onChange={event => { setActive(event.target.value); setTicket(null); setConfirmed({}); }}>
        <option value="">작업 선택</option>{jobs.map(value => <option key={value.job_id} value={value.job_id}>{visibleQuery(value.query)} · {value.items.length}편 · {value.created_at}</option>)}
      </select></label></div>
    {!jobs.length && fresh ? <p>아직 저장된 수집 작업이 없습니다.</p> : null}
    {job ? <section aria-label="현재 수집 작업"><p>선택한 작업: {job.items.map(item => item.candidate.arxiv_id).join(", ")} · 검색 범위 arxiv.org · 수집 시 AI 전송 없음</p>
      {job.query_rejected || sensitiveQuery(job.query) ? <p role="alert">이 작업의 검색식에 연결 정보가 포함되어 연결할 수 없습니다. 논문 검색식으로 저장한 다른 작업을 선택하세요.</p> : null}
      <div className="collection-controls"><V2Button disabled={busy || !fresh || job.query_rejected || sensitiveQuery(job.query)} onClick={() => void action(connect)}>브라우저 연결 코드 발급</V2Button>
        <V2Button disabled={busy || !fresh} onClick={() => void action(async () => { await fetchJson(`${root}/${job.job_id}/revoke`, post({})); setTicket(null); await refresh(); })}>이 작업의 연결 권한 철회</V2Button></div>
      {ticket?.job === job.job_id ? <div><label htmlFor="browser-connection-ticket">확장 프로그램에 붙여넣을 연결 코드</label><textarea id="browser-connection-ticket" readOnly value={ticket.value} rows={3} spellCheck={false}/><p>이 작업에만 유효합니다. 만료: {ticket.expires}. 코드가 보이는 화면을 공유하지 마세요.</p>
        <V2Button disabled={busy} onClick={() => {
          const copy = navigator.clipboard?.writeText;
          if (!copy) { setNotice("자동 복사를 사용할 수 없습니다. 위 코드 전체를 직접 선택해 복사하세요."); return; }
          void navigator.clipboard.writeText(ticket.value).then(() => { if (mounted.current) setNotice("연결 코드를 복사했습니다. Whale의 XRML Paper Collector 팝업에 붙여넣어 연결하세요."); }, () => { if (mounted.current) setNotice("복사를 완료하지 못했습니다. 위 코드 전체를 직접 선택해 복사하세요."); });
        }}>연결 코드 복사</V2Button> <V2Button onClick={() => setTicket(null)}>코드 숨기기</V2Button>
        <ol><li>Whale 도구 모음에서 XRML Paper Collector 확장 팝업을 엽니다.</li><li>코드를 붙여넣고 연결한 뒤, 원하는 논문의 다운로드 버튼을 누릅니다.</li><li>다운로드 완료 후 이 화면에서 수집 상태를 새로고침하고 PDF를 확인해 Papers에 저장합니다.</li></ol>
      </div> : null}
      <ul className="collection-items">{job.items.map(item => <li key={item.item_id}><h4>{item.candidate.title}</h4>
        <p>{item.candidate.arxiv_id} · {labels[item.state]} · 시도 {item.attempts.length}회</p>
        <a href={safeSource(item.candidate.source_url)} target="_blank" rel="noreferrer">출처에서 서지정보 확인</a>
        {item.state === "auth_required" ? <p>브라우저에서 로그인·MFA·CAPTCHA를 직접 처리한 뒤 재개하세요. 작업과 이전 시도는 보존됩니다.</p> : null}
        {item.state === "downloading" ? <V2Button disabled={busy || !fresh} onClick={() => void action(() => control(item, "pause"))}>이 항목 접수 중단</V2Button> : null}
        {["auth_required", "interrupted", "failed"].includes(item.state) ? <V2Button disabled={busy || !fresh} onClick={() => void action(() => control(item, "resume"))}>이 항목 재개 준비</V2Button> : null}
        {item.pdf && item.state === "awaiting_confirmation" ? <div>
          <a href={`${getApiBase()}${root}/${job.job_id}/items/${item.item_id}/pdf`} target="_blank" rel="noreferrer">검사된 PDF 열기</a>
          <p>{item.pdf.bytes.toLocaleString("ko-KR")}바이트 · 정상 PDF 구조 확인. 서지정보와 내용의 일치는 직접 확인해야 합니다.</p>
          <label><input type="checkbox" checked={confirmed[item.item_id] === item.pdf.sha256} disabled={busy || !fresh} onChange={event => setConfirmed(old => ({ ...old, [item.item_id]: event.target.checked ? item.pdf!.sha256 : "" }))}/>PDF의 제목·저자·버전을 확인했고 이 논문에 연결하겠습니다.</label>
          <V2Button disabled={busy || !fresh || confirmed[item.item_id] !== item.pdf.sha256} onClick={() => void action(() => save(item))}>확인한 PDF를 Papers에 저장</V2Button>
        </div> : null}
        {item.saved ? <V2Button disabled={!onOpenPaper || busy} onClick={() => onOpenPaper?.(item.saved!.paper_version_id)}>저장된 논문 열기</V2Button> : null}
      </li>)}</ul>
    </section> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {!fresh && !busy ? <p>최신 상태를 확인하지 못했습니다. 수집 상태를 새로고침한 뒤 계속하세요.</p> : null}
  </div>;
}
export function BrowserCollection(props: Props) {
  const [opened, setOpened] = useState(false);
  return <details className="browser-collection-panel" onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>브라우저에서 논문 수집</summary>{opened ? <Workspace key={props.topicId} {...props}/> : null}</details>;
}
