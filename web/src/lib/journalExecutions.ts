export const executionStates = { READY: "입력 준비 기록", LEASED: "호출 선점 기록", RUNNING: "응답 대기 기록", SUCCEEDED: "응답 저장 완료", FAILED: "실패", INTERRUPTED: "응답 확인 불가", CANCELLED: "결과 저장 취소" };
export type ExecutionState = keyof typeof executionStates;
export const executionReasons: Record<string, string> = {
  provider_unavailable: "실행 도구 사용 불가", provider_refused: "도구의 요청 거절", provider_error: "도구 응답 오류", adapter_error: "어댑터 오류",
  provider_incomplete: "도구 응답이 미완료로 종료됨", provider_failed: "도구가 생성 실패를 보고함", provider_cancelled: "도구가 생성 취소를 보고함",
  output_invalid: "출력 형식·입력·인용 검증 실패", lease_expired_before_dispatch: "호출 전 결과 접수 기한 만료", lease_expired_after_dispatch: "호출 후 결과 접수 기한 만료", result_acceptance_cancelled: "결과 저장을 취소함",
  worker_stopped_before_dispatch: "호출 전에 실행 처리가 종료됨", worker_stopped_after_dispatch: "호출 후 실행 처리가 종료되어 응답 확인 불가",
  worker_failed_before_dispatch: "호출 전 실행 처리 오류", worker_failed_after_dispatch: "호출 후 실행 처리 오류로 응답 확인 불가",
  dispatch_authority_expired: "호출 전에 실행 허용 기한이 만료됨",
  dispatch_proof_invalidated: "호출 전에 출력 형식 증명이 만료되거나 철회됨",
  dispatch_authority_revoked_before_dispatch: "호출 전에 실행 허용이 철회됨",
  dispatch_authority_revoked_after_dispatch: "호출 선점 후 실행 허용이 철회되어 응답 확인 불가",
};
export type ExecutionControl = { request_id: string; action: "cancel" | "recover"; expected_sequence: number };
export type OpenAIJournalManifest = {
  schema_version: "OPENAI_JOURNAL_WIRE_V1"; endpoint: string; model: string;
  body_bytes: number; body_sha256: string; timeout_seconds: number;
};
export type CodexJournalManifest = {
  schema_version: "CODEX_JOURNAL_CLI_V1"; contract_digest: string; model: string; reasoning_effort: string;
  runtime_identity: { launcher_sha256: string; environment_sha256: string; workspace_sha256: string };
  argv_shape: string[]; stdin_sha256: string; stdin_bytes: number; schema_sha256: string;
  timeout_seconds: number; max_stdout_bytes: number; max_stderr_bytes: number; max_final_bytes: number;
  automatic_retry: false; output_token_limit_enforced: false; temperature_enforced: false;
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED"; model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED";
};
export type CodexJournalProfile = "CODEX_JOURNAL_CLI_V1" | "CODEX_JOURNAL_CLI_V2";
export type CodexV2Policy = {
  runtime_profile: "CODEX_JOURNAL_CLI_V2"; model_provider: "xrml_openai"; requires_openai_auth: true;
  wire_api: "responses"; forced_login_method: "chatgpt"; request_max_retries: 0; stream_max_retries: 0; supports_websockets: false;
  authentication: "OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED"; endpoint_resolution: "OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED";
  retry_policy: "REQUESTED_ZERO_NOT_LIVE_VERIFIED"; xrml_process_relaunch: false; remote_call_count_enforced: false;
  cost_limit_enforced: false; live_execution: "NOT_VERIFIED"; billing_route: "CHATGPT_ACCOUNT_NOT_VERIFIED";
  raw_output_encoding: "LAST_MESSAGE_JSON_UTF8_V1"; raw_envelope_retained: false;
};
export type CodexJournalV2Manifest = Omit<CodexJournalManifest, "schema_version"> & CodexV2Policy & {
  schema_version: "CODEX_JOURNAL_CLI_V2"; adapter_id: "xrml-codex-frozen-journal"; adapter_version: "2";
  contract_sha256: string; request_sha256: string;
};
export type XaiJournalManifest = {
  schema_version: "XAI_JOURNAL_WIRE_V1"; endpoint: "https://api.x.ai/v1/responses"; model: string;
  body_bytes: number; body_sha256: string; timeout_seconds: number;
  store: false; stream: false; tools: []; redirects: false; environment_proxy: false; automatic_retry: false;
  schema_compiler: "XAI_NONBLANK_V1"; output_token_limit_enforced: false; temperature_enforced: false;
  output_token_scope: "VISIBLE_OUTPUT_ONLY"; truncation_enforced: false; cost_limit_enforced: false;
  request_metadata: "LOCAL_BINDING_ONLY_NOT_TRANSMITTED"; model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED";
};
export type AnthropicJournalManifest = {
  schema_version: "ANTHROPIC_JOURNAL_WIRE_V1"; adapter_id: "xrml-anthropic-frozen-journal"; adapter_version: "1";
  provider_name: "anthropic"; method: "POST"; endpoint: "https://api.anthropic.com/v1/messages"; api_version: "2023-06-01"; model: string;
  contract_digest: string; request_sha256: string; body_sha256: string; body_bytes: number; timeout_seconds: number;
  max_response_bytes: 4000000; max_journal_output_bytes: 512000;
  schema_compiler: "ANTHROPIC_JSON_SCHEMA_V1"; schema_constraints: "LOCAL_ORIGINAL_VALIDATION";
  local_schema_sha256: string; wire_schema_sha256: string;
  stream: false; tools: []; automatic_retry: false; redirects: false; environment_proxy: false;
  output_token_limit_enforced: false; temperature_enforced: false; cost_limit_enforced: false;
  temperature_policy: "OMITTED_PROVIDER_DEFAULT";
  request_metadata: "LOCAL_BINDING_ONLY_NOT_TRANSMITTED"; output_token_scope: "MESSAGES_OUTPUT_TOKENS";
  token_limit_policy: "REQUESTED_NOT_LIVE_VERIFIED"; model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED";
  live_execution: "NOT_VERIFIED"; billing_route: "API_KEY_ACCOUNT_NOT_VERIFIED";
  raw_output_encoding: "CONTENT_TEXT_JSON_UTF8_V1"; raw_envelope_retained: false;
  usage_input_semantics: "NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE"; total_tokens: "NOT_REPORTED_NOT_DERIVED";
};
export type ClaudeJournalManifest = {
  schema_version: "CLAUDE_JOURNAL_CLI_V1"; contract_digest: string; model: string;
  runtime_identity: { launcher_sha256: string; environment_sha256: string; workspace_sha256: string; config_dir_sha256: string };
  argv_shape: string[]; stdin_sha256: string; stdin_bytes: number; schema_sha256: string; system_prompt_sha256: string;
  timeout_seconds: number; max_stdout_bytes: number; max_stderr_bytes: number; max_journal_output_bytes: number;
  automatic_retry: false; xrml_process_relaunch: false; remote_call_count_enforced: false;
  internal_retries: "CLI_MANAGED_NOT_LIVE_VERIFIED"; managed_hooks: "NOT_ISOLATED_NOT_LIVE_VERIFIED";
  output_token_limit_enforced: false; temperature_enforced: false; cost_limit_enforced: false;
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED"; model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED";
  authentication: "OFFICIAL_CLI_OWNED_NOT_VERIFIED"; billing_route: "NOT_VERIFIED";
  raw_journal_encoding: "EXTRACTED_CANONICAL_JSON_V1"; journal_result_storage: "EXTRACTED_STRUCTURED_OUTPUT_ONLY"; raw_envelope_retained: false;
};
export type AntigravityJournalManifest = Omit<ClaudeJournalManifest, "schema_version" | "runtime_identity" | "system_prompt_sha256" | "tool_isolation" | "raw_journal_encoding" | "journal_result_storage"> & {
  schema_version: "ANTIGRAVITY_JOURNAL_CLI_V1";
  runtime_identity: { launcher_sha256: string; environment_sha256: string; workspace_sha256: string };
  input_turns: 1; reasoning_effort: "low"; agent_name: "xrml-paper-review-v1"; agent_sha256: string;
  global_config_and_mcp: "NOT_ISOLATED_NOT_LIVE_VERIFIED"; windows_sandbox: "REQUESTED_NOT_LIVE_VERIFIED";
  tool_isolation: "REQUESTED_AND_OUTPUT_CHECKED_NOT_LIVE_VERIFIED"; output_rejection_prevents_side_effects: false;
  raw_journal_encoding: "RESULT_RESPONSE_JSON_UTF8_V1"; journal_result_storage: "RESULT_RESPONSE_ONLY";
  step_text: "NONCANONICAL_STREAM_TELEMETRY";
};
export type GrokJournalManifest = Pick<ClaudeJournalManifest, "contract_digest" | "model" | "argv_shape" | "schema_sha256" | "timeout_seconds" | "max_stdout_bytes" | "max_stderr_bytes" | "max_journal_output_bytes" | "automatic_retry" | "xrml_process_relaunch" | "remote_call_count_enforced" | "internal_retries" | "managed_hooks" | "output_token_limit_enforced" | "temperature_enforced" | "cost_limit_enforced" | "model_identity" | "billing_route" | "raw_envelope_retained"> & {
  schema_version: "GROK_JOURNAL_ACP_V1"; runtime_identity: AntigravityJournalManifest["runtime_identity"];
  prompt_sha256: string; prompt_bytes: number; agent_sha256: string; protocol_version: 1; prompt_count: 1; max_events: number;
  client_capabilities: Record<string, never>; authentication: "OFFICIAL_CLI_CACHED_TOKEN_ONLY_NOT_VERIFIED";
  session_retention: "CLI_MANAGED_PROFILE_STORAGE_NOT_ISOLATED"; model_selection: "ACP_CONFIG_OPTION_MODEL_EXACT";
  structured_output: "PROMPT_REQUESTED_LOCAL_VALIDATION_ONLY"; completion: "CORRELATED_PROMPT_STOP_REASON";
  process_cleanup: "HOST_TERMINATES_OWNED_ACP_CHILD_AFTER_TERMINAL"; global_config_and_mcp: "NOT_ISOLATED_NOT_LIVE_VERIFIED";
  tool_isolation: "NOT_ESTABLISHED"; output_rejection_prevents_side_effects: false; physical_remote_cancellation: "NOT_ESTABLISHED";
  token_usage: "NOT_REPORTED_BY_SUPPORTED_ACP_PROFILE"; raw_journal_encoding: "ACP_AGENT_MESSAGE_TEXT_UTF8_V1";
  journal_result_storage: "AGENT_MESSAGE_TEXT_ONLY";
};
export type JournalAdapterManifest = OpenAIJournalManifest | CodexJournalManifest | CodexJournalV2Manifest | XaiJournalManifest | AnthropicJournalManifest | ClaudeJournalManifest | AntigravityJournalManifest | GrokJournalManifest;
export const reportedJournalModel = (model: string) => model === "not_reported" ? "실제 보고 모델 미확인" : model;
export type ExecutionEvent = {
  sequence: number; state: ExecutionState; attempt: number; at: string | null; deadline: string | null;
  outcome_unknown: boolean; reason: string | null;
  result?: null | { provider_reported: { provider_name: string; model: string; response_id: string | null; usage: Record<string, unknown> } };
  control?: ExecutionControl & { schema_version: "JOURNAL_EXECUTION_CONTROL_V1"; actor_id: string };
  authority_revocation?: { authorization_id: string; actor_id: string; reason: string; at: string };
  adapter_manifest?: JournalAdapterManifest;
  dispatch?: { schema_version: "JOURNAL_DISPATCH_RECEIPT_V1" | "JOURNAL_DISPATCH_RECEIPT_V2"; actor_id: string; reserved_output_tokens: number;
    authority?: { authorization_id: string; record_digest: string };
    grant: { authorization_id: string; expires_at: string; max_attempts: number; max_reserved_output_tokens: number } };
};
export type ExecutionDetail = {
  contract: { execution_id: string; actor_id: string; created_at: string;
    provider_request?: { instructions: string; prompt: string; schema_name: string; schema: unknown; metadata: unknown; temperature: number; max_output_tokens: number };
    packet: { packet_id: string }; specification: { provider_name: string; model: string; max_attempts: number; max_output_tokens: number; expected_input_digest: string } };
  current: ExecutionEvent; events: ExecutionEvent[];
};

export type OfflineCapability = {
  enabled: boolean; mode: "OFFLINE_SYNTHETIC"; provider_name: string; model: string; reason: string | null;
};
export type OfflineAuthority = {
  record_digest: string; status: "AVAILABLE" | "EXPIRED" | "REVOKED";
  revocation: null | { actor_id: string; at: string; reason: string };
  record: { registered_by: string; registered_at: string; decision_ref: string;
    grant: { authorization_id: string; expires_at: string; max_attempts: number; max_reserved_output_tokens: number } };
};
export const offlineJournalTarget = { provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1" };
export const isOfflineJournal = (provider: string, model: string) => provider === offlineJournalTarget.provider_name && model === offlineJournalTarget.model;

export type CodexDispatchRequest = {
  request_id: string; expected_sequence: number; acknowledge_unknown: boolean;
  authorization_id: string; expected_authority_digest: string; expected_binding_digest: string;
};
export type CodexDispatchAuthority = {
  authorization_id: string; record_digest: string; status: "AVAILABLE" | "EXPIRED" | "REVOKED" | "MISMATCH" | "BUDGET_EXHAUSTED";
  expires_at: string; issuer: string; policy_version: string;
};
export type CodexDispatchStatus = {
  schema_version: "CODEX_JOURNAL_DISPATCH_STATUS_V1"; enabled: boolean; reason: string | null;
  profile: "CODEX_JOURNAL_CLI_V2"; execution_id: string; actor_id: string; contract_digest: string; observed_sequence: number;
  binding: { surface_id: "codex_cli"; adapter_id: "xrml-codex-frozen-journal"; adapter_version: "2"; command_shape_sha256: string } | null;
  manifest: CodexJournalV2Manifest | null; authorities: CodexDispatchAuthority[]; authorities_has_more: boolean;
};
