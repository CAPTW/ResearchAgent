import type { AntigravityJournalManifest, AnthropicJournalManifest, ClaudeJournalManifest, CodexJournalManifest, CodexJournalV2Manifest, CodexV2Policy, GrokJournalManifest, JournalAdapterManifest as Manifest, OpenAIJournalManifest, XaiJournalManifest } from "../../lib/journalExecutions";

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const bytes = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const timeout = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value > 0;

export function hasCodexV2Policy(value: unknown): value is CodexV2Policy & Record<string, unknown> {
  return record(value) && value.runtime_profile === "CODEX_JOURNAL_CLI_V2" && value.model_provider === "xrml_openai"
    && value.requires_openai_auth === true && value.wire_api === "responses" && value.forced_login_method === "chatgpt"
    && value.request_max_retries === 0 && value.stream_max_retries === 0 && value.supports_websockets === false
    && value.authentication === "OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED" && value.endpoint_resolution === "OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED"
    && value.retry_policy === "REQUESTED_ZERO_NOT_LIVE_VERIFIED" && value.xrml_process_relaunch === false && value.remote_call_count_enforced === false
    && value.cost_limit_enforced === false && value.live_execution === "NOT_VERIFIED" && value.billing_route === "CHATGPT_ACCOUNT_NOT_VERIFIED"
    && value.raw_output_encoding === "LAST_MESSAGE_JSON_UTF8_V1" && value.raw_envelope_retained === false;
}

export function readJournalAdapterManifest(value: unknown): Manifest | null {
  if (!record(value) || !text(value.model) || !timeout(value.timeout_seconds)) return null;
  switch (value.schema_version) {
    case "GROK_JOURNAL_ACP_V1": {
      const runtime = value.runtime_identity;
      return hash(value.contract_digest)
        && record(runtime) && hash(runtime.launcher_sha256) && hash(runtime.environment_sha256) && hash(runtime.workspace_sha256)
        && Array.isArray(value.argv_shape) && value.argv_shape.length > 0 && value.argv_shape.every(text)
        && hash(value.prompt_sha256) && bytes(value.prompt_bytes) && hash(value.schema_sha256) && hash(value.agent_sha256)
        && bytes(value.max_stdout_bytes) && bytes(value.max_stderr_bytes) && value.max_journal_output_bytes === 262144 && value.max_events === 4096
        && value.protocol_version === 1 && value.prompt_count === 1 && record(value.client_capabilities) && Object.keys(value.client_capabilities).length === 0
        && value.authentication === "OFFICIAL_CLI_CACHED_TOKEN_ONLY_NOT_VERIFIED" && value.billing_route === "NOT_VERIFIED"
        && value.session_retention === "CLI_MANAGED_PROFILE_STORAGE_NOT_ISOLATED" && value.model_selection === "ACP_CONFIG_OPTION_MODEL_EXACT"
        && value.structured_output === "PROMPT_REQUESTED_LOCAL_VALIDATION_ONLY" && value.completion === "CORRELATED_PROMPT_STOP_REASON"
        && value.process_cleanup === "HOST_TERMINATES_OWNED_ACP_CHILD_AFTER_TERMINAL"
        && value.automatic_retry === false && value.xrml_process_relaunch === false && value.remote_call_count_enforced === false
        && value.internal_retries === "CLI_MANAGED_NOT_LIVE_VERIFIED" && value.managed_hooks === "NOT_ISOLATED_NOT_LIVE_VERIFIED"
        && value.global_config_and_mcp === "NOT_ISOLATED_NOT_LIVE_VERIFIED" && value.tool_isolation === "NOT_ESTABLISHED"
        && value.output_rejection_prevents_side_effects === false && value.physical_remote_cancellation === "NOT_ESTABLISHED"
        && value.output_token_limit_enforced === false && value.temperature_enforced === false && value.cost_limit_enforced === false
        && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED" && value.token_usage === "NOT_REPORTED_BY_SUPPORTED_ACP_PROFILE"
        && value.raw_journal_encoding === "ACP_AGENT_MESSAGE_TEXT_UTF8_V1" && value.journal_result_storage === "AGENT_MESSAGE_TEXT_ONLY"
        && value.raw_envelope_retained === false ? value as GrokJournalManifest : null;
    }
    case "ANTIGRAVITY_JOURNAL_CLI_V1": {
      const runtime = value.runtime_identity;
      return hash(value.contract_digest)
        && record(runtime) && hash(runtime.launcher_sha256) && hash(runtime.environment_sha256) && hash(runtime.workspace_sha256)
        && Array.isArray(value.argv_shape) && value.argv_shape.length > 0 && value.argv_shape.every(text)
        && hash(value.stdin_sha256) && bytes(value.stdin_bytes) && hash(value.schema_sha256) && hash(value.agent_sha256)
        && bytes(value.max_stdout_bytes) && bytes(value.max_stderr_bytes) && value.max_journal_output_bytes === 262144
        && value.input_turns === 1 && value.reasoning_effort === "low" && value.agent_name === "xrml-paper-review-v1"
        && value.automatic_retry === false && value.xrml_process_relaunch === false && value.remote_call_count_enforced === false
        && value.internal_retries === "CLI_MANAGED_NOT_LIVE_VERIFIED" && value.managed_hooks === "NOT_ISOLATED_NOT_LIVE_VERIFIED"
        && value.global_config_and_mcp === "NOT_ISOLATED_NOT_LIVE_VERIFIED" && value.windows_sandbox === "REQUESTED_NOT_LIVE_VERIFIED"
        && value.output_token_limit_enforced === false && value.temperature_enforced === false && value.cost_limit_enforced === false
        && value.tool_isolation === "REQUESTED_AND_OUTPUT_CHECKED_NOT_LIVE_VERIFIED" && value.output_rejection_prevents_side_effects === false
        && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED" && value.authentication === "OFFICIAL_CLI_OWNED_NOT_VERIFIED"
        && value.billing_route === "NOT_VERIFIED" && value.raw_journal_encoding === "RESULT_RESPONSE_JSON_UTF8_V1"
        && value.journal_result_storage === "RESULT_RESPONSE_ONLY" && value.raw_envelope_retained === false
        && value.step_text === "NONCANONICAL_STREAM_TELEMETRY" ? value as AntigravityJournalManifest : null;
    }
    case "CLAUDE_JOURNAL_CLI_V1": {
      const runtime = value.runtime_identity;
      return hash(value.contract_digest)
        && record(runtime) && hash(runtime.launcher_sha256) && hash(runtime.environment_sha256) && hash(runtime.workspace_sha256) && hash(runtime.config_dir_sha256)
        && Array.isArray(value.argv_shape) && value.argv_shape.length > 0 && value.argv_shape.every((arg, index, args) =>
          text(arg) || (arg === "" && ["--tools", "--setting-sources"].includes(args[index - 1])))
        && hash(value.stdin_sha256) && bytes(value.stdin_bytes) && hash(value.schema_sha256) && hash(value.system_prompt_sha256)
        && bytes(value.max_stdout_bytes) && bytes(value.max_stderr_bytes) && bytes(value.max_journal_output_bytes)
        && value.automatic_retry === false && value.xrml_process_relaunch === false && value.remote_call_count_enforced === false
        && value.internal_retries === "CLI_MANAGED_NOT_LIVE_VERIFIED" && value.managed_hooks === "NOT_ISOLATED_NOT_LIVE_VERIFIED"
        && value.output_token_limit_enforced === false && value.temperature_enforced === false && value.cost_limit_enforced === false
        && value.tool_isolation === "REQUESTED_NOT_LIVE_VERIFIED" && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
        && value.authentication === "OFFICIAL_CLI_OWNED_NOT_VERIFIED" && value.billing_route === "NOT_VERIFIED"
        && value.raw_journal_encoding === "EXTRACTED_CANONICAL_JSON_V1" && value.journal_result_storage === "EXTRACTED_STRUCTURED_OUTPUT_ONLY"
        && value.raw_envelope_retained === false ? value as ClaudeJournalManifest : null;
    }
    case "OPENAI_JOURNAL_WIRE_V1":
      return text(value.endpoint) && bytes(value.body_bytes) && hash(value.body_sha256) ? value as OpenAIJournalManifest : null;
    case "ANTHROPIC_JOURNAL_WIRE_V1":
      return value.adapter_id === "xrml-anthropic-frozen-journal" && value.adapter_version === "1" && value.provider_name === "anthropic"
        && value.method === "POST" && value.endpoint === "https://api.anthropic.com/v1/messages" && value.api_version === "2023-06-01"
        && hash(value.contract_digest) && hash(value.request_sha256) && hash(value.body_sha256) && bytes(value.body_bytes)
        && value.timeout_seconds <= 120 && value.max_response_bytes === 4000000 && value.max_journal_output_bytes === 512000
        && value.schema_compiler === "ANTHROPIC_JSON_SCHEMA_V1" && value.schema_constraints === "LOCAL_ORIGINAL_VALIDATION"
        && hash(value.local_schema_sha256) && hash(value.wire_schema_sha256)
        && value.stream === false && Array.isArray(value.tools) && value.tools.length === 0
        && value.automatic_retry === false && value.redirects === false && value.environment_proxy === false
        && value.output_token_limit_enforced === false && value.temperature_enforced === false && value.cost_limit_enforced === false
        && value.temperature_policy === "OMITTED_PROVIDER_DEFAULT"
        && value.request_metadata === "LOCAL_BINDING_ONLY_NOT_TRANSMITTED" && value.output_token_scope === "MESSAGES_OUTPUT_TOKENS"
        && value.token_limit_policy === "REQUESTED_NOT_LIVE_VERIFIED" && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
        && value.live_execution === "NOT_VERIFIED" && value.billing_route === "API_KEY_ACCOUNT_NOT_VERIFIED"
        && value.raw_output_encoding === "CONTENT_TEXT_JSON_UTF8_V1" && value.raw_envelope_retained === false
        && value.usage_input_semantics === "NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE" && value.total_tokens === "NOT_REPORTED_NOT_DERIVED"
        ? value as AnthropicJournalManifest : null;
    case "XAI_JOURNAL_WIRE_V1":
      return value.endpoint === "https://api.x.ai/v1/responses" && bytes(value.body_bytes) && hash(value.body_sha256)
        && value.store === false && value.stream === false && Array.isArray(value.tools) && value.tools.length === 0
        && value.redirects === false && value.environment_proxy === false && value.automatic_retry === false
        && value.schema_compiler === "XAI_NONBLANK_V1" && value.output_token_limit_enforced === false && value.temperature_enforced === false
        && value.output_token_scope === "VISIBLE_OUTPUT_ONLY" && value.truncation_enforced === false && value.cost_limit_enforced === false
        && value.request_metadata === "LOCAL_BINDING_ONLY_NOT_TRANSMITTED" && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
        ? value as XaiJournalManifest : null;
    case "CODEX_JOURNAL_CLI_V2": {
      const runtime = value.runtime_identity;
      return value.adapter_id === "xrml-codex-frozen-journal" && value.adapter_version === "2" && hasCodexV2Policy(value)
        && hash(value.contract_digest) && hash(value.contract_sha256) && hash(value.request_sha256) && text(value.reasoning_effort)
        && record(runtime) && hash(runtime.launcher_sha256) && hash(runtime.environment_sha256) && hash(runtime.workspace_sha256)
        && Array.isArray(value.argv_shape) && value.argv_shape.length > 0 && value.argv_shape.every(text)
        && hash(value.stdin_sha256) && bytes(value.stdin_bytes) && hash(value.schema_sha256)
        && bytes(value.max_stdout_bytes) && bytes(value.max_stderr_bytes) && bytes(value.max_final_bytes)
        && value.automatic_retry === false && value.output_token_limit_enforced === false && value.temperature_enforced === false
        && value.tool_isolation === "REQUESTED_NOT_LIVE_VERIFIED" && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
        ? value as CodexJournalV2Manifest : null;
    }
    case "CODEX_JOURNAL_CLI_V1": {
      const runtime = value.runtime_identity;
      return hash(value.contract_digest) && text(value.reasoning_effort)
        && record(runtime) && hash(runtime.launcher_sha256) && hash(runtime.environment_sha256) && hash(runtime.workspace_sha256)
        && Array.isArray(value.argv_shape) && value.argv_shape.length > 0 && value.argv_shape.every(text)
        && hash(value.stdin_sha256) && bytes(value.stdin_bytes) && hash(value.schema_sha256)
        && bytes(value.max_stdout_bytes) && bytes(value.max_stderr_bytes) && bytes(value.max_final_bytes)
        && value.automatic_retry === false && value.output_token_limit_enforced === false && value.temperature_enforced === false
        && value.tool_isolation === "REQUESTED_NOT_LIVE_VERIFIED" && value.model_identity === "REQUESTED_NOT_INDEPENDENTLY_VERIFIED"
        ? value as CodexJournalManifest : null;
    }
    default: return null;
  }
}

export function JournalAdapterManifest({ value, context = "record" }: { value: unknown; context?: "record" | "preview" }) {
  const manifest = readJournalAdapterManifest(value);
  return <details><summary>{context === "preview" ? "현재 미리보기 전송 조건" : "고정된 전송 조건"}</summary>
    {!manifest ? <>
      <p>전송 조건 형식이 지원되지 않거나 필요한 값이 없습니다. 전송 방식·한도·모델 사용 여부를 추정하지 않습니다.</p>
      {record(value) && text(value.schema_version) ? <p className="local-report-id">기록된 형식: {value.schema_version}</p> : null}
    </> : manifest.schema_version === "OPENAI_JOURNAL_WIRE_V1" ? <>
      <p className="local-report-id">요청 주소: {manifest.endpoint}</p>
      <p>요청 모델: {manifest.model} · 본문 크기: {manifest.body_bytes}바이트</p>
      <p>네트워크 작업별 대기 제한: {manifest.timeout_seconds}초. 전체 실행 시간이나 비용 상한은 아닙니다.</p>
      <p>리디렉션·자동 재시도·백그라운드 생성은 사용하지 않으며, API 응답 저장을 요청하지 않는 전송 조건입니다. 서비스의 별도 데이터 보존 정책까지 없어진다는 뜻은 아닙니다.</p>
      <p>이 기록은 계정 확인·실제 사용량·요금 또는 모델 실행의 독립적인 증명이 아닙니다.</p>
      <p className="local-report-id">본문 SHA-256: {manifest.body_sha256}</p>
    </> : manifest.schema_version === "ANTHROPIC_JOURNAL_WIRE_V1" ? <>
      <h5>Anthropic Messages API 전송 계획</h5>
      <p>Claude 구독 로그인과 별도인 API 키 경로입니다. 이 기록은 실제 계정·요금·모델 실행의 독립적인 증명이 아닙니다.</p>
      <p className="local-report-id">요청 주소: {manifest.endpoint}</p>
      <p>요청 모델: {manifest.model} · API 버전: {manifest.api_version} · 본문 크기: {manifest.body_bytes}바이트</p>
      <p>네트워크 작업별 대기 제한: {manifest.timeout_seconds}초. 전체 실행 시간이나 비용 상한은 아닙니다.</p>
      <p>도구를 제공하지 않으며 스트리밍·자동 재시도·리디렉션·환경 프록시를 사용하지 않습니다.</p>
      <p>Messages 출력 토큰 상한은 요청값이며 실제 강제 여부는 확인되지 않았습니다. 전체 사용량·비용 상한을 보장하지 않습니다.</p>
      <p>고정 입력의 온도는 전송하지 않으며 Provider 기본값을 사용합니다.</p>
      <p>출력 규격을 Messages 형식으로 변환하고 응답은 원래 규격으로 로컬 검증합니다. 요청 metadata는 로컬 기록에만 보존하며 전송하지 않습니다.</p>
      <p>HTTP 응답 읽기 한도: {manifest.max_response_bytes}바이트 · Journal JSON 한도: {manifest.max_journal_output_bytes}바이트</p>
      <p>assistant content text JSON을 개행·문자 그대로 보존합니다. HTTP 응답 전체를 보존하지 않습니다.</p>
      <p className="local-report-id">본문 SHA-256: {manifest.body_sha256}</p>
      <p className="local-report-id">로컬 출력 규격 SHA-256: {manifest.local_schema_sha256}</p>
      <p className="local-report-id">전송 출력 규격 SHA-256: {manifest.wire_schema_sha256}</p>
    </> : manifest.schema_version === "XAI_JOURNAL_WIRE_V1" ? <>
      <h5>xAI API 전송 계획</h5>
      <p>Grok 구독 로그인과 별도인 API 키 경로입니다. 이 기록은 실제 계정·요금·모델 실행의 독립적인 증명이 아닙니다.</p>
      <p className="local-report-id">요청 주소: {manifest.endpoint}</p>
      <p>요청 모델: {manifest.model} · 본문 크기: {manifest.body_bytes}바이트</p>
      <p>네트워크 작업별 대기 제한: {manifest.timeout_seconds}초. 전체 실행 시간이나 비용 상한은 아닙니다.</p>
      <p>도구를 제공하지 않으며 자동 재시도·리디렉션·환경 프록시를 사용하지 않습니다.</p>
      <p>API 응답 저장을 요청하지 않습니다. 서비스의 별도 데이터 보존 정책까지 없어진다는 뜻은 아닙니다.</p>
      <p>출력 토큰 제한은 보이는 출력에 대한 요청값으로, 추론 토큰을 포함한 전체 사용량·비용 상한이 아닙니다. 출력 토큰 상한·온도의 실제 강제 여부는 확인되지 않았습니다.</p>
      <p>입력 잘림 방지는 보장되지 않습니다. 요청 metadata는 로컬 기록에만 보존하며 전송하지 않습니다.</p>
      <p className="local-report-id">본문 SHA-256: {manifest.body_sha256}</p>
    </> : manifest.schema_version === "GROK_JOURNAL_ACP_V1" ? <>
      <h5>Grok ACP 실행 계획</h5>
      <p>요청 모델: {manifest.model} · 단일 입력 턴 · ACP 모델 선택 확인</p>
      <p>문헌 요청 크기: {manifest.prompt_bytes}바이트</p>
      <p className="local-report-id">문헌 요청 SHA-256: {manifest.prompt_sha256}</p>
      <p className="local-report-id">출력 규격 SHA-256: {manifest.schema_sha256}</p>
      <p className="local-report-id">실행 지침 SHA-256: {manifest.agent_sha256}</p>
      <p>공식 CLI가 기존 로그인 정보를 사용하며 실제 계정·구독 과금 경로·요청 모델 사용 여부는 확인되지 않았습니다.</p>
      <p>입력·출력 토큰 사용량은 이 ACP 경로에서 보고되지 않습니다. 문맥 사용량과 누적 비용을 입출력 토큰으로 바꾸지 않습니다.</p>
      <p>요청과 연결된 종료 응답으로 완료를 확인합니다. 프로세스 종료나 무응답 시간을 성공으로 취급하지 않습니다.</p>
      <p>CLI 프로세스 대기 제한: {manifest.timeout_seconds}초. 로컬 프로세스 정리는 외부 작업의 실제 중단이나 비용 상한을 보장하지 않습니다.</p>
      <p>XRML은 프로세스를 자동 재실행하지 않습니다. CLI 내부 재시도·외부 호출 횟수·총비용은 제한을 보장하지 않습니다.</p>
      <p>CLI 내부 도구의 완전 차단은 보장되지 않습니다. 전역 설정·MCP·훅과 Windows 격리는 검증되지 않았으며 예상 밖 출력을 거부해도 이미 발생한 부수 효과를 되돌리지는 않습니다.</p>
      <p>CLI 프로필에 대화가 별도로 저장될 수 있습니다. XRML의 결과 보존 범위와 별개입니다.</p>
      <p>요청 출력 토큰 상한·온도는 CLI에서 강제되지 않습니다. 출력 바이트 한도는 토큰·비용 상한과 다릅니다.</p>
      <p>출력 저장 한도 — 표준 출력 수신: {manifest.max_stdout_bytes}바이트 · 오류 출력 수신: {manifest.max_stderr_bytes}바이트 · Journal JSON: {manifest.max_journal_output_bytes}바이트</p>
      <p>ACP 응답 문장에서 추출한 JSON을 개행·문자 그대로 보존합니다. 생각 조각과 전체 ACP 대화 로그는 보존되지 않습니다.</p>
    </> : manifest.schema_version === "ANTIGRAVITY_JOURNAL_CLI_V1" ? <>
      <h5>Antigravity 실행 계획</h5>
      <p>요청 모델: {manifest.model} · 단일 입력 턴 · 요청 추론 수준: {manifest.reasoning_effort}</p>
      <p>표준 입력 크기: {manifest.stdin_bytes}바이트</p>
      <p className="local-report-id">표준 입력 SHA-256: {manifest.stdin_sha256}</p>
      <p className="local-report-id">출력 규격 SHA-256: {manifest.schema_sha256}</p>
      <p className="local-report-id">실행 지침 SHA-256: {manifest.agent_sha256}</p>
      <p>공식 CLI가 인증을 관리하며 실제 계정·구독 과금 경로·요청 모델 사용 여부는 확인되지 않았습니다.</p>
      <p>CLI 프로세스 대기 제한: {manifest.timeout_seconds}초. 외부 작업의 실제 중단이나 비용 상한을 보장하지 않습니다.</p>
      <p>XRML은 프로세스를 자동 재실행하지 않습니다. CLI 내부 재시도·외부 호출 횟수·총비용은 제한을 보장하지 않습니다.</p>
      <p>도구 제한은 요청한 설정이며 전역 설정·MCP·훅과 Windows 격리는 검증되지 않았습니다. 예상 밖 출력을 거부해도 이미 발생한 부수 효과를 되돌리지는 않습니다.</p>
      <p>요청 출력 토큰 상한·온도는 CLI에서 강제되지 않습니다. 출력 바이트 한도는 토큰·비용 상한과 다릅니다.</p>
      <p>출력 저장 한도 — 표준 출력 수신: {manifest.max_stdout_bytes}바이트 · 오류 출력 수신: {manifest.max_stderr_bytes}바이트 · Journal JSON: {manifest.max_journal_output_bytes}바이트</p>
      <p>최종 결과 응답의 JSON을 받은 그대로 보존합니다. 중간 문장 조각과 전체 NDJSON 출력 로그는 보존되지 않습니다.</p>
    </> : manifest.schema_version === "CLAUDE_JOURNAL_CLI_V1" ? <>
      <h5>Claude Code 실행 계획</h5>
      <p>요청 모델: {manifest.model} · 표준 입력 크기: {manifest.stdin_bytes}바이트</p>
      <p className="local-report-id">표준 입력 SHA-256: {manifest.stdin_sha256}</p>
      <p className="local-report-id">출력 규격 SHA-256: {manifest.schema_sha256}</p>
      <p>공식 CLI가 인증을 관리하며 실제 계정·구독 과금 경로·요청 모델 사용 여부는 확인되지 않았습니다.</p>
      <p>CLI 프로세스 대기 제한: {manifest.timeout_seconds}초. 외부 작업의 실제 중단이나 비용 상한을 보장하지 않습니다.</p>
      <p>XRML은 프로세스를 자동 재실행하지 않습니다. CLI 내부 재시도·외부 호출 횟수·총비용은 제한을 보장하지 않습니다.</p>
      <p>도구 차단은 요청한 설정이며 관리자 설정의 훅 격리와 실제 실행 환경은 검증되지 않았습니다.</p>
      <p>요청 출력 토큰 상한·온도는 CLI에서 강제되지 않습니다. 출력 바이트 한도는 토큰·비용 상한과 다릅니다.</p>
      <p>출력 저장 한도 — 표준 출력 수신: {manifest.max_stdout_bytes}바이트 · 오류 출력 수신: {manifest.max_stderr_bytes}바이트 · Journal JSON: {manifest.max_journal_output_bytes}바이트</p>
      <p>저장된 원고는 구조화된 결과에서 추출한 JSON입니다. CLI 표준 출력 원문은 이 Journal 결과에 보존되지 않습니다.</p>
    </> : manifest.schema_version === "CODEX_JOURNAL_CLI_V2" ? <>
      <h5>Codex CLI V2 실행 계획</h5>
      <p>공식 CLI가 관리하는 ChatGPT 로그인을 요구하는 프로필입니다. 실제 로그인·계정·구독 과금 경로는 확인되지 않았습니다.</p>
      <p>요청 모델: {manifest.model} · 요청 추론 수준: {manifest.reasoning_effort}</p>
      <p>요청 프로필: xrml_openai · Responses · WebSocket 사용 안 함 요청</p>
      <p>요청 재시도 0회 · 스트림 재시도 0회. XRML은 프로세스를 자동 재실행하지 않습니다. 실제 내부 재시도·외부 호출 횟수·비용 상한은 검증되지 않았습니다.</p>
      <p>요청 주소와 인증은 공식 CLI가 결정합니다. 이 계획은 실제 접속 주소나 인증 성공의 증명이 아닙니다.</p>
      <p className="local-report-id">요청 계약 원문 SHA-256: {manifest.contract_sha256}</p>
      <p>표준 입력 크기: {manifest.stdin_bytes}바이트</p>
      <p className="local-report-id">표준 입력 SHA-256: {manifest.stdin_sha256}</p>
      <p className="local-report-id">출력 규격 SHA-256: {manifest.schema_sha256}</p>
      <p>CLI 프로세스 대기 제한: {manifest.timeout_seconds}초. 외부 작업의 실제 중단이나 비용 상한을 보장하지 않습니다.</p>
      <p>출력 저장 한도 — 표준 출력: {manifest.max_stdout_bytes}바이트 · 오류 출력: {manifest.max_stderr_bytes}바이트 · 최종 응답: {manifest.max_final_bytes}바이트</p>
      <p>{context === "preview" ? "현재 서버 조건으로 계산한 미리보기이며 실행 이력에 저장된 계획이 아닙니다." : "저장된 실행 계획이며 실제 계정·모델 호출을 검증한 기록이 아닙니다."}</p>
      <p>요청 출력 토큰 상한·온도는 CLI에서 강제되지 않습니다. 출력 바이트 한도는 토큰·비용 상한과 다릅니다.</p>
      <p>도구 격리와 요청 모델의 실제 사용 여부는 독립적으로 확인되지 않았습니다.</p>
      <p>최종 응답 JSON을 개행·문자 그대로 보존합니다. 전체 CLI 출력 로그는 Journal 결과에 보존되지 않습니다.</p>
    </> : <>
      <h5>Codex CLI 실행 계획</h5>
      <p>요청 모델: {manifest.model} · 요청 추론 수준: {manifest.reasoning_effort}</p>
      <p>표준 입력 크기: {manifest.stdin_bytes}바이트</p>
      <p className="local-report-id">표준 입력 SHA-256: {manifest.stdin_sha256}</p>
      <p className="local-report-id">출력 규격 SHA-256: {manifest.schema_sha256}</p>
      <p>CLI 프로세스 대기 제한: {manifest.timeout_seconds}초. 외부 작업의 실제 중단이나 비용 상한을 보장하지 않습니다.</p>
      <p>출력 저장 한도 — 표준 출력: {manifest.max_stdout_bytes}바이트 · 오류 출력: {manifest.max_stderr_bytes}바이트 · 최종 응답: {manifest.max_final_bytes}바이트</p>
      <p>자동 재시도: 사용하지 않음. {context === "preview" ? "현재 서버 조건으로 계산한 미리보기이며 실행 이력에 저장된 계획이 아닙니다." : "저장된 실행 계획이며 실제 계정·모델 호출을 검증한 기록이 아닙니다."}</p>
      <p>요청 출력 토큰 상한·온도는 CLI에서 강제되지 않습니다. 출력 바이트 한도는 토큰·비용 상한과 다릅니다.</p>
      <p>도구 격리는 요청한 설정이며 실제 실행에서 검증되지 않았습니다.</p>
      <p>요청 모델의 실제 사용 여부는 독립적으로 확인되지 않았습니다.</p>
    </>}
  </details>;
}
