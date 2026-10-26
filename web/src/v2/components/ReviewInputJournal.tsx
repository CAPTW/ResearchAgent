import { useEffect, useId, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";

type Component = { component_id: string; label: string; text: string; kind: "interpretation" | "question";
  section: "overview" | "limitations" | "questions"; eligible: boolean; support: "NOT_ASSESSED" | "NOT_ESTABLISHED";
  ineligible_reason: null | "BLANK_TEXT" | "TEXT_TOO_LONG" | "NO_SELECTED_CITED_EVIDENCE" | "CITATION_LIMIT_EXCEEDED" };
type Input = { receipt_id: string; paper_version_id: string; input_origin: string; model: string | null; validation_status: string; components: Component[] };
type Packet = { packet_id: string; inputs: Input[] };
type Command = { expected_packet_id: string; receipt_id: string; component_ids: string[] };
type Pending = { body: Command; input: Input };
type Props = { root: string; disabled?: boolean; onSaved: (candidateId: string) => void; onLocked?: (locked: boolean) => void };
const title = "저장된 분석으로 Journal 후보 만들기";
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const sectionLabels = { overview: "개요", limitations: "한계", questions: "후속 질문" };
const reasons = { BLANK_TEXT: "원 응답의 텍스트가 비어 있습니다.", TEXT_TOO_LONG: "후보 문장 길이 제한을 초과합니다. 원문을 자동으로 자르지 않습니다.",
  NO_SELECTED_CITED_EVIDENCE: "이 분석이 인용한 근거와 보고서에 선택한 근거의 공통 항목이 없습니다.",
  CITATION_LIMIT_EXCEEDED: "연결할 인용이 20개를 초과합니다. 근거를 자동으로 제외하지 않습니다." };
const origins: Record<string, string> = {
  PROVIDER_RESPONSE: "Provider 응답 보존 기록", FIXTURE_SYNTHETIC: "합성 시험 응답",
  LOCAL_APP_CAPTURE: "앱에 보존된 응답의 사본", FIXTURE_SYNTHETIC_CAPTURE: "앱에 보존된 합성 응답의 사본",
  MANUAL_IMPORT_UNVERIFIED: "수동 반입 · 실행 여부 미확인",
};

function readPacket(value: unknown): Packet | null {
  if (!object(value) || !text(value.packet_id) || !Array.isArray(value.inputs)) return null;
  const receipts = new Set<string>();
  for (const input of value.inputs) {
    if (!object(input) || !text(input.receipt_id) || receipts.has(input.receipt_id) || !text(input.paper_version_id)
      || !text(input.input_origin) || !(input.model === null || text(input.model)) || !text(input.validation_status) || !Array.isArray(input.components)) return null;
    receipts.add(input.receipt_id); const ids = new Set<string>();
    for (const item of input.components) {
      if (!object(item) || !text(item.component_id) || ids.has(item.component_id) || !text(item.label) || typeof item.text !== "string"
        || !["interpretation", "question"].includes(String(item.kind)) || !["overview", "limitations", "questions"].includes(String(item.section))
        || !["NOT_ASSESSED", "NOT_ESTABLISHED"].includes(String(item.support)) || typeof item.eligible !== "boolean"
        || (item.eligible ? item.ineligible_reason !== null || !item.text.trim() : !Object.hasOwn(reasons, String(item.ineligible_reason)))) return null;
      ids.add(item.component_id);
    }
  }
  return value as Packet;
}

function readSaved(value: unknown, pending: Pending): { candidateId: string; created: boolean } | null {
  if (!object(value) || typeof value.created !== "boolean" || !object(value.candidate)) return null;
  const candidate = value.candidate, input = pending.input;
  if (candidate.schema_version !== "REVIEW_INPUT_JOURNAL_CANDIDATE_V1" || candidate.origin !== "PRESERVED_REVIEW_DERIVATION"
    || !text(candidate.candidate_id) || !text(candidate.preserved_by) || typeof candidate.preserved_at !== "string" || !Number.isFinite(Date.parse(candidate.preserved_at))
    || !object(candidate.packet) || candidate.packet.packet_id !== pending.body.expected_packet_id || !object(candidate.source)
    || !Array.isArray(candidate.selected_component_ids) || JSON.stringify(candidate.selected_component_ids) !== JSON.stringify(pending.body.component_ids)
    || !object(candidate.proposal) || !text(candidate.proposal.title) || !Array.isArray(candidate.proposal.sections)
    || !Array.isArray(candidate.component_bindings) || candidate.component_bindings.length !== pending.body.component_ids.length
    || !Array.isArray(candidate.citations) || !candidate.citations.length || typeof candidate.raw_json !== "string" || !hash(candidate.raw_sha256)) return null;
  const source = candidate.source;
  if (!["receipt_id", "paper_version_id", "input_origin", "model", "validation_status"].every(key => source[key] === input[key as keyof Input])) return null;
  const citations = new Set<string>();
  for (const citation of candidate.citations) {
    if (!object(citation) || !text(citation.citation_id) || citations.has(citation.citation_id) || !text(citation.title)
      || typeof citation.text !== "string" || !text(citation.kind)) return null;
    citations.add(citation.citation_id);
  }
  const seen = new Set<string>(), sections = new Set<string>(); let entries = 0;
  for (const section of candidate.proposal.sections) {
    if (!object(section) || !text(section.kind) || sections.has(section.kind) || !Object.hasOwn(sectionLabels, section.kind) || !Array.isArray(section.entries) || !section.entries.length) return null;
    sections.add(section.kind);
    for (const [position, entry] of section.entries.entries()) {
      const matches = candidate.component_bindings.filter(binding => object(binding) && binding.section === section.kind && binding.position === position);
      if (matches.length !== 1 || !object(matches[0])) return null;
      const binding = matches[0], component = input.components.find(item => item.component_id === binding.component_id);
      if (!component || !component.eligible || !pending.body.component_ids.includes(component.component_id) || seen.has(component.component_id)
        || component.section !== section.kind || binding.label !== component.label || binding.support !== component.support || !object(entry)
        || entry.kind !== component.kind || entry.text !== component.text || !Array.isArray(entry.citation_ids) || !entry.citation_ids.length
        || !entry.citation_ids.every(id => typeof id === "string" && citations.has(id))) return null;
      seen.add(component.component_id); entries++;
    }
  }
  return entries === pending.body.component_ids.length ? { candidateId: candidate.candidate_id, created: value.created } : null;
}

function Editor({ root, disabled, onSaved, onLocked }: Props) {
  const id = useId();
  const [packet, setPacket] = useState<Packet | null>(null), [receiptId, setReceiptId] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]), [pending, setPending] = useState<Pending | null>(null);
  const [loading, setLoading] = useState(false), [saving, setSaving] = useState(false), [fresh, setFresh] = useState(false);
  const [error, setError] = useState(""), [message, setMessage] = useState("");
  const alive = useRef(true), locked = useRef(false), latest = useRef({ packet, receiptId, selectedIds });
  latest.current = { packet, receiptId, selectedIds };
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onLocked?.(saving || !!pending); }, [saving, pending, onLocked]);
  useEffect(() => () => onLocked?.(false), [onLocked]);
  async function reload() {
    if (locked.current || disabled || pending) return;
    locked.current = true; setLoading(true); setFresh(false); setError(""); setMessage("");
    try {
      const value = readPacket(await fetchJson<unknown>(`${root}/journal-review-inputs`));
      if (!value) throw new Error("invalid_saved_analysis_inputs");
      if (!alive.current) return;
      const previous = latest.current, nextInput = value.inputs.find(input => input.receipt_id === previous.receiptId);
      const oldInput = previous.packet?.inputs.find(input => input.receipt_id === previous.receiptId);
      if (previous.packet?.packet_id === value.packet_id && nextInput && oldInput && JSON.stringify(nextInput) === JSON.stringify(oldInput)) {
        setSelectedIds(previous.selectedIds);
      } else { setReceiptId(""); setSelectedIds([]); }
      setPacket(value); setFresh(true);
    } catch (cause) {
      if (alive.current) setError(cause instanceof ApiError && cause.status === 403
        ? "이 주제의 저장 분석을 읽을 권한이 없습니다. 현재 계정과 주제 접근 권한을 확인하세요."
        : "저장 분석 항목을 확인하지 못했습니다. 빈 목록으로 처리하지 않았습니다. 다시 확인하세요.");
    } finally { locked.current = false; if (alive.current) setLoading(false); }
  }
  useEffect(() => { void reload(); }, []);
  const input = packet?.inputs.find(item => item.receipt_id === receiptId);
  const selected = input?.components.filter(item => selectedIds.includes(item.component_id)) ?? [];
  const busy = !!disabled || loading || saving;
  const valid = fresh && !!packet && !!input && selected.length > 0 && selected.length <= 30 && selected.every(item => item.eligible);
  async function save() {
    if (locked.current || disabled || (!pending && !valid)) return;
    const attempt = pending ?? { input: input!, body: { expected_packet_id: packet!.packet_id, receipt_id: input!.receipt_id,
      component_ids: selected.map(item => item.component_id) } };
    locked.current = true; setPending(attempt); setSaving(true); setError(""); setMessage("");
    try {
      const response = await fetchJson<unknown>(`${root}/journal-review-input-candidates`, { method: "POST", body: JSON.stringify(attempt.body) });
      const result = readSaved(response, attempt);
      if (!result) throw new Error("saved_analysis_candidate_ack_mismatch");
      if (!alive.current) return;
      setPending(null); setMessage(result.created ? "선택한 원문을 미검토 Journal 후보로 보존했습니다. 문장·근거 검토와 채택은 별도 단계입니다."
        : "이미 보존된 같은 후보를 확인했습니다. 새 모델 호출이나 자동 채택은 수행하지 않았습니다.");
      onSaved(result.candidateId);
    } catch (cause) {
      if (!alive.current) return;
      const rejected = cause instanceof ApiError && cause.status >= 400 && cause.status < 500;
      if (rejected) { setPending(null); if (cause.status === 409) setFresh(false); }
      setError(rejected ? cause.status === 403 ? "이 주제에 후보를 저장할 권한이 없습니다. 선택 항목은 유지됩니다."
        : cause.status === 409 ? "고정 입력 또는 선택 항목이 현재 저장 조건과 다릅니다. 선택은 유지됩니다. 저장된 분석 항목을 다시 확인하세요."
        : "후보 저장 요청이 거부되었습니다. 선택 항목과 근거를 확인하세요. 입력은 유지됩니다."
        : "후보 저장 응답을 확인하지 못했습니다. 같은 요청을 보존했으며 자동 재시도하지 않습니다. 같은 후보 저장 다시 확인을 사용하세요.");
    } finally { locked.current = false; if (alive.current) setSaving(false); }
  }
  return <section aria-label={title} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
    <p>새 모델 호출·외부 전송 없이 이 작업에 고정된 저장 분석의 원문 항목을 후보로 구성합니다. 논문 전체 검토나 문장의 과학적 뒷받침을 검증한 결과가 아닙니다.</p>
    <p>분석 한 개와 최대 30개 항목을 직접 선택하세요. 후보 저장 후 문장·인용 검토와 명시적 채택을 거쳐 내보냅니다.</p>
    <button type="button" disabled={busy || !!pending} onClick={() => void reload()}>저장된 분석 항목 다시 확인</button>
    {loading ? <p role="status">저장 분석 항목 확인 중</p> : null}{saving ? <p role="status">Journal 후보 저장 중</p> : null}
    {error ? <p role="alert">{error}</p> : null}{message ? <p role="status">{message}</p> : null}
    {packet ? <>
      <p>고정 입력 묶음: {packet.packet_id}</p>
      {!fresh && !loading ? <p>이전에 확인한 입력을 유지하고 있습니다. 현재 저장 조건은 다시 확인해야 합니다.</p> : null}
      {!packet.inputs.length ? fresh ? <p>이 작업에 선택된 저장 분석이 없습니다. 논문의 저장 검토 기록을 보고서 입력으로 선택한 작업에서 사용하세요.</p>
        : <p>이전 목록에는 항목이 없었습니다. 현재 상태는 확인되지 않았습니다.</p> : <>
        <label htmlFor={`${id}-input`}>후보에 사용할 저장 분석</label>
        <select id={`${id}-input`} value={receiptId} disabled={busy || !!pending} onChange={event => { setReceiptId(event.target.value); setSelectedIds([]); setError(""); setMessage(""); }}>
          <option value="">저장 분석을 선택하세요</option>
          {packet.inputs.map(item => <option key={item.receipt_id} value={item.receipt_id}>{item.receipt_id} · {item.model ?? "모델 기록 없음"}</option>)}
        </select>
        {input ? <>
          <p>출처: {origins[input.input_origin] ?? `출처 코드 ${input.input_origin} (의미 미확인)`} · {input.input_origin}</p>
          <p>요청 모델(보존 기록): {input.model ?? "기록 없음"} · 당시 검증 상태: {input.validation_status}</p>
          <p>논문 버전: {input.paper_version_id} · 검토 기록: {input.receipt_id}</p>
          <p>출처·모델 식별자와 형식 검증은 실제 모델 신원, 전체 문서 검토 또는 과학적 품질의 증명이 아닙니다.</p>
          <fieldset disabled={busy || !!pending}><legend>Journal 후보에 포함할 분석 항목</legend>
            {input.components.map((item, index) => <div key={item.component_id}>
              <label htmlFor={`${id}-item-${index}`}><input id={`${id}-item-${index}`} type="checkbox" style={{ width: "auto", display: "inline-block", marginRight: 8 }}
                checked={selectedIds.includes(item.component_id)} disabled={busy || !!pending || !item.eligible || (selectedIds.length >= 30 && !selectedIds.includes(item.component_id))}
                onChange={event => { setMessage(""); setSelectedIds(ids => event.target.checked ? [...ids, item.component_id] : ids.filter(value => value !== item.component_id)); }}/>{item.label} · {item.component_id}</label>
              <p>{sectionLabels[item.section]} · {item.kind === "question" ? "질문" : "해석 문장"} · {item.support === "NOT_ESTABLISHED" ? "원 응답에서 근거 미확인으로 표시된 주장" : "문장 근거 미평가"}</p>
              {!item.eligible && item.ineligible_reason ? <p>선택 불가: {reasons[item.ineligible_reason]}</p> : null}
              <details><summary>{item.label} 원문 보기</summary><pre tabIndex={0} className="journal-preserved-text" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{item.text || "(원문이 비어 있음)"}</pre></details>
            </div>)}
          </fieldset><p>선택 {selectedIds.length} / 최대 30개 · 저장 순서는 원 분석의 항목 순서입니다.</p>
          {!input.components.length ? <p>이 분석에는 후보로 구성할 항목이 없습니다.</p> : null}
        </> : null}
        <button type="button" disabled={busy || !!pending || !valid} onClick={() => void save()}>선택한 항목을 Journal 후보로 저장</button>
      </>}
    </> : null}
    {pending ? <><p>응답이 확인될 때까지 고정 입력·분석·선택 항목을 유지합니다. 같은 저장 요청만 다시 확인할 수 있습니다.</p>
      <button type="button" disabled={busy} onClick={() => void save()}>같은 Journal 후보 저장 다시 확인</button></> : null}
  </section>;
}

function Scope(props: Props) {
  const [active, setActive] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open && !props.disabled) setActive(true); }}>
    <summary aria-disabled={props.disabled || undefined} onClick={event => { if (props.disabled) event.preventDefault(); }}>{title}</summary>
    {active ? <Editor {...props}/> : null}
  </details>;
}
export function ReviewInputJournal(props: Props) { return <Scope key={props.root} {...props}/>; }
