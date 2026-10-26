import { useState } from "react";
import { executionStates, reportedJournalModel, type ExecutionDetail, type ExecutionEvent } from "../../lib/journalExecutions";
import { readJournalAdapterManifest } from "./JournalAdapterManifest";

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 1_000_000_000 ? value : null;
}

function Attempt({ events, limit }: { events: ExecutionEvent[]; limit: number }) {
  const latest = events[events.length - 1];
  const leased = events.find(event => event.state === "LEASED");
  const reservation = leased?.dispatch;
  const manifest = readJournalAdapterManifest(leased?.adapter_manifest);
  const report = latest.state === "SUCCEEDED" ? latest.result?.provider_reported : null;
  const input = count(report?.usage.input_tokens), output = count(report?.usage.output_tokens), total = count(report?.usage.total_tokens);
  const anthropic = report?.provider_name === "anthropic" && manifest?.schema_version === "ANTHROPIC_JOURNAL_WIRE_V1"
    && manifest.usage_input_semantics === "NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE";
  const cacheCreation = count(report?.usage.cache_creation_input_tokens), cacheRead = count(report?.usage.cache_read_input_tokens);
  const inconsistent = !anthropic && input !== null && output !== null && total !== null && input + output !== total;
  return <section aria-label={`시도 ${latest.attempt} 토큰 기록`}>
    <h6>시도 {latest.attempt} · {executionStates[latest.state]}</h6>
    <dl>
      <dt>요청 출력 토큰 상한</dt><dd>{limit}</dd>
      <dt>이 시도의 출력 토큰 예약 상한</dt><dd>{reservation?.reserved_output_tokens ?? "예약 기록 없음"}</dd>
      <dt>{anthropic ? "보고된 비캐시 입력 토큰" : "보고된 입력 토큰"}</dt><dd>{input ?? "미보고"}</dd>
      {anthropic ? <><dt>보고된 캐시 생성 입력 토큰</dt><dd>{cacheCreation ?? "미보고"}</dd>
        <dt>보고된 캐시 읽기 입력 토큰</dt><dd>{cacheRead ?? "미보고"}</dd></> : null}
      <dt>보고된 출력 토큰</dt><dd>{output ?? "미보고"}</dd>
      <dt>보고된 합계 토큰</dt><dd>{total ?? "미보고"}</dd>
    </dl>
    {report ? <p className="local-report-id">보고 도구·모델: {report.provider_name} · {reportedJournalModel(report.model)} · 응답 식별자: {report.response_id ?? "기록 없음"}</p>
      : <p>저장된 성공 응답의 사용량 보고가 없습니다. 0 사용이나 무료라는 뜻은 아닙니다.</p>}
    {anthropic ? <p>Anthropic 입력 토큰은 비캐시 입력이며 캐시 생성·읽기 입력은 별도로 표시합니다. 합계는 직접 보고된 값만 표시하고 이 수치들을 더해 만들지 않습니다.</p> : null}
    {report && (input === null || output === null || total === null || (anthropic && (cacheCreation === null || cacheRead === null))) ? <p>일부 사용량이 보고되지 않았습니다. 없는 값은 다른 수치로 계산하지 않습니다.</p> : null}
    {inconsistent ? <p role="note">입력·출력의 합과 보고된 합계가 다릅니다. 원래 보고 값을 그대로 표시합니다.</p> : null}
    {output !== null && output > limit ? <p role="note">보고된 출력 토큰이 요청 상한보다 큽니다. 이 기록으로 한도 준수나 요금을 확인할 수 없습니다.</p> : null}
  </section>;
}

export function JournalExecutionEvidence({ saved }: { saved: ExecutionDetail }) {
  const [open, setOpen] = useState(false), [requestOpen, setRequestOpen] = useState(false);
  const request = saved.contract.provider_request;
  const attempts = [...new Set(saved.events.filter(event => event.attempt > 0).map(event => event.attempt))];
  return <details className="journal-execution-evidence" onToggle={event => { setOpen(event.currentTarget.open); if (!event.currentTarget.open) setRequestOpen(false); }}>
    <summary>요청 내용·토큰 사용 기록</summary>
    {open ? <>
      <p>서버에 보존된 요청과 응답 기록입니다. 화면을 여는 동작은 외부로 전송하지 않습니다.</p>
      {request ? <details onToggle={event => setRequestOpen(event.currentTarget.open)}>
        <summary>보존된 어댑터 입력 보기</summary>
        {requestOpen ? <>
          <p>어댑터에 전달하도록 고정한 입력입니다. 실제 전송 여부나 외부 모델 실행을 증명하지 않습니다.</p>
          <p>출력 형식: {request.schema_name} · 요청 온도: {request.temperature} · 요청 출력 토큰 상한: {request.max_output_tokens}</p>
          <h5>지시문</h5><pre aria-label="보존된 지시문" tabIndex={0}>{request.instructions}</pre>
          <h5>문헌 입력</h5><pre aria-label="보존된 문헌 입력" tabIndex={0}>{request.prompt}</pre>
          <h5>출력 JSON 규격</h5><pre aria-label="보존된 출력 JSON 규격" tabIndex={0}>{JSON.stringify(request.schema, null, 2)}</pre>
          <h5>요청 메타데이터</h5><pre aria-label="보존된 요청 메타데이터" tabIndex={0}>{JSON.stringify(request.metadata, null, 2)}</pre>
        </> : null}
      </details> : <p>보존된 어댑터 입력을 확인할 수 없습니다. 현재 문헌으로 다시 만들지 않습니다.</p>}
      <h5>시도별 토큰 사용 기록</h5>
      <p>요청·예약 상한과 응답이 보고한 사용량을 구분합니다. 청구 내역이나 금액 예산이 아닙니다. 미보고된 시도를 0으로 합산하지 않습니다.</p>
      {attempts.length ? attempts.map(attempt => <Attempt key={attempt} events={saved.events.filter(event => event.attempt === attempt)} limit={saved.contract.specification.max_output_tokens}/>)
        : <p>저장된 호출 시도가 없습니다.</p>}
    </> : null}
  </details>;
}
