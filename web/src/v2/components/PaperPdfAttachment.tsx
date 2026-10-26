import { useEffect, useId, useRef, useState } from "react";
import { ApiError, fetchJson } from "../../lib/api";
import { V2Button } from "../V2Primitives";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";

type Props = { root: string; topicId: string; paperVersionId: string; disabled: boolean; onAttached: () => void };
type Target = { target_id: string; run_id: string };
type Attachment = { schema_version: string; attachment_id: string; topic_id: string; paper_version_id: string; target_id: string; run_id: string; paper_id: string;
  pdf_sha256: string; pdf_bytes: number; artifact_ref: string; created_at: string; created_by: string; binding: string; state: string };
type Inventory = { schema_version: string; topic_id: string; paper_version_id: string; targets: Target[]; attachments: Attachment[]; max_pdf_bytes: number };
type Command = { file: File; digest: string; target: Target };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && !!value.trim();
const hash = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const identity = (value: unknown, prefix: string) => typeof value === "string" && new RegExp(`^${prefix}_[a-f0-9]{64}$`).test(value);
const fail = () => { throw new Error("선택한 논문의 PDF 첨부 응답을 확인할 수 없습니다."); };
function readAttachment(value: unknown, scope: Props): Attachment {
  if (!object(value) || value.schema_version !== "PAPER_PDF_ATTACHMENT_V1" || value.topic_id !== scope.topicId || value.paper_version_id !== scope.paperVersionId
    || !identity(value.attachment_id, "pdfa") || !identity(value.target_id, "pdft") || !text(value.run_id) || !text(value.paper_id)
    || !hash(value.pdf_sha256) || !Number.isSafeInteger(value.pdf_bytes) || Number(value.pdf_bytes) < 1 || Number(value.pdf_bytes) > 25000000
    || value.artifact_ref !== `run:attachments/pdf/${value.pdf_sha256}.pdf` || !text(value.created_at) || !Number.isFinite(Date.parse(value.created_at))
    || !text(value.created_by) || value.binding !== "USER_SELECTED_NOT_IDENTITY_VERIFIED" || value.state !== "ATTACHED_NOT_EXTRACTED_OR_REVIEWED") return fail();
  return value as Attachment;
}
function readInventory(value: unknown, scope: Props): Inventory {
  if (!object(value) || value.schema_version !== "PAPER_PDF_ATTACHMENTS_V1" || value.topic_id !== scope.topicId || value.paper_version_id !== scope.paperVersionId
    || !Array.isArray(value.targets) || value.targets.some(t => !object(t) || !identity(t.target_id, "pdft") || !text(t.run_id))
    || !Array.isArray(value.attachments) || !Number.isSafeInteger(value.max_pdf_bytes) || Number(value.max_pdf_bytes) < 1 || Number(value.max_pdf_bytes) > 25000000) return fail();
  const attachments = value.attachments.map(item => readAttachment(item, scope)), targets = value.targets as Target[];
  // Historical bindings can remain readable after an occurrence ceases to be
  // an eligible new-upload target. Do not hide that authorized history.
  if (new Set(targets.map(t => t.target_id)).size !== targets.length || new Set(attachments.map(a => a.attachment_id)).size !== attachments.length) return fail();
  return { ...value, attachments } as Inventory;
}
function message(error: unknown): string {
  if (error instanceof ApiError) return ({ review_pdf_source_changed: "선택한 파일의 해시 또는 보존 원본이 다릅니다. 덮어쓰지 않았습니다.", review_pdf_source_too_large: "PDF 크기 한도를 넘었습니다.", review_pdf_encrypted: "암호로 잠긴 PDF는 반입할 수 없습니다.", review_pdf_engine_unavailable: "PDF를 확인할 로컬 추출 엔진(PyMuPDF)이 필요합니다.", review_pdf_page_limit_exceeded: "PDF 페이지 수 한도를 넘었습니다.", review_pdf_extraction_failed: "PDF를 읽을 수 없습니다. 손상 여부를 확인하세요.", review_pdf_source_invalid: "유효한 PDF 또는 안전한 저장 경로가 아닙니다.", review_preparation_not_found: "이 논문·출처에 접근할 수 없습니다. 주제와 권한을 확인하세요." } as Record<string, string>)[error.message] ?? "PDF 연결을 확인하지 못했습니다. 첨부 이력을 새로고침해 확인하세요.";
  return error instanceof Error ? error.message : "PDF 연결 응답을 확인하지 못했습니다.";
}
async function digestFile(file: File) {
  const bytes = await new Promise<ArrayBuffer>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.onerror = () => reject(new Error("선택한 파일을 읽을 수 없습니다.")); reader.readAsArrayBuffer(file); });
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), n => n.toString(16).padStart(2, "0")).join("");
}
function Workspace(props: Props) {
  const { preferences } = useOptionalAppearance(), id = useId(), alive = useRef(true), busyRef = useRef(false), fileInput = useRef<HTMLInputElement>(null);
  const [inventory, setInventory] = useState<Inventory | null>(null), [loading, setLoading] = useState(true), [revision, setRevision] = useState(0), [fresh, setFresh] = useState(false);
  const [file, setFile] = useState<File | null>(null), [target, setTarget] = useState(""), [confirmed, setConfirmed] = useState(false), [pending, setPending] = useState<Command | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [readError, setReadError] = useState(""), [notice, setNotice] = useState("");
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { let cancelled = false; setLoading(true); setFresh(false); setReadError("");
    void fetchJson(`${props.root}/pdf-attachments`).then(value => { const parsed = readInventory(value, props); if (!cancelled) { setInventory(parsed); setFresh(true); } }).catch(error => { if (!cancelled) setReadError(message(error)); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [props.root, props.topicId, props.paperVersionId, revision]);
  useEffect(() => { const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; }; if (file || pending) window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn); }, [file, pending]);
  const selected = inventory?.targets.find(item => item.target_id === target), locked = busy || pending !== null || props.disabled;
  function clear() { setFile(null); setConfirmed(false); setError(""); setNotice(""); if (fileInput.current) fileInput.current.value = ""; }
  async function save() {
    if (busyRef.current || props.disabled || (!pending && (!file || !selected || !confirmed || !fresh))) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const command = pending ?? { file: file!, digest: await digestFile(file!), target: selected! };
      if (!alive.current) return;
      setPending(command);
      const value = await fetchJson<unknown>(`${props.root}/pdf-attachments/${command.target.target_id}/${command.digest}`, { method: "PUT", headers: { "Content-Type": "application/pdf" }, body: command.file });
      if (!object(value) || typeof value.created !== "boolean") fail();
      const response = value as { attachment: unknown; created: boolean }, saved = readAttachment(response.attachment, props);
      if (saved.target_id !== command.target.target_id || saved.run_id !== command.target.run_id || saved.pdf_sha256 !== command.digest || saved.pdf_bytes !== command.file.size) fail();
      if (!alive.current) return;
      setPending(null); clear(); setNotice(response.created ? "PDF 복사본을 연결했습니다. 추출할 PDF를 직접 선택하세요." : "같은 PDF의 첨부 이력을 확인했습니다. 중복 저장하지 않았습니다."); setRevision(r => r + 1); props.onAttached();
    } catch (error) { if (alive.current) { setError(message(error)); if (error instanceof ApiError && [400, 401, 403, 404, 409, 413, 422].includes(error.status)) { setPending(null); setFresh(false); } } }
    finally { busyRef.current = false; if (alive.current) setBusy(false); }
  }
  return <section aria-label="논문 PDF 파일 연결">
    <p>현재 논문에 PDF 복사본을 연결합니다. 파일 선택만으로 전송하지 않습니다. 연결 시 현재 XRML 앱 서버에만 반입하며 외부 Provider에는 보내지 않습니다.</p>
    <p>연결은 사용자의 지정이며 논문 일치·과학적 근거를 자동 검증하지 않습니다. 기존 발췌문·분석은 유지되고, 추출·모델 실행·채택은 시작하지 않습니다.</p>
    <V2Button disabled={loading || busy || props.disabled} onClick={() => setRevision(r => r + 1)}>PDF 첨부 이력 새로고침</V2Button>
    {loading ? <p role="status">PDF 첨부 이력 확인 중</p> : readError ? <p role="alert">{readError}</p> : null}
    {inventory ? <>
      <p>최대 {inventory.max_pdf_bytes.toLocaleString("ko-KR")}바이트 · 암호화 PDF 미지원. 원래 파일명과 PC 경로는 저장하지 않습니다.</p>
      <label htmlFor={`${id}-target`}>PDF를 연결할 출처 관측</label><select id={`${id}-target`} value={target} disabled={locked || !fresh} onChange={event => { setTarget(event.target.value); setConfirmed(false); }}>
        <option value="">연결할 출처를 선택하세요</option>{inventory.targets.map((item, index) => <option key={item.target_id} value={item.target_id}>{item.run_id} · 관측 {index + 1}</option>)}</select>
      <label htmlFor={`${id}-file`}>연결할 PDF 파일</label><input ref={fileInput} id={`${id}-file`} type="file" accept=".pdf,application/pdf" disabled={locked || !fresh} onChange={event => { const selected = event.target.files?.[0]; if (!selected) return; setConfirmed(false); setNotice(""); if (!selected.size || selected.size > inventory.max_pdf_bytes) { clear(); setError("비어 있지 않은 크기 한도 이내의 PDF를 선택하세요."); return; } setError(""); setFile(selected); }}/>
      {file ? <><p>선택: {file.name} · {file.size.toLocaleString("ko-KR")}바이트</p><V2Button disabled={locked} onClick={clear}>PDF 파일 선택 취소</V2Button></> : null}
      <label className="pdf-attachment-confirmation"><input type="checkbox" checked={confirmed} disabled={locked || !file || !selected || !fresh} onChange={event => setConfirmed(event.target.checked)}/><span>이 파일을 현재 논문의 선택한 출처에 연결하겠습니다.</span></label>
      <V2Button disabled={locked || !file || !selected || !confirmed || !fresh} busy={busy} onClick={() => void save()}>선택한 PDF 복사본 연결</V2Button>
      <h4>보존된 PDF 첨부 이력</h4>{inventory.attachments.length ? <ul>{inventory.attachments.map(item => <li key={item.attachment_id}><p>{formatTimestamp(item.created_at, preferences.timeFormat, "ko-KR")} · {item.pdf_bytes.toLocaleString("ko-KR")}바이트 · 사용자 지정 연결</p><p>PDF SHA-256: {item.pdf_sha256}</p><p>첨부 ID: {item.attachment_id}</p></li>)}</ul> : fresh ? <p>사용자가 반입한 PDF가 없습니다.</p> : null}
    </> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {pending ? <p>저장 응답 확인 전입니다. 파일·출처를 유지합니다. 화면을 떠났다면 첨부 이력을 먼저 확인하세요. 같은 파일 재반입도 중복 이력을 만들지 않습니다.</p> : null}
    {pending && !busy ? <V2Button disabled={props.disabled} onClick={() => void save()}>같은 PDF 연결 요청 다시 확인</V2Button> : null}
  </section>;
}
export function PaperPdfAttachment(props: Props) {
  const [opened, setOpened] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>내 PDF 파일 연결</summary>{opened ? <Workspace key={JSON.stringify([props.root, props.topicId, props.paperVersionId])} {...props}/> : null}</details>;
}
