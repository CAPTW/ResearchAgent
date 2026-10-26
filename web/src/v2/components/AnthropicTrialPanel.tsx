import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { actOnAnthropicTrial, advancesTrial, getAnthropicCapture, getAnthropicTrial, sameTrialInput, trialTerminal, trialTraceLabel, trialDiagnosticLabels, trialOutputReasonLabels,
  type AnthropicTrial, type TrialAction, type TrialEnvelope, type TrialState } from "../../lib/anthropicTrial";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import "./AnthropicTrialPanel.css";

type Pending = { action: "execute" | "cancel"; body: TrialAction; frozen: AnthropicTrial };
const labels: Record<TrialState, string> = { PREPARED: "준비됨 · 아직 실행하지 않음", CLAIMED: "시도 시작됨 · 결과 대기",
  CAPTURED: "응답 저장됨 · 아직 구조 검증하지 않음", STRUCTURE_VALID: "구조 검증 통과 · 과학적 타당성 미평가",
  STRUCTURE_INVALID: "응답 구조 검증 실패", INCOMPLETE: "출력 미완료 · 성공으로 처리하지 않음", REFUSED: "Provider가 요청을 거절함",
  UNKNOWN: "실행 결과 불명 · 자동 재실행하지 않음", CANCELLED: "취소됨 · 새 실행 없음", EXPIRED: "실행 허용 시간 만료" };

export function AnthropicTrialPanel() {
  const { preferences } = useOptionalAppearance();
  const [shown, setShown] = useState<TrialEnvelope | null>(null), [pending, setPendingState] = useState<Pending | null>(null);
  const [loading, setLoading] = useState(true), [executing, setExecuting] = useState(false), [cancelling, setCancelling] = useState(false);
  const [mustRefresh, setMustRefresh] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [raw, setRaw] = useState<string | null>(null), [rawLoading, setRawLoading] = useState(false), [rawError, setRawError] = useState("");
  const alive = useRef(false), epoch = useRef(0), reads = useRef(0), rawReads = useRef(0);
  const current = useRef<AnthropicTrial | null>(null), pendingRef = useRef<Pending | null>(null);
  const executionLock = useRef(false), cancelLock = useRef(false), reader = useRef<AbortController | null>(null), rawReader = useRef<AbortController | null>(null);
  const setPending = (value: Pending | null) => { pendingRef.current = value; setPendingState(value); };
  const active = (cycle: number) => alive.current && epoch.current === cycle;
  function clearRaw() { rawReads.current++; rawReader.current?.abort(); setRaw(null); setRawError(""); setRawLoading(false); }
  function accept(value: TrialEnvelope, from?: Pending): boolean {
    const next = value.trial, prior = current.current;
    if (from && (!next || !sameTrialInput(from.frozen, next) || next.state === "PREPARED")) throw new Error("unexpected_trial_response");
    if (from && prior && !sameTrialInput(prior, from.frozen)) return false;
    if (next && prior && next.binding_sha256 === prior.binding_sha256 && !advancesTrial(prior, next)) {
      if (from && trialTerminal(prior.state)) return false;
      throw new Error("stale_trial_response");
    }
    if (next) current.current = next;
    setShown(value); setMustRefresh(false);
    const waiting = pendingRef.current;
    if (next && waiting && sameTrialInput(waiting.frozen, next)
      && (trialTerminal(next.state) || waiting.action === "execute" && next.attempts === 1)) setPending(null);
    return true;
  }
  async function reload() {
    const request = ++reads.current, cycle = epoch.current;
    reader.current?.abort(); const controller = new AbortController(); reader.current = controller;
    setLoading(true); setError(""); setNotice(""); clearRaw();
    try {
      const value = await getAnthropicTrial(controller.signal);
      if (!active(cycle) || request !== reads.current) return;
      accept(value); setNotice("저장된 시험 상태를 확인했습니다. 새 모델 호출을 요청하지 않았습니다.");
    } catch {
      if (active(cycle) && request === reads.current) { setMustRefresh(true); setError("Anthropic 시험 상태를 확인하지 못했습니다. 현재 권한과 로컬 앱 연결을 확인하고 상태를 새로고침하세요."); }
    } finally { if (active(cycle) && request === reads.current) setLoading(false); }
  }
  useEffect(() => {
    alive.current = true; void reload();
    return () => { alive.current = false; epoch.current++; reads.current++; rawReads.current++; reader.current?.abort(); rawReader.current?.abort(); };
  }, []);

  const trial = shown?.trial ?? pending?.frozen ?? current.current;
  const outputDiagnostic = trial?.output_diagnostic;
  const usage = outputDiagnostic?.usage ?? trial?.usage;
  const mayExecute = !!shown?.enabled && !!shown.trial?.can_execute && !loading && !mustRefresh && !pending && !executing && !cancelling
    && shown.trial.state === "PREPARED" && Date.parse(shown.trial.expires_at) > Date.now();
  const cancelTrial = pending?.frozen ?? shown?.trial ?? current.current;
  const mayCancel = !!cancelTrial && !trialTerminal(current.current?.state ?? cancelTrial.state) && !cancelling;
  async function mutate(action: "execute" | "cancel") {
    if (!alive.current || (action === "execute" ? executionLock.current || !mayExecute : cancelLock.current || !mayCancel)) return;
    const frozen = action === "execute" ? shown!.trial! : cancelTrial!;
    const request: Pending = { action, frozen, body: { expected_binding_sha256: frozen.binding_sha256 } };
    const cycle = epoch.current;
    if (action === "execute") { executionLock.current = true; setExecuting(true); }
    else { cancelLock.current = true; setCancelling(true); }
    reads.current++; reader.current?.abort(); setLoading(false); clearRaw(); setPending(request); setError(""); setNotice("");
    try {
      const value = await actOnAnthropicTrial(action, request.body);
      if (!active(cycle)) return;
      if (accept(value, request)) { setPending(null); setNotice(action === "cancel"
        ? value.trial?.state === "CANCELLED"
          ? "취소 기록을 확인했습니다. 이미 전송된 요청의 원격 중단이나 비용 취소를 보장하지 않습니다."
          : "이미 종료 상태가 기록되어 취소로 변경하지 않았습니다. 표시된 저장 결과를 확인하세요."
        : "시험 기록을 확인했습니다. 구조 검증과 과학적 검토·원고 채택은 별개입니다."); }
    } catch (failure) {
      if (!active(cycle) || current.current && trialTerminal(current.current.state) && sameTrialInput(current.current, frozen)) return;
      const definitive = failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && ![408, 429].includes(failure.status);
      if (definitive) { if (pendingRef.current?.action === action) setPending(null); setMustRefresh(true); }
      setError(definitive ? "시험 요청이 거절되었습니다. 상태를 새로고침하고 변경된 조건을 확인하세요."
        : "시험 요청의 응답을 확인하지 못했습니다. 원래 요청을 보존했습니다. 상태 새로고침 또는 취소만 사용하며 자동 재실행하지 않습니다.");
    } finally {
      if (action === "execute") executionLock.current = false; else cancelLock.current = false;
      if (active(cycle)) { if (action === "execute") setExecuting(false); else setCancelling(false); }
    }
  }
  async function reveal() {
    if (!trial?.raw_capture_available || rawLoading) return;
    const frozen = trial, request = ++rawReads.current, cycle = epoch.current;
    rawReader.current?.abort(); const controller = new AbortController(); rawReader.current = controller;
    setRawLoading(true); setRawError("");
    try {
      const text = await getAnthropicCapture(frozen, controller.signal);
      if (active(cycle) && request === rawReads.current && current.current && sameTrialInput(current.current, frozen)) setRaw(text);
    } catch {
      if (active(cycle) && request === rawReads.current) setRawError("원시 응답을 확인하지 못했습니다. 저장 여부·시험 식별자·내용 지문을 다시 확인하세요. 새 모델 호출은 하지 않았습니다.");
    } finally { if (active(cycle) && request === rawReads.current) setRawLoading(false); }
  }
  return <section className="anthropic-trial-panel" aria-label="Anthropic 1회 시험">
    <h3>Anthropic 1회 시험</h3>
    <p>서버가 별도로 허용한 고정 발췌·모델·키 버전으로 첫 응답을 확인합니다. 일반 실행 권한이나 원고 채택 권한을 만들지 않습니다.</p>
    <p>운영자가 선택한 발췌의 응답을 확인하는 시험입니다. Paper Bank 검토 기록이나 채택된 Journal을 저장하지 않습니다.</p>
    <p>선택한 발췌만 전달하며 논문 전체 검토가 아닙니다. 과학적 타당성은 미평가입니다. 자동 재실행과 자동 채택은 하지 않습니다.</p>
    <V2Button type="button" onClick={() => void reload()}>Anthropic 시험 상태 새로고침</V2Button>
    {loading ? <p role="status">Anthropic 시험 상태 확인 중</p> : null}
    {shown?.enabled === false ? <p>이 서버에서는 1회 시험 실행이 비활성화되어 있습니다. 저장된 기록이 있으면 상태를 확인할 수 있습니다.</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {trial ? <>
      <p><strong>{trial.provider_diagnostic && trial.state === "UNKNOWN" ? "Provider가 요청을 거절함 · 자동 재실행하지 않음" : labels[trial.state]}</strong></p>
      {trial.provider_diagnostic ? <>
        <p>{trialDiagnosticLabels[trial.provider_diagnostic]}</p>
        <p>기존 저장 상태: {trial.state} · 저장된 HTTP 오류 응답을 해석한 안내입니다. 원본 이력은 바꾸지 않습니다.</p>
      </> : null}
      {outputDiagnostic ? <section className="anthropic-trial-diagnostic" aria-label="응답 검증 진단">
        <h4>응답은 받았지만 검토 결과로 사용할 수 없습니다</h4>
        <ul>{outputDiagnostic.reason_codes.map(code => <li key={code}>{trialOutputReasonLabels[code]}</li>)}</ul>
        <p>아래 모델·토큰 정보와 진단은 저장된 원시 응답에서 읽었습니다. 원본 이력과 실패 상태를 바꾸지 않으며, 새 모델 호출이나 초안 채택을 하지 않습니다.</p>
        <p>Provider의 보고 값이며 실제 계정·청구 금액·과학적 타당성을 검증한 것은 아닙니다.</p>
        <details><summary>진단에 사용한 응답 지문</summary><code>{outputDiagnostic.raw_response_sha256}</code></details>
      </section> : null}
      {trial.origin === "FIXTURE_SYNTHETIC" ? <p>합성 시험 · 실제 모델 실행의 증거가 아닙니다.</p> : <p>공개 논문의 지정 발췌 · 표시한 범위만 외부 Provider에 전송합니다.</p>}
      {(loading || mustRefresh || shown?.trial === null) ? <p>마지막으로 확인한 내용 · 실행 조건은 새로고침으로 확인하세요.</p> : null}
      <h4>{trial.source.title}</h4>
      <p>페이지 {trial.source.page} · 문자 범위 {trial.source.start}–{trial.source.end}</p>
      <pre className="anthropic-trial-text" aria-label="전송할 발췌">{trial.source.excerpt}</pre>
      <dl className="anthropic-trial-facts">
        <div><dt>요청 모델</dt><dd>{trial.model}</dd></div>
        <div><dt>최대 출력 토큰</dt><dd>{trial.max_output_tokens} · thinking과 최종 텍스트를 포함합니다.</dd></div>
        <div><dt>고정 키 버전</dt><dd>{trial.credential_revision} · 키 값과 계정 유효성은 표시하지 않습니다.</dd></div>
        <div><dt>실행 허용 만료</dt><dd>{formatTimestamp(trial.expires_at, preferences.timeFormat, "ko-KR")}</dd></div>
        <div><dt>기록된 시도</dt><dd>{trial.attempts}/1 · 실제 비용 상한을 보장하지 않습니다.</dd></div>
        <div><dt>시험 식별자</dt><dd>{trial.trial_id}</dd></div>
        <div><dt>입력 결합 SHA-256</dt><dd>{trial.binding_sha256}</dd></div>
        <div><dt>원본 PDF SHA-256</dt><dd>{trial.source.pdf_sha256}</dd></div>
        <div><dt>발췌 SHA-256</dt><dd>{trial.source.excerpt_sha256}</dd></div>
      </dl>
      {trial.http_timeouts ? <p>HTTP 대기 제한: 연결 {trial.http_timeouts.connect}초 · 전송 {trial.http_timeouts.write}초 · 응답 읽기 {trial.http_timeouts.read}초 · 연결 풀 {trial.http_timeouts.pool}초.
        각 단계에서 데이터가 오지 않는 대기 시간이며 전체 실행 시간의 상한은 아닙니다.</p> : null}
      {Date.parse(trial.expires_at) <= Date.now() && trial.state === "PREPARED" ? <p>실행 허용 시간이 지났습니다. 새 실행을 요청하지 않습니다.</p> : null}
      <div className="anthropic-trial-actions">
        <V2Button type="button" disabled={!mayExecute} busy={executing} onClick={() => void mutate("execute")}>확인한 발췌로 1회 시험 실행</V2Button>
        <V2Button type="button" disabled={!mayCancel} busy={cancelling} onClick={() => void mutate("cancel")}>Anthropic 시험 취소</V2Button>
      </div>
      <p>시도 횟수는 1회 실행 기회를 소비한 기록입니다. HTTP 전송 전에 멈춘 경우에도 1회로 남을 수 있습니다. 취소가 이미 전송된 요청의 원격 중단이나 비용 취소를 보장하지 않습니다.</p>
      <p>Provider 보고 모델: {outputDiagnostic?.reported_model ?? trial.actual_model ?? "미확인"} · 응답 식별자: {outputDiagnostic?.response_id ?? trial.response_id ?? "미확인"}</p>
      {usage ? <div className="anthropic-trial-usage">
        <p>비캐시 입력 토큰: {usage.input_tokens ?? "미보고"} · 출력 토큰: {usage.output_tokens ?? "미보고"}</p>
        <p>캐시 생성 토큰: {usage.cache_creation_input_tokens ?? "미보고"} · 캐시 읽기 토큰: {usage.cache_read_input_tokens ?? "미보고"}</p>
      </div> : <p>토큰 사용량: 미보고</p>}
      {trial.http_status !== null ? <p>HTTP 상태: {trial.http_status}</p> : null}
      {trial.error_code ? <p>기록된 오류: {trial.error_code}</p> : null}
      {trial.transport_trace?.length ? <details><summary>저장된 통신 단계 보기</summary>
        <p>HTTP 처리 시작부터의 경과 시간입니다. 서버 내부 처리·생각 시간이나 과금 여부는 확인하지 않습니다. 실행 종료 후 저장된 기록입니다.</p>
        <ol>{trial.transport_trace.map((item, index) => <li key={index}>{trialTraceLabel(item.event)} · {(item.elapsed_ms / 1000).toFixed(3)}초</li>)}</ol>
      </details> : null}
      {trial.draft ? <details><summary>구조 검증된 초안 보기 · 미채택</summary><pre className="anthropic-trial-text">{JSON.stringify(trial.draft, null, 2)}</pre></details> : null}
      {trial.raw_capture_available ? <div>
        <V2Button type="button" disabled={rawLoading} onClick={() => raw === null ? void reveal() : clearRaw()}>{raw === null ? "저장된 원시 응답 보기" : "원시 응답 숨기기"}</V2Button>
        {rawLoading ? <p role="status">저장된 원시 응답 확인 중</p> : null}
        {raw !== null ? <pre className="anthropic-trial-text" aria-label="저장된 원시 응답">{raw}</pre> : null}
      </div> : <p>저장된 원시 응답: 없음 또는 사용 불가</p>}
    </> : null}
    {pending ? <div className="anthropic-trial-pending"><p>응답 미확인 요청 · 원래 요청의 결합 지문을 유지합니다.</p><code>{pending.body.expected_binding_sha256}</code>
      <p>새 실행을 만들지 않고 상태를 조회하세요. 화면을 다시 열어도 저장된 상태를 먼저 확인합니다.</p></div> : null}
    {rawError ? <p role="alert">{rawError}</p> : null}{notice ? <p role="status">{notice}</p> : null}
  </section>;
}
