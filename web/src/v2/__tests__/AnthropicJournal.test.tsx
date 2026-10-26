import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import type { AnthropicJournalManifest, ExecutionDetail, ExecutionEvent } from "../../lib/journalExecutions";
import { JournalAdapterManifest, readJournalAdapterManifest } from "../views/JournalAdapterManifest";
import { JournalExecutionEvidence } from "../views/JournalExecutionEvidence";

const hash = "a".repeat(64);
const manifest: AnthropicJournalManifest = {
  schema_version: "ANTHROPIC_JOURNAL_WIRE_V1", adapter_id: "xrml-anthropic-frozen-journal", adapter_version: "1",
  provider_name: "anthropic", method: "POST", endpoint: "https://api.anthropic.com/v1/messages", api_version: "2023-06-01",
  model: "requested-model", contract_digest: hash, request_sha256: hash, body_sha256: hash, body_bytes: 1200, timeout_seconds: 45,
  max_response_bytes: 4_000_000, max_journal_output_bytes: 512000,
  schema_compiler: "ANTHROPIC_JSON_SCHEMA_V1", schema_constraints: "LOCAL_ORIGINAL_VALIDATION",
  local_schema_sha256: hash, wire_schema_sha256: hash,
  stream: false, tools: [], automatic_retry: false, request_metadata: "LOCAL_BINDING_ONLY_NOT_TRANSMITTED",
  redirects: false, environment_proxy: false, output_token_limit_enforced: false, temperature_enforced: false,
  temperature_policy: "OMITTED_PROVIDER_DEFAULT",
  output_token_scope: "MESSAGES_OUTPUT_TOKENS", cost_limit_enforced: false, token_limit_policy: "REQUESTED_NOT_LIVE_VERIFIED",
  model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED", live_execution: "NOT_VERIFIED", billing_route: "API_KEY_ACCOUNT_NOT_VERIFIED",
  raw_output_encoding: "CONTENT_TEXT_JSON_UTF8_V1", raw_envelope_retained: false,
  usage_input_semantics: "NONCACHED_INPUT_TOKENS_CACHE_COUNTS_SEPARATE", total_tokens: "NOT_REPORTED_NOT_DERIVED",
};

it("recognizes the native Messages plan and distinguishes the API route from subscription login", () => {
  expect(readJournalAdapterManifest(manifest)).toEqual(manifest);
  render(<JournalAdapterManifest value={manifest}/>);
  fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByRole("heading", { name: "Anthropic Messages API 전송 계획" })).toBeTruthy();
  expect(screen.getByText(/Claude 구독 로그인과 별도인 API 키 경로/)).toBeTruthy();
  expect(screen.getByText(/API 버전: 2023-06-01/)).toBeTruthy();
  expect(screen.getByText(/로컬 출력 규격 SHA-256/)).toBeTruthy();
  expect(screen.getByText(/전송 출력 규격 SHA-256/)).toBeTruthy();
  expect(screen.getByText(/HTTP 응답 전체를 보존하지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/온도는 전송하지 않으며 Provider 기본값을 사용합니다/)).toBeTruthy();
  expect(screen.queryByText(/API 응답 저장을 요청하지|백그라운드 생성은 사용하지|입력 잘림 방지/)).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

it.each([
  ["endpoint", "https://other.invalid/v1/messages"], ["provider_name", "claude_code"], ["adapter_id", "other"],
  ["api_version", "future"], ["schema_compiler", "XAI_NONBLANK_V1"], ["schema_constraints", "SERVER_VALIDATED"],
  ["local_schema_sha256", "bad"], ["wire_schema_sha256", "bad"], ["automatic_retry", true], ["stream", true],
  ["tools", [{ type: "web_search" }]], ["output_token_limit_enforced", true], ["max_response_bytes", 5_000_000],
  ["max_journal_output_bytes", 262144], ["raw_output_encoding", "HTTP_ENVELOPE"], ["raw_envelope_retained", true],
  ["usage_input_semantics", "ALL_INPUT_TOKENS"], ["total_tokens", "DERIVED"],
  ["temperature_policy", undefined],
])("does not infer a supported Messages contract when %s changes", (key, value) => {
  expect(readJournalAdapterManifest({ ...manifest, [key]: value })).toBeNull();
});

function saved(usage: Record<string, unknown>, native = true): ExecutionDetail {
  const current: ExecutionEvent = { sequence: 3, state: "SUCCEEDED", attempt: 1, at: null, deadline: null, outcome_unknown: false, reason: null,
    result: { provider_reported: { provider_name: "anthropic", model: "reported-model", response_id: "message", usage } } };
  return { contract: { execution_id: "exec", actor_id: "owner", created_at: "2026-10-07T00:00:00Z", packet: { packet_id: "packet" },
    specification: { provider_name: "anthropic", model: "requested-model", max_attempts: 1, max_output_tokens: 4000, expected_input_digest: "frozen" } }, current,
    events: [...(native ? [{ ...current, sequence: 1, state: "LEASED" as const, result: undefined, adapter_manifest: manifest }] : []), current] };
}

async function openUsage(usage: Record<string, unknown>, native = true) {
  render(<JournalExecutionEvidence saved={saved(usage, native)}/>);
  fireEvent.click(screen.getByText("요청 내용·토큰 사용 기록"));
  return within(await screen.findByRole("region", { name: "시도 1 토큰 기록" }));
}

it("keeps non-cache, cache creation, cache read, and missing total distinct", async () => {
  const row = await openUsage({ input_tokens: 12, output_tokens: 5, cache_creation_input_tokens: 20, cache_read_input_tokens: 0 });
  expect(row.getByText("보고된 비캐시 입력 토큰").nextElementSibling?.textContent).toBe("12");
  expect(row.getByText("보고된 캐시 생성 입력 토큰").nextElementSibling?.textContent).toBe("20");
  expect(row.getByText("보고된 캐시 읽기 입력 토큰").nextElementSibling?.textContent).toBe("0");
  expect(row.getByText("보고된 합계 토큰").nextElementSibling?.textContent).toBe("미보고");
  expect(row.queryByText("보고된 입력 토큰")).toBeNull();
});

it("does not derive cache counts or compare the non-cache input/output sum with a reported total", async () => {
  const row = await openUsage({ input_tokens: 12, output_tokens: 5, total_tokens: 37, cache_creation_input_tokens: -1, cache_read_input_tokens: "20" });
  expect(row.getByText("보고된 캐시 생성 입력 토큰").nextElementSibling?.textContent).toBe("미보고");
  expect(row.getByText("보고된 캐시 읽기 입력 토큰").nextElementSibling?.textContent).toBe("미보고");
  expect(row.getByText("보고된 합계 토큰").nextElementSibling?.textContent).toBe("37");
  expect(row.queryByText(/입력·출력의 합과 보고된 합계가 다릅니다/)).toBeNull();
});

it("preserves legacy generic Anthropic input labels and total warning without a native Messages lease", async () => {
  const row = await openUsage({ input_tokens: 12, output_tokens: 5, total_tokens: 37 }, false);
  expect(row.getByText("보고된 입력 토큰").nextElementSibling?.textContent).toBe("12");
  expect(row.getByText("보고된 합계 토큰").nextElementSibling?.textContent).toBe("37");
  expect(row.getByText(/입력·출력의 합과 보고된 합계가 다릅니다/)).toBeTruthy();
  expect(row.queryByText("보고된 비캐시 입력 토큰")).toBeNull();
  expect(row.queryByText("보고된 캐시 생성 입력 토큰")).toBeNull();
});
