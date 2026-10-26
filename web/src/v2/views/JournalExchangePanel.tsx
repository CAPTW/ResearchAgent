import { saveExport } from "../../lib/saveExport";
import { useEffect, useId, useRef, useState } from "react";
import { fetchJson, fetchText } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { LocalReportReader } from "./LocalReportReader";
import { JournalReviewPanel } from "./JournalReviewPanel";
import { candidateTimestamp, candidateOriginLabel, type CandidateStamp, type CandidateSummary } from "../../lib/journalCandidates";
import { reportedJournalModel } from "../../lib/journalExecutions";
import { frozenCitationLabel, type JournalReviewInput } from "../../lib/localSynthesis";
import { JournalReviewInputContext } from "../components/ReviewInputHistory";
import { ReviewInputJournal } from "../components/ReviewInputJournal";

type Listing = { items: CandidateSummary[]; total: number; page: number; limit: number };
type Candidate = {
  candidate_id: string; raw_json: string; raw_sha256: string;
  proposal: { title: string; sections: { kind: string; entries: { kind: string; text: string; citation_ids: string[] }[] }[] };
  packet: { packet_id: string; references: { paper_version_id: string; title: string }[]; review_inputs?: JournalReviewInput[] }; markdown: string;
  citations: { citation_id: string; title: string; kind: string; text: string; paper_version_id: string; locator: unknown; source_identity: unknown; review_state: string; rights: unknown; review_receipt_id?: string; input_origin?: string }[];
  coverage: { selected_papers: number; cited_papers: number; abstract_citations: number; excerpt_citations: number; review_input_excerpt_citations?: number; uncited_paper_version_ids: string[] };
} & CandidateStamp & ({ origin?: "USER_IMPORTED_UNVERIFIED"; imported_by: string } | {
  origin: "RECORDED_ADAPTER_RESULT"; preserved_by: string;
  execution: { attempt: number; completed_at: string; result_digest: string; schema_version?: string;
    adapter_manifest?: { schema_version?: string; raw_journal_encoding?: string; raw_output_encoding?: string; raw_envelope_retained?: boolean };
    contract: { execution_id: string; actor_id: string; specification: { provider_name: string; model: string } };
    provider_reported: { provider_name: string; model: string; response_id: string | null } };
} | {
  origin: "PRESERVED_REVIEW_DERIVATION"; preserved_by: string;
  source: { paper_version_id: string; receipt_id: string; input_origin: string; model: string | null; validation_status: string;
    receipt_sha256: string; request_sha256: string | null; raw_text_sha256: string | null; selected_input_component_ids: string[] };
  selected_component_ids: string[];
  component_bindings: { section: string; position: number; component_id: string; label: string; support: "NOT_ASSESSED" | "NOT_ESTABLISHED" }[];
});
const MAX_BYTES = 512000;
const sections: Record<string, string> = { overview: "개요", synthesis: "문헌 간 해석", limitations: "한계", questions: "후속 질문" };
const kinds: Record<string, string> = { observation: "관찰 문장", interpretation: "해석 문장", question: "질문" };

function describe(error: unknown) {
  const text = String(error);
  if (text.includes("journal_packet_mismatch")) return "다른 고정 입력의 결과입니다. 이 입력에서 내려받은 묶음의 packet_id를 사용한 결과를 선택하세요.";
  if (text.includes("journal_citation_invalid")) return "고정 입력에 없는 인용이 있습니다. 결과의 citation_ids를 확인하세요.";
  if (text.includes("journal_exchange_invalid")) return "JSON 형식·항목·길이가 입력 묶음의 출력 규격에 맞지 않습니다. 중복 키와 알 수 없는 항목도 허용되지 않습니다.";
  if (text.includes("journal_sources_missing")) return "이 입력에는 가져온 글을 연결할 초록·발췌문이 없습니다.";
  return text;
}

export function SavedCandidate({ root, candidateId, initialReviewId, showReview, onReviewSaved }: { root: string; candidateId: string; initialReviewId?: string; showReview?: boolean; onReviewSaved?: () => void }) {
  const { preferences } = useOptionalAppearance();
  const [value, setValue] = useState<Candidate | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true), locked = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    void fetchJson<Candidate>(`${root}/${encodeURIComponent(candidateId)}`).then(
      data => { if (!cancelled) { setValue(data); setLoading(false); } }, e => { if (!cancelled) { setError(describe(e)); setLoading(false); } },
    );
    return () => { cancelled = true; };
  }, [root, candidateId, revision]);
  async function exportCandidate() {
    if (locked.current || !value) return;
    locked.current = true; setBusy(true); setError("");
    try {
      const raw = await fetchText(`${root}/${encodeURIComponent(candidateId)}`);
      const current = JSON.parse(raw) as Candidate;
      if (current.candidate_id !== candidateId) throw new Error("내려받은 후보 식별자가 선택한 후보와 다릅니다.");
      if (alive.current) await saveExport(raw, `${candidateId}.json`, "application/json");
    } catch (e) { if (alive.current) setError(describe(e)); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  const extracted = value?.origin === "RECORDED_ADAPTER_RESULT" && value.execution.schema_version === "JOURNAL_EXECUTION_RECEIPT_V2"
    && value.execution.adapter_manifest?.schema_version === "CLAUDE_JOURNAL_CLI_V1"
    && value.execution.adapter_manifest.raw_journal_encoding === "EXTRACTED_CANONICAL_JSON_V1"
    && value.execution.adapter_manifest.raw_envelope_retained === false;
  const antigravityResponse = value?.origin === "RECORDED_ADAPTER_RESULT" && value.execution.schema_version === "JOURNAL_EXECUTION_RECEIPT_V2"
    && value.execution.adapter_manifest?.schema_version === "ANTIGRAVITY_JOURNAL_CLI_V1"
    && value.execution.adapter_manifest.raw_journal_encoding === "RESULT_RESPONSE_JSON_UTF8_V1"
    && value.execution.adapter_manifest.raw_envelope_retained === false;
  const grokResponse = value?.origin === "RECORDED_ADAPTER_RESULT" && value.execution.schema_version === "JOURNAL_EXECUTION_RECEIPT_V2"
    && value.execution.adapter_manifest?.schema_version === "GROK_JOURNAL_ACP_V1"
    && value.execution.adapter_manifest.raw_journal_encoding === "ACP_AGENT_MESSAGE_TEXT_UTF8_V1"
    && value.execution.adapter_manifest.raw_envelope_retained === false;
  const anthropicResponse = value?.origin === "RECORDED_ADAPTER_RESULT" && value.execution.schema_version === "JOURNAL_EXECUTION_RECEIPT_V2"
    && value.execution.adapter_manifest?.schema_version === "ANTHROPIC_JOURNAL_WIRE_V1"
    && value.execution.adapter_manifest.raw_output_encoding === "CONTENT_TEXT_JSON_UTF8_V1"
    && value.execution.adapter_manifest.raw_envelope_retained === false;
  const codexV2Response = value?.origin === "RECORDED_ADAPTER_RESULT" && value.execution.schema_version === "JOURNAL_EXECUTION_RECEIPT_V2"
    && value.execution.adapter_manifest?.schema_version === "CODEX_JOURNAL_CLI_V2"
    && value.execution.adapter_manifest.raw_output_encoding === "LAST_MESSAGE_JSON_UTF8_V1"
    && value.execution.adapter_manifest.raw_envelope_retained === false;
  return <article aria-label={value?.origin === "PRESERVED_REVIEW_DERIVATION" ? "저장된 분석으로 구성한 저널 후보" : value?.origin === "RECORDED_ADAPTER_RESULT" ? "생성 결과 저널 후보" : "가져온 저널 후보"}>
    <button type="button" disabled={busy || loading} onClick={() => setRevision(v => v + 1)}>선택 후보 다시 확인</button>
    {error ? <p role="alert">후보 확인 오류: {error}</p> : null}
    {!value ? (!error ? <p role="status">저널 후보 확인 중</p> : null) : <>
      <h4><span className="journal-title-label">보존된 원고 제목</span>{" "}<span className="journal-preserved-text">{value.proposal.title}</span></h4>
      <p>후보를 보존한 당시의 제목 원문입니다. 제목에 적힌 상태와 현재 검토 상태는 다를 수 있습니다. 현재 상태는 사람의 근거 검토·저널 채택 이력에서 확인하세요.</p>
      {value.origin === "RECORDED_ADAPTER_RESULT" ? <>
        <p>저장된 생성 결과에서 보존 · 보존 당시 미검토 · 자동 의미 검증·독립적 과학 검증 미수행</p>
        <p>어댑터가 보고한 Provider·모델: {value.execution.provider_reported.provider_name} · {reportedJournalModel(value.execution.provider_reported.model)}</p>
        <p>실제 외부 모델 실행의 독립적인 증명이 아닙니다. 테스트 어댑터의 응답일 수도 있습니다.</p>
        {extracted ? <p>이 후보 JSON은 Claude 구조화된 결과에서 추출한 값입니다. CLI 표준 출력 원문을 보존한 파일이 아닙니다.</p> : null}
        {antigravityResponse ? <p>이 후보는 Antigravity 최종 결과 응답의 JSON을 받은 그대로 보존합니다. 전체 NDJSON 출력 로그를 보존한 파일이 아닙니다.</p> : null}
        {grokResponse ? <p>이 후보는 Grok ACP 응답 문장에서 추출한 JSON을 개행·문자 그대로 보존합니다. 전체 ACP 대화 로그를 보존한 파일이 아닙니다.</p> : null}
        {anthropicResponse ? <p>이 후보는 Anthropic 응답의 assistant content text JSON을 개행·문자 그대로 보존합니다. HTTP 응답 전체를 보존한 파일이 아닙니다.</p> : null}
        {codexV2Response ? <p>이 후보는 Codex CLI V2의 최종 응답 JSON을 개행·문자 그대로 보존합니다. 전체 CLI 출력 로그를 보존한 파일이 아닙니다.</p> : null}
        <details><summary>보존된 실행 출처</summary>
          <p>실행 기록: <span className="local-report-id">{value.execution.contract.execution_id}</span></p>
          <p>요청 Provider·모델: {value.execution.contract.specification.provider_name} · {value.execution.contract.specification.model}</p>
          <p>요청자: {value.execution.contract.actor_id} · 시도 {value.execution.attempt}</p>
          <p>응답 ID: {value.execution.provider_reported.response_id || "기록 없음"}</p>
          <p>결과 저장 시각: {formatTimestamp(value.execution.completed_at, preferences.timeFormat, "ko-KR")}</p>
          <p>실행 결과 SHA-256: <span className="local-report-id">{value.execution.result_digest}</span></p>
        </details>
      </> : value.origin === "PRESERVED_REVIEW_DERIVATION" ? <>
        <p>새 모델 호출 없이 저장된 분석 항목으로 구성 · 보존 당시 미검토 · 자동 의미 검증·독립적 과학 검증 미수행</p>
        <p>원 분석의 해석·질문을 그대로 보존했습니다. 전체 논문 검토나 문장별 과학적 뒷받침을 확인한 것이 아니며, 자동 채택되지 않습니다.</p>
        <details><summary>후보로 구성한 저장 분석의 출처</summary>
          <p>원 검토 기록: {value.source.receipt_id} · 논문 버전: {value.source.paper_version_id}</p>
          <p>원 분석 출처: {value.source.input_origin} · 보존된 분석 상태: {value.source.validation_status}</p>
          <p>요청 모델(보존 기록): {value.source.model ?? "기록 없음"} · 모델 신원·실행 품질의 독립 검증이 아닙니다.</p>
          <p>검토 기록 SHA-256: {value.source.receipt_sha256}</p>
          <p>원 요청 SHA-256: {value.source.request_sha256 ?? "기록 없음"}</p><p>원 응답 SHA-256: {value.source.raw_text_sha256 ?? "기록 없음"}</p>
          <p>선택한 분석 항목: {value.selected_component_ids.join(", ")}</p>
          <p>선택한 원 입력 구간: {value.source.selected_input_component_ids.join(", ")}</p>
        </details>
      </> : <p>가져올 당시 미검토 · 생성 도구·모델 및 실행 여부 미확인 · 자동 의미 검증·독립적 과학 검증 미수행</p>}
      <JournalReviewPanel root={`${root}/${encodeURIComponent(candidateId)}/reviews`} citations={value.citations} enabled={!loading && !error} initialReviewId={initialReviewId} initiallyOpen={showReview} onSaved={onReviewSaved}/>
      <p>{value.origin === "RECORDED_ADAPTER_RESULT" || value.origin === "PRESERVED_REVIEW_DERIVATION" ? `후보로 보존한 사용자: ${value.preserved_by}` : `가져온 사용자: ${value.imported_by}`} · {formatTimestamp(candidateTimestamp(value), preferences.timeFormat, "ko-KR")}</p>
      <p>인용한 논문 {value.coverage.cited_papers} / 선택 논문 {value.coverage.selected_papers} · 초록 {value.coverage.abstract_citations}개 · 리뷰 발췌 {value.coverage.excerpt_citations}개</p>
      <p>인용 수는 문장의 타당성이나 전체 문헌 검토율을 뜻하지 않습니다.</p>
      {typeof value.coverage.review_input_excerpt_citations === "number" ? <p>선택한 검토 입력 발췌 인용: {value.coverage.review_input_excerpt_citations}개</p> : null}
      {value.packet.review_inputs?.length ? <details><summary>선택한 과거 검토 문맥 {value.packet.review_inputs.length}개</summary>{value.packet.review_inputs.map(input=><JournalReviewInputContext key={`${input.paper_version_id}:${input.receipt_id}`} value={input}/>)}</details> : null}
      {value.coverage.uncited_paper_version_ids.length ? <details><summary>인용하지 않은 선택 논문 {value.coverage.uncited_paper_version_ids.length}개</summary><ul>{value.coverage.uncited_paper_version_ids.map(id => <li key={id}>{value.packet.references.find(ref => ref.paper_version_id === id)?.title || "인용할 텍스트 없음"}<small className="local-report-id">{id}</small></li>)}</ul></details> : null}
      {value.proposal.sections.map(section => <section key={section.kind} aria-label={sections[section.kind]}>
        <h5>{sections[section.kind]}</h5>
        {section.entries.map((entry, index) => {
          const component = value.origin === "PRESERVED_REVIEW_DERIVATION" ? value.component_bindings.find(binding => binding.section === section.kind && binding.position === index) : undefined;
          return <div className="journal-imported-entry" key={index}>
          {component ? <p>원래 분석 항목: {component.label} · {component.component_id} · {component.support === "NOT_ESTABLISHED" ? "원 응답에서 근거 미확인으로 표시된 주장" : "문장 근거 미평가"}</p> : null}
          <p>{kinds[entry.kind]} · {index + 1}</p><p className="journal-preserved-text">{entry.text}</p>
          {entry.citation_ids.map(id => {
            const citation = value.citations.find(ref => ref.citation_id === id)!;
            return <details key={id}><summary>인용 [{value.citations.indexOf(citation) + 1}] · {citation.title} · {frozenCitationLabel(citation.kind)}</summary>
              <blockquote className="journal-preserved-text">{citation.text}</blockquote>
              <p>보존 위치: {citation.locator ? JSON.stringify(citation.locator) : "기록 없음"}</p><p>출처: {JSON.stringify(citation.source_identity)}</p>
              <p>당시 검토: {citation.review_state} · 권리: {JSON.stringify(citation.rights)}</p><p>논문 버전: {citation.paper_version_id}</p><p>근거: {citation.citation_id}</p>
              {citation.review_receipt_id ? <p>선택한 검토 기록: {citation.review_receipt_id} · 입력 출처: {citation.input_origin || "미확인"}</p> : null}
            </details>;
          })}
        </div>; })}
      </section>)}
      <details><summary>인용 번호가 포함된 후보 Markdown 보기</summary><LocalReportReader content={value.markdown}/></details>
      <details><summary>보존된 후보 JSON과 식별자</summary><p>후보: {value.candidate_id}</p><p>입력 묶음: {value.packet.packet_id}</p><p>JSON UTF-8 SHA-256: {value.raw_sha256}</p><pre>{value.raw_json}</pre></details>
      <button type="button" disabled={busy || loading || !!error} onClick={() => void exportCandidate()}>원본 후보·근거 JSON 내려받기</button>
    </>}
  </article>;
}

function Workspace({ root }: { root: string }) {
  const id = useId();
  const { preferences } = useOptionalAppearance();
  const [listing, setListing] = useState<Listing | null>(null);
  const [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [listError, setListError] = useState(""), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [deriving, setDeriving] = useState(false);
  const [pending, setPending] = useState<{ name: string; raw: string } | null>(null);
  const [selected, setSelected] = useState("");
  const [packet, setPacket] = useState<({ packet_id: string; review_inputs?: JournalReviewInput[] } & Record<string, unknown>) | null>(null);
  const alive = useRef(true), locked = useRef(false), fileRead = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; fileRead.current++; }; }, []);
  useEffect(() => {
    let cancelled = false; setLoading(true); setListError("");
    void fetchJson<Listing>(`${root}/journal-candidates?page=${page}&limit=10`).then(
      data => { if (!cancelled) { setListing(data); setLoading(false); } },
      e => { if (!cancelled) { setListError(describe(e)); setLoading(false); } },
    );
    return () => { cancelled = true; };
  }, [root, page, revision]);
  async function choose(file?: File) {
    if (deriving) return;
    const generation = ++fileRead.current;
    setPending(null); setError(""); setMessage("");
    if (!file) return;
    try {
      if (file.size > MAX_BYTES) throw new Error("결과 파일은 UTF-8 기준 512,000바이트 이하여야 합니다.");
      const raw = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer());
      JSON.parse(raw.replace(/^\uFEFF/, ""));
      if (alive.current && generation === fileRead.current) setPending({ name: file.name, raw });
    } catch { if (alive.current && generation === fileRead.current) setError("파일을 읽지 못했습니다. 512,000바이트 이하의 UTF-8 JSON 파일을 선택하세요."); }
  }
  async function run(action: "packet" | "preview" | "import") {
    if (locked.current || deriving || (action === "import" && !pending)) return;
    locked.current = true; setBusy(true); setError(""); setMessage("");
    try {
      if (action !== "import") {
        const raw = action === "packet" ? await fetchText(`${root}/journal-exchange`) : null;
        const value = raw !== null ? JSON.parse(raw) as { packet_id: string; review_inputs?: JournalReviewInput[] } & Record<string, unknown>
          : await fetchJson<{ packet_id: string; review_inputs?: JournalReviewInput[] } & Record<string, unknown>>(`${root}/journal-exchange`);
        if (alive.current) { setPacket(value); if(raw !== null)await saveExport(raw, `${value.packet_id}.json`, "application/json"); }
      } else {
        const result = await fetchJson<{ candidate: Candidate; created: boolean }>(`${root}/journal-candidates`, { method: "POST", body: JSON.stringify({ raw_json: pending!.raw }) });
        if (alive.current) {
          setSelected(result.candidate.candidate_id); setPage(1); setRevision(v => v + 1);
          setMessage(result.created ? "가져온 글을 미검토 후보로 보존했습니다." : "이미 보존된 같은 원본입니다. 기존 후보를 열었습니다.");
        }
      }
    } catch (e) { if (alive.current) setError(describe(e)); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function openSavedCandidate(candidateId: string) {
    if (!alive.current) return;
    setSelected(candidateId); setPage(1); setRevision(value => value + 1); setMessage("");
  }
  return <section aria-label="저널 결과 교환">
    <ReviewInputJournal root={root} disabled={busy} onLocked={setDeriving} onSaved={openSavedCandidate}/>
    <p>입력 JSON에는 이 작업에 고정된 초록·리뷰 발췌, 선택한 검토 입력과 별도로 구분한 과거 검토 결과, 출처와 출력 규격이 들어 있습니다. 확인·내려받기는 Provider 실행·외부 전송을 수행하지 않습니다.</p>
    <button type="button" disabled={busy || deriving} onClick={() => void run("preview")}>저널 고정 입력 확인</button>
    <button type="button" disabled={busy || deriving} onClick={() => void run("packet")}>저널 고정 입력 JSON 내려받기</button>
    {packet ? <section aria-label="보존된 저널 입력 미리보기"><p>입력 묶음: {packet.packet_id}</p>{packet.review_inputs?.map(input=><JournalReviewInputContext key={`${input.paper_version_id}:${input.receipt_id}`} value={input}/>)}<details><summary>저널 고정 입력 원문(JSON)</summary><pre className="journal-preserved-text">{JSON.stringify(packet,null,2)}</pre></details></section> : null}
    <p>준비된 결과 JSON을 선택한 뒤 가져오세요. 고정 입력과 인용 식별자를 검사해 별도 후보로 보존하며, 사용자 작성 초안이나 채택된 보고서를 변경하지 않습니다.</p>
    <label htmlFor={id}>저널 결과 JSON 파일</label><input id={id} type="file" accept=".json,application/json" disabled={busy || deriving} onChange={e => void choose(e.target.files?.[0])}/>
    {pending ? <><p>선택한 파일: {pending.name}</p><details><summary>저장 전 결과 원문 확인</summary><pre>{pending.raw}</pre></details></> : null}
    <button type="button" disabled={busy || deriving || !pending} onClick={() => void run("import")}>{busy ? "처리 중" : "미검토 저널 후보 가져오기"}</button>
    {error ? <p role="alert">가져오기·내려받기 오류: {error}{pending ? " 선택한 파일은 유지됩니다." : ""}</p> : null}
    {message ? <p role="status">{message}</p> : null}
    <button type="button" disabled={loading} onClick={() => setRevision(v => v + 1)}>보존 후보 이력 새로고침</button>
    {listError ? <p role="alert">후보 이력 오류: {listError}. 이전 목록은 현재 상태로 확인되지 않았습니다.</p> : loading ? <p role="status">보존 후보 이력 확인 중</p> : null}
    {listing ? <><h4>보존된 저널 후보 · {listing.total}개</h4>
      <ul>{listing.items.map(item => <li key={item.candidate_id}><button type="button" disabled={deriving || loading || !!listError} aria-pressed={selected === item.candidate_id} onClick={() => setSelected(item.candidate_id)}>{item.title} · {candidateOriginLabel(item)} · {formatTimestamp(candidateTimestamp(item), preferences.timeFormat, "ko-KR")}<small className="local-report-id">{item.candidate_id}</small></button></li>)}</ul>
      <button type="button" disabled={deriving || loading || !!listError || page === 1} onClick={() => setPage(v => v - 1)}>이전 후보 페이지</button><span> {page} / {Math.max(1, Math.ceil(listing.total / listing.limit))} </span><button type="button" disabled={deriving || loading || !!listError || page * listing.limit >= listing.total} onClick={() => setPage(v => v + 1)}>다음 후보 페이지</button>
    </> : null}
    {selected ? <SavedCandidate key={selected} root={`${root}/journal-candidates`} candidateId={selected}/> : null}
  </section>;
}

export function JournalExchangePanel({ topicId, jobId }: { topicId: string; jobId: string }) {
  const [activated, setActivated] = useState(false);
  const root = `/api/topics/${encodeURIComponent(topicId)}/local-synthesis/${encodeURIComponent(jobId)}`;
  return <><p>저장된 분석에서 후보를 만들거나 결과 파일을 가져온 뒤 근거를 검토합니다. 저장된 분석으로 구성할 때는 별도 파일이 필요하지 않습니다.</p>
    <details className="local-journal journal-exchange" onToggle={e => { if (e.currentTarget.open) setActivated(true); }}><summary>저널 입력·결과 파일 교환</summary>{activated ? <Workspace key={root} root={root}/> : null}</details></>;
}
