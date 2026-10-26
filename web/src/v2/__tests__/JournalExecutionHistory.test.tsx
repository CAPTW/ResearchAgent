import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ExecutionDetail } from "../../lib/journalExecutions";
import { JournalExecutionHistory } from "../views/JournalExecutionHistory";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const event = { sequence: 2, state: "RUNNING" as const, attempt: 1, at: "2026-09-27T00:00:00Z", deadline: "2026-09-27T00:02:00Z", outcome_unknown: false, reason: null };
const saved: ExecutionDetail = { contract: { execution_id: "exec", actor_id: "requester", created_at: event.at, packet: { packet_id: "packet" }, specification: { provider_name: "test", model: "requested-model", max_attempts: 3, max_output_tokens: 4000, expected_input_digest: "frozen-input" } }, current: event, events: [{ ...event, state: "LEASED", sequence: 1 }, event] };
const url = "/api/topics/topic/local-synthesis/job/journal-executions/exec";
const changed = vi.fn();
beforeEach(() => vi.resetAllMocks());
function cancelled() {
  const current = { ...event, sequence: 3, state: "CANCELLED" as const, outcome_unknown: true, reason: "result_acceptance_cancelled", control: { schema_version: "JOURNAL_EXECUTION_CONTROL_V1" as const, actor_id: "server-user", action: "cancel" as const, request_id: "request", expected_sequence: 2 } };
  return { ...saved, current, events: [...saved.events, current] };
}

it.each([
  ["dispatch_authority_expired", "호출 전에 실행 허용 기한이 만료됨"],
  ["dispatch_proof_invalidated", "호출 전에 출력 형식 증명이 만료되거나 철회됨"],
])("shows dispatch actor, capacity and %s separately from actual usage", async (reason, label) => {
  const dispatch = { schema_version: "JOURNAL_DISPATCH_RECEIPT_V1" as const, actor_id: "dispatcher-user", reserved_output_tokens: 4000,
    grant: { authorization_id: "approval-record", expires_at: event.deadline, max_attempts: 2, max_reserved_output_tokens: 8000 } };
  const current = { ...event, state: "FAILED" as const, reason };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, current, events: [{ ...saved.events[0], dispatch }, current] });
  await open();
  expect(screen.getByText("실행 요청 사용자: dispatcher-user")).toBeTruthy();
  expect(screen.getAllByText(/사용자 조작 정보 없음/)).toHaveLength(1);
  fireEvent.click(screen.getByText("실행 허용·예약 기록"));
  expect(screen.getByText("이 시도의 출력 토큰 예약 상한: 4000")).toBeTruthy();
  expect(screen.getByText(/실제 토큰 사용량·요금·모델 실행 증명이 아닙니다/)).toBeTruthy();
  expect(screen.getByText(label)).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});
async function open() {
  const view = render(<JournalExecutionHistory url={url} onChanged={changed}/>);
  await screen.findByText(/최근 저장 상태:/);
  return view;
}

it("offers a current Codex preview separately from history without fetching it automatically", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, contract: { ...saved.contract, specification: { ...saved.contract.specification, provider_name: "codex_cli" } } });
  await open();
  expect(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "현재 Codex 실행 조건 미리보기" })).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
  expect(screen.queryByText("Codex CLI 실행 계획")).toBeNull();
});

it("removes the current Codex preview when history refresh fails", async () => {
  const value = { ...saved, contract: { ...saved.contract, specification: { ...saved.contract.specification, provider_name: "codex_cli" } } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(value).mockResolvedValueOnce({
    schema_version: "CODEX_JOURNAL_PREVIEW_V1", preview_only: true, execution_allowed: false,
    execution_id: "exec", contract_digest: "a".repeat(64), actor_id: "requester", observed_sequence: event.sequence,
    available: false, reason: "codex_preview_not_configured", manifest: null, binding: null, preview_digest: null,
  }).mockRejectedValueOnce(new Error("history offline"));
  await open();
  fireEvent.click(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" }));
  await screen.findByText(/서버의 Codex 미리보기 실행 환경이 설정되지 않았습니다/);
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/history offline/);
  expect(screen.queryByRole("region", { name: "현재 Codex 실행 조건 미리보기" })).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.map(([path]) => path)).toEqual([url, `${url}/codex-preview`, url]);
});

it("reads preserved inputs without another request and hides them if history refresh fails", async () => {
  const value = { ...saved, contract: { ...saved.contract, provider_request: { instructions: "Preserved instructions", prompt: "Preserved private input",
    schema_name: "JOURNAL_PROPOSAL_V1", schema: {}, metadata: {}, temperature: 0.2, max_output_tokens: 4000 } } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(value).mockRejectedValueOnce(new Error("store offline"));
  await open();
  fireEvent.click(screen.getByText("요청 내용·토큰 사용 기록"));
  fireEvent.click(await screen.findByText("보존된 어댑터 입력 보기"));
  expect((await screen.findByLabelText("보존된 문헌 입력")).textContent).toBe("Preserved private input");
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/store offline/);
  expect(screen.queryByText("Preserved private input")).toBeNull();
  expect(screen.queryByText("요청 내용·토큰 사용 기록")).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("reads preserved wire conditions as inert text without dispatch or cost claims", async () => {
  const adapter_manifest = { schema_version: "OPENAI_JOURNAL_WIRE_V1", endpoint: "https://offline.invalid/v1/responses", model: "offline-model", body_bytes: 1024, timeout_seconds: 45, body_sha256: "a".repeat(64) };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText("요청 주소: https://offline.invalid/v1/responses")).toBeTruthy();
  expect(screen.getByText(/본문 크기: 1024바이트/)).toBeTruthy();
  expect(screen.getByText(/전체 실행 시간이나 비용 상한은 아닙니다/)).toBeTruthy();
  expect(screen.getByText(/서비스의 별도 데이터 보존 정책/)).toBeTruthy();
  expect(screen.queryByRole("link", { name: /offline.invalid/ })).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it("reads a Codex CLI manifest as a requested process plan without inventing OpenAI transport guarantees", async () => {
  const adapter_manifest = {
    schema_version: "CODEX_JOURNAL_CLI_V1", contract_digest: "a".repeat(64), model: "requested-codex-model", reasoning_effort: "xhigh",
    runtime_identity: { launcher_sha256: "b".repeat(64), environment_sha256: "c".repeat(64), workspace_sha256: "d".repeat(64) },
    argv_shape: ["{explicit_launcher}", "exec", "--model", "requested-codex-model"],
    stdin_sha256: "e".repeat(64), stdin_bytes: 1234, schema_sha256: "f".repeat(64), timeout_seconds: 90,
    max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_final_bytes: 512000,
    automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false,
    tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
  };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText("Codex CLI 실행 계획")).toBeTruthy();
  expect(screen.getByText("요청 모델: requested-codex-model · 요청 추론 수준: xhigh")).toBeTruthy();
  expect(screen.getByText(/표준 입력 크기: 1234바이트/)).toBeTruthy();
  expect(screen.getByText(`표준 입력 SHA-256: ${adapter_manifest.stdin_sha256}`)).toBeTruthy();
  expect(screen.getByText(`출력 규격 SHA-256: ${adapter_manifest.schema_sha256}`)).toBeTruthy();
  expect(screen.getByText(/CLI 프로세스 대기 제한: 90초/)).toBeTruthy();
  expect(screen.getByText(/표준 출력: 1048576바이트 · 오류 출력: 32768바이트 · 최종 응답: 512000바이트/)).toBeTruthy();
  expect(screen.getByText(/자동 재시도: 사용하지 않음/)).toBeTruthy();
  expect(screen.getByText(/출력 토큰 상한·온도는 CLI에서 강제되지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/도구 격리는 요청한 설정이며 실제 실행에서 검증되지 않았습니다/)).toBeTruthy();
  expect(screen.getByText(/요청 모델의 실제 사용 여부는 독립적으로 확인되지 않았습니다/)).toBeTruthy();
  expect(screen.queryByText(/요청 주소:|네트워크 작업별 대기 제한:|API 응답 저장을 요청하지 않는/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

const xaiManifest = {
  schema_version: "XAI_JOURNAL_WIRE_V1", endpoint: "https://api.x.ai/v1/responses", model: "synthetic-xai",
  body_bytes: 1200, body_sha256: "b".repeat(64), timeout_seconds: 45,
  store: false, stream: false, tools: [], redirects: false, environment_proxy: false,
  automatic_retry: false, schema_compiler: "XAI_NONBLANK_V1", output_token_limit_enforced: false,
  temperature_enforced: false, model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
  output_token_scope: "VISIBLE_OUTPUT_ONLY", truncation_enforced: false, cost_limit_enforced: false,
  request_metadata: "LOCAL_BINDING_ONLY_NOT_TRANSMITTED",
};

const claudeManifest = {
  schema_version: "CLAUDE_JOURNAL_CLI_V1", contract_digest: "a".repeat(64), model: "synthetic-claude",
  runtime_identity: { launcher_sha256: "b".repeat(64), environment_sha256: "c".repeat(64), workspace_sha256: "d".repeat(64), config_dir_sha256: "e".repeat(64) },
  argv_shape: ["{explicit_launcher}", "-p", "--safe-mode", "--tools", "", "--setting-sources", ""],
  stdin_sha256: "a".repeat(64), stdin_bytes: 1234,
  schema_sha256: "b".repeat(64), system_prompt_sha256: "c".repeat(64), timeout_seconds: 45,
  max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_journal_output_bytes: 512000,
  automatic_retry: false, xrml_process_relaunch: false, remote_call_count_enforced: false,
  internal_retries: "CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks: "NOT_ISOLATED_NOT_LIVE_VERIFIED",
  output_token_limit_enforced: false, temperature_enforced: false, cost_limit_enforced: false,
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
  authentication: "OFFICIAL_CLI_OWNED_NOT_VERIFIED", billing_route: "NOT_VERIFIED", raw_journal_encoding: "EXTRACTED_CANONICAL_JSON_V1",
  journal_result_storage: "EXTRACTED_STRUCTURED_OUTPUT_ONLY", raw_envelope_retained: false,
};

const grokManifest = {
  schema_version: "GROK_JOURNAL_ACP_V1", contract_digest: "a".repeat(64), model: "synthetic-grok",
  runtime_identity: { launcher_sha256: "a".repeat(64), environment_sha256: "b".repeat(64), workspace_sha256: "c".repeat(64) },
  argv_shape: ["{explicit_launcher}", "--no-auto-update", "agent", "--model", "synthetic-grok", "--agent-profile", "{attempt}/paper-agent.md", "--no-leader", "stdio"],
  prompt_sha256: "d".repeat(64), prompt_bytes: 1234, schema_sha256: "e".repeat(64), agent_sha256: "f".repeat(64),
  protocol_version: 1, prompt_count: 1, client_capabilities: {}, timeout_seconds: 45,
  max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_journal_output_bytes: 262144, max_events: 4096,
  authentication: "OFFICIAL_CLI_CACHED_TOKEN_ONLY_NOT_VERIFIED", billing_route: "NOT_VERIFIED",
  session_retention: "CLI_MANAGED_PROFILE_STORAGE_NOT_ISOLATED", model_selection: "ACP_CONFIG_OPTION_MODEL_EXACT",
  structured_output: "PROMPT_REQUESTED_LOCAL_VALIDATION_ONLY", completion: "CORRELATED_PROMPT_STOP_REASON",
  process_cleanup: "HOST_TERMINATES_OWNED_ACP_CHILD_AFTER_TERMINAL", automatic_retry: false, xrml_process_relaunch: false,
  remote_call_count_enforced: false, internal_retries: "CLI_MANAGED_NOT_LIVE_VERIFIED", managed_hooks: "NOT_ISOLATED_NOT_LIVE_VERIFIED",
  global_config_and_mcp: "NOT_ISOLATED_NOT_LIVE_VERIFIED", tool_isolation: "NOT_ESTABLISHED", output_rejection_prevents_side_effects: false,
  physical_remote_cancellation: "NOT_ESTABLISHED", output_token_limit_enforced: false, temperature_enforced: false, cost_limit_enforced: false,
  model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED", token_usage: "NOT_REPORTED_BY_SUPPORTED_ACP_PROFILE",
  raw_journal_encoding: "ACP_AGENT_MESSAGE_TEXT_UTF8_V1", journal_result_storage: "AGENT_MESSAGE_TEXT_ONLY", raw_envelope_retained: false,
};

it("reads Grok Journal ACP limits and exact text without claiming transcript retention", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: grokManifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText("Grok ACP 실행 계획")).toBeTruthy();
  expect(screen.getByText(/전체 ACP 대화 로그는 보존되지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/Journal JSON: 262144바이트/)).toBeTruthy();
  expect(screen.getByText(/CLI 내부 도구의 완전 차단은 보장되지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/입력·출력 토큰 사용량은 이 ACP 경로에서 보고되지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/공식 CLI가 기존 로그인 정보를 사용하며/)).toBeTruthy();
  expect(screen.getByText(/CLI 프로필에 대화가 별도로 저장될 수 있습니다/)).toBeTruthy();
  expect(screen.queryByText(/Claude Code 실행 계획|Antigravity 실행 계획|요청 주소:/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it.each([
  { client_capabilities: { fs: {} } }, { prompt_count: 2 }, { protocol_version: 2 }, { tool_isolation: "ISOLATED" },
  { raw_envelope_retained: true }, { completion: "PROCESS_EXIT" }, { raw_journal_encoding: "FULL_TRANSCRIPT" },
  { max_journal_output_bytes: 512000 }, { physical_remote_cancellation: "VERIFIED" }, { token_usage: "KNOWN_ZERO" },
])("rejects incompatible Grok Journal manifest guarantees: %j", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: { ...grokManifest, ...change } }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText(/전송 조건 형식이 지원되지 않거나 필요한 값이 없습니다/)).toBeTruthy();
  expect(screen.queryByText("Grok ACP 실행 계획")).toBeNull();
});

const antigravityManifest = {
  ...claudeManifest, schema_version: "ANTIGRAVITY_JOURNAL_CLI_V1", model: "synthetic-agy",
  runtime_identity: { launcher_sha256: "a".repeat(64), environment_sha256: "b".repeat(64), workspace_sha256: "c".repeat(64) },
  argv_shape: ["{explicit_launcher}", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"],
  input_turns: 1, reasoning_effort: "low", agent_name: "xrml-paper-review-v1", agent_sha256: "d".repeat(64),
  global_config_and_mcp: "NOT_ISOLATED_NOT_LIVE_VERIFIED", windows_sandbox: "REQUESTED_NOT_LIVE_VERIFIED",
  tool_isolation: "REQUESTED_AND_OUTPUT_CHECKED_NOT_LIVE_VERIFIED", output_rejection_prevents_side_effects: false,
  max_journal_output_bytes: 262144, raw_journal_encoding: "RESULT_RESPONSE_JSON_UTF8_V1",
  journal_result_storage: "RESULT_RESPONSE_ONLY", step_text: "NONCANONICAL_STREAM_TELEMETRY",
};

it("reads Antigravity Journal final response and effective limits without implying stream retention", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: antigravityManifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText("Antigravity 실행 계획")).toBeTruthy();
  expect(screen.getByText(/최종 결과 응답의 JSON/)).toBeTruthy();
  expect(screen.getByText(/전체 NDJSON 출력 로그는 보존되지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/Journal JSON: 262144바이트/)).toBeTruthy();
  expect(screen.getByText(/전역 설정·MCP·훅과 Windows 격리/)).toBeTruthy();
  expect(screen.getByText(/출력을 거부해도 이미 발생한 부수 효과를 되돌리지는 않습니다/)).toBeTruthy();
  expect(screen.getByText(/외부 호출 횟수·총비용/)).toBeTruthy();
  expect(screen.queryByText(/Claude Code 실행 계획|요청 주소:/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it.each([
  { global_config_and_mcp: "ISOLATED" }, { windows_sandbox: "VERIFIED" }, { input_turns: 2 },
  { output_rejection_prevents_side_effects: true }, { raw_envelope_retained: true }, { raw_journal_encoding: "ORIGINAL_STREAM" },
  { max_journal_output_bytes: 512000 }, { argv_shape: ["{explicit_launcher}", ""] },
])("does not infer Antigravity guarantees from an incompatible manifest: %j", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: { ...antigravityManifest, ...change } }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText(/전송 조건 형식이 지원되지 않거나 필요한 값이 없습니다/)).toBeTruthy();
  expect(screen.queryByText("Antigravity 실행 계획")).toBeNull();
});

it("reads Claude Journal manifest with extracted JSON and unverified CLI boundaries", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: claudeManifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText("Claude Code 실행 계획")).toBeTruthy();
  expect(screen.getByText(/공식 CLI가 인증을 관리하며/)).toBeTruthy();
  expect(screen.getByText(/관리자 설정의 훅 격리/)).toBeTruthy();
  expect(screen.getByText(/CLI 내부 재시도·외부 호출 횟수·총비용/)).toBeTruthy();
  expect(screen.getByText(/구조화된 결과에서 추출한 JSON/)).toBeTruthy();
  expect(screen.getByText(/CLI 표준 출력 원문은 이 Journal 결과에 보존되지 않습니다/)).toBeTruthy();
  expect(screen.queryByText(/요청 주소:|요청 추론 수준:/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it.each([
  { authentication: "VERIFIED" }, { managed_hooks: "ISOLATED" }, { remote_call_count_enforced: true },
  { raw_envelope_retained: true }, { raw_journal_encoding: "ORIGINAL" }, { runtime_identity: {} },
  { argv_shape: [] }, { argv_shape: [""] }, { argv_shape: ["{explicit_launcher}", "--model", ""] },
  { argv_shape: ["{explicit_launcher}", 0] },
])("does not infer Claude guarantees from an incompatible manifest: %j", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: { ...claudeManifest, ...change } }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText(/전송 조건 형식이 지원되지 않거나 필요한 값이 없습니다/)).toBeTruthy();
  expect(screen.queryByText("Claude Code 실행 계획")).toBeNull();
});

it("reads xAI API conditions separately from subscription login and unverified token limits", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: xaiManifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText("xAI API 전송 계획")).toBeTruthy();
  expect(screen.getByText("요청 주소: https://api.x.ai/v1/responses")).toBeTruthy();
  expect(screen.getByText(/Grok 구독 로그인과 별도인 API 키 경로/)).toBeTruthy();
  expect(screen.getByText(/출력 토큰 상한·온도의 실제 강제 여부는 확인되지 않았습니다/)).toBeTruthy();
  expect(screen.getByText(/서비스의 별도 데이터 보존 정책/)).toBeTruthy();
  expect(screen.getByText(/자동 재시도·리디렉션·환경 프록시를 사용하지 않습니다/)).toBeTruthy();
  expect(screen.queryByText(/백그라운드 생성은 사용하지/)).toBeNull();
  expect(screen.queryByRole("link", { name: /api.x.ai/ })).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it.each([
  { endpoint: "https://unexpected.invalid/v1/responses" }, { store: true }, { tools: ["web_search"] },
  { automatic_retry: true }, { schema_compiler: "unknown" }, { output_token_limit_enforced: true },
])("does not invent xAI guarantees for an incompatible manifest: %j", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest: { ...xaiManifest, ...change } }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText(/전송 조건 형식이 지원되지 않거나 필요한 값이 없습니다/)).toBeTruthy();
  expect(screen.queryByText("xAI API 전송 계획")).toBeNull();
});

it.each([
  { schema_version: "FUTURE_TRANSPORT_V9", endpoint: "https://must-not-interpret.invalid", model: "future" },
  { schema_version: "CODEX_JOURNAL_CLI_V1", model: "incomplete" },
  { schema_version: "OPENAI_JOURNAL_WIRE_V1", endpoint: "https://incomplete.invalid" },
])("does not reinterpret an unknown or incomplete manifest as OpenAI wire conditions: $schema_version", async adapter_manifest => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, events: [{ ...saved.events[0], adapter_manifest }, event] });
  await open(); fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByText(/전송 조건 형식이 지원되지 않거나 필요한 값이 없습니다/)).toBeTruthy();
  expect(screen.queryByText(/요청 주소:|네트워크 작업별 대기 제한:|CLI 프로세스 대기 제한:|자동 재시도: 사용하지 않음/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it.each([
  ["provider_incomplete", "도구 응답이 미완료로 종료됨"],
  ["provider_failed", "도구가 생성 실패를 보고함"],
  ["provider_cancelled", "도구가 생성 취소를 보고함"],
])("explains %s as reported non-success, not no cost or a retry", async (reason, label) => {
  const current = { ...event, sequence: 3, state: "FAILED" as const, reason };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, current, events: [saved.events[0], event, current] });
  await open();
  expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByText(/비용이 발생하지 않았다는 뜻은 아니며/)).toBeTruthy();
  expect(screen.queryByText(/외부 호출의 완료 여부는 확인되지 않았습니다/)).toBeNull();
  expect(screen.queryByRole("button", { name: /재시도 실행/ })).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
});

it("reads history without recovery/dispatch and renders fixed request, event order and unknown actor honestly", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(saved);
  await open();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(url);
  const items = within(screen.getByRole("list", { name: "실행 사건 이력" })).getAllByRole("listitem");
  expect(items.map(el => el.textContent)).toEqual([expect.stringContaining("입력 준비"), expect.stringContaining("호출 선점 기록"), expect.stringContaining("응답 대기 기록")]);
  expect(screen.getAllByText(/사용자 조작 정보 없음/)).toHaveLength(2);
  fireEvent.click(screen.getByText("고정된 실행 요청 정보"));
  expect(screen.getByText("입력: frozen-input")).toBeTruthy();
  expect(screen.getByText(/외부 호출의 중단·비용 취소를 보장하지 않습니다/)).toBeTruthy();
});

it("explicit expiry check submits observed sequence and shows server result without a new attempt", async () => {
  const current = { ...event, sequence: 3, state: "INTERRUPTED" as const, reason: "lease_expired_after_dispatch", outcome_unknown: true };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockResolvedValueOnce({ execution: { ...saved, current, events: [...saved.events, current] }, applied: true });
  await open();
  fireEvent.click(screen.getByRole("button", { name: "접수 기한 만료 확인" }));
  await screen.findByText(/최근 저장 상태: 응답 확인 불가 · 시도 1/);
  const body = JSON.parse(vi.mocked(api.fetchJson).mock.calls[1][1]!.body as string);
  expect(body).toEqual({ action: "recover", expected_sequence: 2, request_id: expect.any(String) });
  expect(body.request_id.length).toBeGreaterThan(10);
  expect(changed).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: "접수 기한 만료 확인" })).toBeNull();
  expect(screen.queryByRole("button", { name: /생성 실행|재시도 실행/ })).toBeNull();
});

it("retries a lost response with the same request and disables competing controls", async () => {
  const authority = { authorization_id: "approval", record_digest: "a".repeat(64) };
  const dispatch = { schema_version: "JOURNAL_DISPATCH_RECEIPT_V2" as const, actor_id: "owner", reserved_output_tokens: 4000, authority,
    grant: { authorization_id: authority.authorization_id, expires_at: event.deadline, max_attempts: 3, max_reserved_output_tokens: 12000 } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...saved, events: [{ ...saved.events[0], dispatch }, event] })
    .mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce({ execution: cancelled(), applied: false });
  await open();
  fireEvent.click(screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }));
  await screen.findByText(/response lost/);
  expect((screen.getByRole("button", { name: "접수 기한 만료 확인" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText("실행 허용·예약 기록"));
  expect((screen.getByRole("button", { name: "허용 출처 확인" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "같은 상태 변경 요청 다시 보내기" }));
  await screen.findByText(/이미 처리한 요청입니다/);
  expect(vi.mocked(api.fetchJson).mock.calls[1]).toEqual(vi.mocked(api.fetchJson).mock.calls[2]);
  expect(screen.getByText("결과 저장 취소 요청 사용자: server-user")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "이 요청의 결과 저장 취소" })).toBeNull();
});

it("requires an explicit fresh read after a state conflict, preserving the failed request until then", async () => {
  const newer = { ...saved, current: { ...event, sequence: 6, attempt: 2 } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockRejectedValueOnce(new Error("journal_execution_conflict"))
    .mockResolvedValueOnce(newer).mockResolvedValueOnce({ execution: cancelled(), applied: true });
  await open();
  fireEvent.click(screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }));
  await screen.findByText(/저장 상태가 바뀌어/);
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/최근 저장 상태: 응답 대기 기록 · 시도 2/);
  fireEvent.click(screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }));
  await screen.findByText(/요청한 상태 변경을 기록/);
  const bodies = vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST").map(([, o]) => JSON.parse(o!.body as string));
  expect(bodies[0].expected_sequence).toBe(2); expect(bodies[1].expected_sequence).toBe(6);
  expect(bodies[0].request_id).not.toBe(bodies[1].request_id);
});

it("does not label an unexpired attempt failed and hides controls after a refresh failure", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockRejectedValueOnce(new Error("journal_execution_not_expired"))
    .mockRejectedValueOnce(new Error("store offline")).mockResolvedValueOnce(saved);
  await open();
  fireEvent.click(screen.getByRole("button", { name: "접수 기한 만료 확인" }));
  await screen.findByText(/서버 기준으로 결과 접수 기한이 아직 남아/);
  expect(screen.getByText(/최근 저장 상태: 응답 대기 기록/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/store offline/);
  expect(screen.queryByRole("button", { name: "이 요청의 결과 저장 취소" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByRole("button", { name: "이 요청의 결과 저장 취소" });
  expect(screen.queryByText(/store offline/)).toBeNull();
});

it("locks duplicate clicks and ignores late mutation responses after the selected execution changes", async () => {
  let resolve!:(value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockImplementationOnce(() => new Promise(r => { resolve = r; }))
    .mockResolvedValueOnce({ ...saved, contract: { ...saved.contract, execution_id: "other" }, current: { ...event, state: "SUCCEEDED" } });
  const view = render(<JournalExecutionHistory key="exec" url={url} onChanged={changed}/>);
  await screen.findByRole("button", { name: "이 요청의 결과 저장 취소" });
  const cancel = screen.getByRole("button", { name: "이 요청의 결과 저장 취소" });
  fireEvent.click(cancel); fireEvent.click(cancel);
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  view.rerender(<JournalExecutionHistory key="other" url={url + "-other"} onChanged={changed}/>);
  await screen.findByText(/최근 저장 상태: 응답 저장 완료/);
  await act(async () => resolve({ execution: cancelled(), applied: true }));
  await waitFor(() => expect(changed).not.toHaveBeenCalled());
  expect(screen.queryByText(/최근 저장 상태: 결과 저장 취소/)).toBeNull();
});

it("distinguishes worker shutdown before dispatch from an unknown outcome after dispatch", async () => {
  const current = { ...event, state: "INTERRUPTED" as const, reason: "worker_stopped_after_dispatch", outcome_unknown: true };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...saved, current, events: [
    { ...event, sequence: 1, state: "FAILED", reason: "worker_stopped_before_dispatch" },
    { ...event, sequence: 2, state: "FAILED", reason: "worker_failed_before_dispatch" },
    { ...event, sequence: 3, state: "INTERRUPTED", reason: "worker_failed_after_dispatch", outcome_unknown: true },
    { ...current, sequence: 4 },
  ] });
  await open();
  for (const label of ["호출 전에 실행 처리가 종료됨", "호출 후 실행 처리가 종료되어 응답 확인 불가", "호출 전 실행 처리 오류", "호출 후 실행 처리 오류로 응답 확인 불가"]) expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByText(/외부 호출의 완료 여부는 확인되지 않았습니다/)).toBeTruthy();
  expect(screen.queryByText(/상세 사유를 확인할 수 없습니다/)).toBeNull();
});

it("withdraws an observed authority and updates history without unmounting its source or dispatching", async () => {
  const authority = { authorization_id: "observed-approval", record_digest: "a".repeat(64) };
  const dispatch = { schema_version: "JOURNAL_DISPATCH_RECEIPT_V2" as const, actor_id: "owner", reserved_output_tokens: 4000, authority,
    grant: { authorization_id: authority.authorization_id, expires_at: event.deadline, max_attempts: 3, max_reserved_output_tokens: 12000 } };
  const value = { ...saved, events: [{ ...saved.events[0], dispatch }, event] };
  const revocation = { authorization_id: authority.authorization_id, actor_id: "server-user", at: event.at, reason: "Stop this allowance" };
  const current = { ...event, state: "INTERRUPTED" as const, sequence: 3, outcome_unknown: true, authority_revocation: revocation,
    reason: "dispatch_authority_revoked_after_dispatch" };
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(value).mockResolvedValueOnce({ record: { registered_by: "owner", registered_at: event.at, decision_ref: "decision" }, revocation: null })
    .mockImplementationOnce(() => new Promise(r => { finish = r; }));
  await open();
  fireEvent.click(screen.getByText("실행 허용·예약 기록"));
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  fireEvent.change(await screen.findByRole("textbox"), { target: { value: revocation.reason } });
  const withdraw = screen.getByRole("button", { name: "이 실행 허용 철회" });
  fireEvent.click(withdraw); fireEvent.click(withdraw);
  expect(api.fetchJson).toHaveBeenCalledTimes(3);
  expect((screen.getByRole("button", { name: "실행 이력 새로고침" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }) as HTMLButtonElement).disabled).toBe(true);
  const [, options] = vi.mocked(api.fetchJson).mock.calls[2];
  expect(vi.mocked(api.fetchJson).mock.calls[2][0]).toBe(`${url}/authority/revoke`);
  expect(JSON.parse(options!.body as string)).toEqual({ authorization_id: authority.authorization_id, expected_record_digest: authority.record_digest,
    reason: revocation.reason, request_id: expect.any(String) });
  await act(async () => finish({ revocation, applied: true, execution: { ...value, current, events: [...value.events, current] } }));
  expect(screen.getByText(/최근 저장 상태: 응답 확인 불가/)).toBeTruthy();
  expect(screen.getByText("실행 허용 철회를 기록했습니다.")).toBeTruthy();
  expect(screen.getByText(/철회 기록: server-user/)).toBeTruthy();
  expect(changed).toHaveBeenCalledOnce();
  expect(api.fetchJson).toHaveBeenCalledTimes(3);
});

it("isolates late controls when its URL changes without an external React key", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved)
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ ...saved, contract: { ...saved.contract, execution_id: "other" }, current: { ...event, state: "SUCCEEDED" } });
  const { rerender } = await open();
  fireEvent.click(screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }));
  rerender(<JournalExecutionHistory url={`${url}-other`} onChanged={changed}/>);
  await screen.findByText(/최근 저장 상태: 응답 저장 완료/);
  await act(async () => finish({ execution: cancelled(), applied: true }));
  expect(changed).not.toHaveBeenCalled();
  expect(screen.queryByText(/최근 저장 상태: 결과 저장 취소/)).toBeNull();
});
