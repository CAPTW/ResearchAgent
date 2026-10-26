import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { getDocumentPlan, getPlanConfiguration, listDocumentPlans, prepareDocumentUnit, saveDocumentPlan,
  type DocumentPlan, type DocumentPlanRequest, type PlanConfiguration } from "../../lib/reviewDocumentPlan";
import type { PreparationSource } from "../../lib/reviewPreparation";
import { V2Button } from "../V2Primitives";
import { formatTimestamp } from "../appearance";
import { useOptionalAppearance } from "../AppearanceProvider";

type Props = { root: string; source?: PreparationSource; disabled?: boolean; onBusyChange: (value: boolean) => void; onOpenPreparation: (id: string) => void; onOpenReceipt?: (id: string) => void };
const number = (text: string) => /^\d+$/.test(text) && Number.isSafeInteger(Number(text)) ? Number(text) : NaN;
const message = (value: unknown) => value instanceof Error ? value.message : String(value);

function Workspace({root, source, disabled, onBusyChange, onOpenPreparation, onOpenReceipt}: Props) {
  const id = useId(), alive = useRef(true), lock = useRef(false);
  const { preferences } = useOptionalAppearance();
  const [config, setConfig] = useState<PlanConfiguration | null>(null), [connector, setConnector] = useState("");
  const [model, setModel] = useState(""), [context, setContext] = useState(""), [input, setInput] = useState("");
  const [output, setOutput] = useState("2200"), [safety, setSafety] = useState("8192");
  const [error, setError] = useState(""), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<DocumentPlanRequest | null>(null), [pendingUnit, setPendingUnit] = useState<{plan: string; index: number} | null>(null);
  const [plans, setPlans] = useState<DocumentPlan[]>([]), [hasMore, setHasMore] = useState(false), [page, setPage] = useState(1);
  const [plan, setPlan] = useState<DocumentPlan | null>(null), [revision, setRevision] = useState(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onBusyChange(busy || pending !== null || pendingUnit !== null); }, [busy, pending, pendingUnit, onBusyChange]);
  function choose(value: string, configuration = config) {
    const item = configuration?.choices.find(c => c.connector_id === value);
    setConnector(value); setModel(item?.model ?? ""); setContext(item?.context_tokens ? String(item.context_tokens) : "");
    setInput(item?.context_tokens ? String(Math.max(0, item.context_tokens - 2200 - 8192)) : ""); setOutput("2200"); setSafety("8192");
    setNotice("");
  }
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    void Promise.all([config ? Promise.resolve(config) : getPlanConfiguration(root), listDocumentPlans(root, page)]).then(([settings, history]) => {
      if (cancelled) return;
      if (!config) { setConfig(settings); choose(settings.default_connector_id, settings); }
      setPlans(history.items); setHasMore(history.hasMore); setLoading(false);
    }, reason => { if (!cancelled) { setError(message(reason)); setLoading(false); } });
    return () => { cancelled = true; };
    // History refresh must not apply changed Settings to the next draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root, page, revision]);
  const storedChoice = config?.choices.find(choice => choice.connector_id === connector);
  const valid = source && storedChoice && model.trim() && number(context) >= 8192 && number(input) >= 4096
    && number(output) >= 256 && number(output) <= 8000 && number(safety) >= 1024
    && number(input) + number(output) + number(safety) <= number(context);
  const locked = busy || pending !== null || pendingUnit !== null || disabled;
  async function refreshSettings() {
    if (lock.current || locked || loading) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const settings = await getPlanConfiguration(root);
      if (alive.current) {
        setConfig(settings);
        setNotice("저장된 모델 설정을 확인했습니다. 현재 입력은 유지했습니다. ‘선택 연결의 저장 모델 적용’으로 다음 계획에 반영하세요.");
      }
    } catch (reason) { if (alive.current) setError(message(reason)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  function applyStoredModel() {
    if (lock.current || locked || loading || !storedChoice) return;
    choose(connector);
    setNotice("선택한 연결의 저장 모델과 기본 예산을 다음 계획 입력에 적용했습니다. 과거 계획·실행은 바뀌지 않았습니다.");
  }
  async function save() {
    if (lock.current || (!pending && (!valid || disabled))) return;
    const body = pending ?? { request_id: crypto.randomUUID(), run_id: source!.run_id, paper_id: source!.paper_id,
      expected_source_digest: source!.source_digest, connector_id: connector, model: model.trim(), context_tokens: number(context),
      input_tokens: number(input), max_output_tokens: number(output), safety_tokens: number(safety) };
    lock.current = true; setPending(body); setBusy(true); setError("");
    try { const saved = await saveDocumentPlan(root, body); if (alive.current) { setPlan(saved); setPending(null); setRevision(r => r + 1); } }
    catch (reason) { if (alive.current) { setError(message(reason)); if (reason instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(reason.status)) setPending(null); } }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  async function open(identity: string) {
    if (lock.current) return; lock.current = true; setBusy(true); setError("");
    try { const saved = await getDocumentPlan(root, identity); if (alive.current) setPlan(saved); }
    catch (reason) { if (alive.current) setError(message(reason)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  async function prepare(index: number) {
    if (lock.current || (!pendingUnit && (!plan || disabled))) return;
    const target = pendingUnit ?? {plan: plan!.plan_id, index};
    lock.current = true; setBusy(true); setPendingUnit(target); setError("");
    try {
      if (!plan || target.plan !== plan.plan_id) throw new Error("document_plan_unit_response_mismatch");
      const saved = await prepareDocumentUnit(root, plan, target.index);
      if (alive.current) { setPendingUnit(null); onOpenPreparation(saved.preparation_id); }
      const refreshed = await getDocumentPlan(root, target.plan);
      if (alive.current) setPlan(refreshed);
    } catch (reason) { if (alive.current) { setError(message(reason)); if (reason instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(reason.status)) setPendingUnit(null); } }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  return <section aria-label="전문 분석 계획" className="review-preparation-record">
    <p>선택한 저장 본문 전체를 모델별 예산 안에 배치합니다. 들어가면 전문 1개, 넘으면 절·문장 경계로 나눕니다. 준비만 저장하며 모델 호출·키 읽기·외부 전송은 하지 않습니다.</p>
    <p>토큰은 로컬 UTF-8 바이트 기반 보수적 추정치입니다. 지시문·JSON 스키마·문맥을 포함하며 실제 토크나이저의 정확한 수치나 비용 상한이 아닙니다.</p>
    <p><a href="/app/settings">설정에서 모델·API 키 관리</a> → 구간 준비 → Native V2 전송 내용 확인 → 해당 요청의 1회 승인 → 실행. 저장된 결과 조회는 새 호출을 하지 않습니다.</p>
    {loading ? <p role="status">모델 예산·계획 이력 확인 중</p> : null}
    {config ? <fieldset disabled={locked || loading}><legend>이번 분석에 사용할 AI</legend>
      <p>아래 선택은 새로 저장할 계획에만 적용됩니다. 이미 저장한 계획과 진행 중 실행의 모델은 유지됩니다.</p>
      <label htmlFor={`${id}-connector`}>분석 연결<select id={`${id}-connector`} value={connector} onChange={e => choose(e.target.value)}>{!storedChoice ? <option value={connector} disabled>{connector || "연결 미선택"} · 설정 목록에 없음</option> : null}{config.choices.map(c => <option key={c.connector_id} value={c.connector_id}>{c.display_name}</option>)}</select></label>
      {!storedChoice ? <p role="status">선택한 연결이 최근 확인한 설정 목록에 없습니다. 다른 연결을 선택하세요. 현재 모델 입력은 유지됩니다.</p> : null}
      <p>최근 확인한 저장 모델: {storedChoice?.model || "미선택"}</p>
      <p>저장 모델을 적용하면 현재 입력한 모델과 컨텍스트·입력·출력·추정 오차 여유 예산을 해당 모델의 기본값으로 바꿉니다. 저장한 계획은 유지됩니다.</p>
      <V2Button onClick={() => void refreshSettings()}>저장된 모델 설정 확인</V2Button>
      <V2Button disabled={!storedChoice} onClick={applyStoredModel}>선택 연결의 저장 모델 적용</V2Button>
      <label htmlFor={`${id}-model`}>계획 모델 ID<input id={`${id}-model`} value={model} onChange={e => { setModel(e.target.value); setContext(""); setInput(""); setNotice(""); }}/></label>
      <p>문서화된 모델은 컨텍스트 값을 채웁니다. 알 수 없는 ID·별칭은 해당 모델의 공식 사양을 확인해 입력하세요. 모델을 바꿔도 과거 계획·결과는 바뀌지 않습니다.</p>
      <div className="review-preparation-range">{([
        ["context", "모델 컨텍스트 토큰", context, setContext], ["input", "요청당 입력 토큰 예산", input, setInput],
        ["output", "출력 토큰 예약", output, setOutput], ["safety", "추정 오차 여유 토큰", safety, setSafety],
      ] as const).map(([key, label, value, change]) => <label key={key} htmlFor={`${id}-${key}`}>{label}<input id={`${id}-${key}`} type="number" step="1" min="0" value={value} onChange={e => change(e.target.value)}/></label>)}</div>
      <p>입력 + 출력 + 여유 ≤ 모델 컨텍스트. 현재 구조화 검토의 출력 예약은 256–8,000 토큰입니다.</p>
      <p>{source ? `계획 대상: ${source.source_chars.toLocaleString("ko-KR")}자 · ${source.paper_id}` : "위에서 사용할 저장 본문 출처를 선택하세요. 저장된 계획은 아래에서 열 수 있습니다."}</p>
      <V2Button disabled={!valid} onClick={() => void save()}>전문 분석 계획 저장</V2Button>
    </fieldset> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {error ? <p role="alert">전문 계획 오류: {error}. 입력과 이미 저장된 계획은 유지됩니다.</p> : null}
    {pending && !busy ? <><p>저장 응답이 미확정입니다. 같은 요청으로 확인하며 다시 모델을 실행하지 않습니다.</p><V2Button onClick={() => void save()}>같은 전문 계획 요청 확인</V2Button></> : null}
    {pendingUnit && !busy ? <V2Button onClick={() => void prepare(pendingUnit.index)}>같은 분석 구간 준비 확인</V2Button> : null}
    <V2Button disabled={locked || loading} onClick={() => setRevision(r => r + 1)}>전문 계획 이력 새로고침</V2Button>
    <ul>{plans.map(item => <li key={item.plan_id}><V2Button disabled={locked} onClick={() => void open(item.plan_id)}>{formatTimestamp(item.created_at, preferences.timeFormat, "ko-KR")} · {item.budget.model} · {item.units.length}개 분석 구간</V2Button></li>)}</ul>
    <nav aria-label="전문 계획 페이지"><V2Button disabled={locked || loading || page === 1} onClick={() => setPage(p => p - 1)}>이전 전문 계획</V2Button><span>{page} 페이지</span><V2Button disabled={locked || loading || !hasMore} onClick={() => setPage(p => p + 1)}>다음 전문 계획</V2Button></nav>
    {plan ? <section aria-label="저장된 전문 분석 계획">
      <h5>{plan.mode === "WHOLE_EXTRACTED_TEXT" ? "전체 추출 본문 1개 요청" : `절별 분석 ${plan.units.length}개 요청`}</h5>
      <p>{plan.budget.provider_name} · {plan.budget.model} · 요청당 입력 예산 {plan.budget.input_tokens.toLocaleString("ko-KR")} 토큰</p>
      <p>절 제목은 정규화된 본문에서 찾은 휴리스틱입니다. 제목을 못 찾거나 한 절이 너무 길면 문장·단어 경계로 나눕니다. 각 구간은 같은 목차와 앞뒤 최대 256자 문맥을 연결하며, 주변 문맥은 그 구간의 분석 완료 범위에 더하지 않습니다.</p>
      <p>대상은 확보·추출된 텍스트입니다. PDF의 이미지·표·누락 페이지까지 읽었다는 뜻이 아닙니다. 과학적 뒷받침·전체 논문 검토·사람의 채택은 별도입니다.</p>
      <V2Button disabled={locked} onClick={() => void open(plan.plan_id)}>이 계획 결과 다시 조회</V2Button>
      <ol>{plan.units.map(unit => <li key={unit.index}>
        <p>{unit.label} · {unit.start}–{unit.end}자 · 입력 추정 {unit.estimated_input_tokens.toLocaleString("ko-KR")} 토큰</p>
        <p>{unit.receipts.length ? `저장된 입력·결과 ${unit.receipts.length}건` : unit.preparation_id ? "준비됨 · 저장된 결과 없음 (미시도라는 뜻은 아님)" : "계획됨 · 아직 실행 요청 아님"}</p>
        {unit.preparation_id ? <V2Button disabled={locked} onClick={() => onOpenPreparation(unit.preparation_id!)}>구간 {unit.index + 1} 입력·실행 상태 열기</V2Button>
          : <V2Button disabled={locked} onClick={() => void prepare(unit.index)}>구간 {unit.index + 1} 분석 요청 준비</V2Button>}
        {unit.receipts.map(receipt => <p key={receipt.receipt_id}>{receipt.input_origin === "FIXTURE_SYNTHETIC" ? "합성 시험" : receipt.input_origin === "PROVIDER_RESPONSE" && receipt.validated_response ? "검증된 구조화 응답" : "미검증 반입"} · {receipt.status} {onOpenReceipt ? <V2Button disabled={locked} onClick={() => onOpenReceipt(receipt.receipt_id)}>구간 {unit.index + 1} 근거·결과 열기</V2Button> : null}</p>)}
      </li>)}</ol>
      <details><summary>계획 식별자</summary><p>{plan.plan_id}</p><p>{plan.plan_sha256}</p><p>{plan.budget_source}</p></details>
    </section> : null}
  </section>;
}

export function ReviewDocumentPlan(props: Props) {
  const [opened, setOpened] = useState(false);
  return <details onToggle={event => { if (event.currentTarget.open) setOpened(true); }}><summary>모델 예산으로 전문·절별 분석 준비</summary>{opened ? <Workspace {...props}/> : null}</details>;
}
