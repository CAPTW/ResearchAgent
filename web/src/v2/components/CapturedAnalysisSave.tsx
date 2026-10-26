import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api";
import { useOptionalAppearance } from "../AppearanceProvider";
import { formatTimestamp } from "../appearance";
import { V2Button } from "../V2Primitives";
import { getCapturedAnalysisSource, readCapturedAnalysisAcknowledgement, saveCapturedAnalysis, type CapturedAnalysisCommand, type CapturedAnalysisSource, type SavedAnalysis } from "../savedAnalyses";

type Attempt = { source: CapturedAnalysisSource; command: CapturedAnalysisCommand };
type Props = { topicId: string; disabled: boolean; onSaving: (saving: boolean) => void; onSaved: (value: SavedAnalysis) => void };
const localAccess = "이 기능은 같은 로컬 앱 주소(/app)의 로컬 사용자만 사용할 수 있습니다. 현재 접근 권한과 앱 주소를 확인하세요.";
export function CapturedAnalysisSave({ topicId, disabled, onSaving, onSaved }: Props) {
  const [source, setSource] = useState<CapturedAnalysisSource | null>(null), [attempt, setAttempt] = useState<Attempt | null>(null);
  const [phase, setPhase] = useState<"reading" | "saving" | null>(null), [error, setError] = useState(""), [message, setMessage] = useState("");
  const alive = useRef(true), generation = useRef(0), busy = useRef(false), pending = useRef<Attempt | null>(null);
  const { preferences } = useOptionalAppearance();
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++; }; }, []);
  const current = (epoch: number) => alive.current && epoch === generation.current;
  const inspect = async () => {
    if (busy.current || pending.current || disabled) return;
    const epoch = ++generation.current; busy.current = true; setPhase("reading"); setSource(null); setError(""); setMessage("");
    try {
      const value = await getCapturedAnalysisSource(topicId);
      if (!current(epoch)) return;
      if (value.available) setSource(value.source);
      else setMessage(value.reason === "anthropic_trial_disabled" ? "이 서버에는 확인할 앱 실행 기록이 연결되지 않았습니다." : "앱에 보존된 성공 응답을 아직 저장할 수 없습니다. 성공 응답이 없거나 저장 계약을 충족하지 못했습니다.");
    } catch (cause) {
      if (current(epoch)) setError(cause instanceof ApiError && cause.status === 403 ? localAccess : "앱의 보존 응답을 확인하지 못했습니다. 다시 확인 버튼으로 재요청하세요.");
    } finally { if (current(epoch)) { busy.current = false; setPhase(null); } }
  };
  const cancelInspection = () => { if (phase !== "reading") return; generation.current++; busy.current = false; setPhase(null); setMessage("응답 확인을 취소했습니다. 새 확인은 직접 요청하세요."); };
  const save = async () => {
    if (busy.current || disabled || (!pending.current && !source)) return;
    const selected = pending.current ?? { source: source!, command: { expected_binding_sha256: source!.binding_sha256, expected_response_sha256: source!.response_sha256 } };
    pending.current = selected; setAttempt(selected); busy.current = true; setPhase("saving"); setError(""); setMessage("");
    const epoch = ++generation.current; onSaving(true);
    try {
      const value = readCapturedAnalysisAcknowledgement(await saveCapturedAnalysis(topicId, selected.command), topicId, selected.source);
      if (!current(epoch)) return;
      pending.current = null; setAttempt(null); setMessage("확인한 응답을 이 주제의 분석으로 저장했습니다. 사람의 검토와 저널 채택은 별도입니다."); onSaved(value);
    } catch (cause) {
      if (!current(epoch)) return;
      if (cause instanceof ApiError && cause.status === 409) {
        pending.current = null; setAttempt(null); setSource(null); setError("확인한 실행 또는 응답이 달라졌습니다. 앱에 보존된 성공 응답을 다시 확인한 뒤 저장하세요.");
      } else if (cause instanceof ApiError && (cause.status === 400 || cause.status === 404 || cause.status === 422)) {
        pending.current = null; setAttempt(null); setSource(null); setError("이 응답의 저장 요청이 거부되었습니다. 보존된 성공 응답을 다시 확인하세요.");
      } else setError(cause instanceof ApiError && cause.status === 403 ? localAccess : "저장 결과를 확인하지 못했습니다. 이미 저장되었을 수 있습니다. 같은 응답 저장 다시 확인을 누르면 처음 확인한 실행과 응답으로만 재요청합니다.");
    } finally { if (current(epoch)) { busy.current = false; setPhase(null); onSaving(false); } }
  };
  return <section aria-label="앱 보존 응답 저장" className="captured-analysis-save" aria-busy={phase !== null}>
    <h4>앱에 보존된 응답에서 저장</h4>
    <p>서버가 보존한 성공 응답의 사본을 현재 주제에 저장합니다. 확인·저장은 모델을 호출하거나 실행을 재시도하지 않습니다.</p>
    <V2Button type="button" disabled={disabled || phase !== null || attempt !== null} onClick={() => void inspect()}>앱에 보존된 성공 응답 확인</V2Button>
    {phase === "reading" ? <><p role="status">앱 보존 응답 확인 중</p><V2Button type="button" onClick={cancelInspection}>응답 확인 취소</V2Button></> : null}
    {source ? <div className="captured-analysis-source">
      <p>요청 모델: {source.model}</p><p>{source.origin === "FIXTURE_SYNTHETIC" ? "합성 fixture 발췌 · 실제 AI 연구 실행의 증거가 아닙니다." : "공개 발췌 · 전체 논문이 아닌 아래 구간의 보존 응답입니다."}</p>
      <p>출처: {source.title} · {source.page}쪽 · 문자 위치 {source.start}–{source.end}</p>
      <p>문자 위치는 0부터 시작하며 끝 위치는 포함하지 않습니다.</p><pre tabIndex={0}>{source.excerpt}</pre>
      <p>앱 캡처 시각: {formatTimestamp(source.captured_at, preferences.timeFormat, "ko-KR")}</p>
      <p>전체 논문 검토·실제 모델 신원·과학적 뒷받침은 이 저장으로 검증되지 않습니다. 새 분석은 사람의 검토가 필요합니다. 기존 분석의 검토 상태는 유지됩니다.</p>
      <details><summary>저장할 실행과 응답 식별자</summary><p>실행: {source.trial_id}</p><p>실행 결합 SHA-256: {source.binding_sha256}</p><p>응답 SHA-256: {source.response_sha256}</p></details>
      <V2Button type="button" disabled={disabled || phase !== null} onClick={() => void save()}>{attempt ? "같은 응답 저장 다시 확인" : "이 주제에 분석 저장"}</V2Button>
    </div> : null}
    {phase === "saving" ? <p role="status">확인한 응답 저장 중 · 완료 전에는 다른 분석 선택과 파일 반입을 잠시 기다려 주세요.</p> : null}
    {error ? <p role="alert">{error}</p> : null}{message ? <p role="status">{message}</p> : null}
  </section>;
}
