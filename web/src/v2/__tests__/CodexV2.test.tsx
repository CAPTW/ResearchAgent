import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ExecutionDetail } from "../../lib/journalExecutions";
import { JournalAdapterManifest, readJournalAdapterManifest } from "../views/JournalAdapterManifest";
import { JournalCodexPreview } from "../views/JournalCodexPreview";
import { JournalCodexCompatibility } from "../views/JournalCodexCompatibility";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
beforeEach(() => vi.resetAllMocks());
const hash = "a".repeat(64);
const identity = { launcher_sha256: hash, environment_sha256: hash, workspace_sha256: hash };
const manifest = {
  schema_version: "CODEX_JOURNAL_CLI_V2", adapter_id: "xrml-codex-frozen-journal", adapter_version: "2",
  contract_digest: hash, contract_sha256: hash, request_sha256: hash, model: "fixture-model", reasoning_effort: "medium", runtime_identity: identity,
  argv_shape: ["{explicit_launcher}", "exec", "--model", "fixture-model", "-c", 'model_provider="xrml_openai"'],
  stdin_sha256: hash, stdin_bytes: 1200, schema_sha256: hash, timeout_seconds: 90,
  max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_final_bytes: 512000,
  automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false,
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
  runtime_profile: "CODEX_JOURNAL_CLI_V2", model_provider: "xrml_openai", requires_openai_auth: true,
  wire_api: "responses", forced_login_method: "chatgpt", request_max_retries: 0, stream_max_retries: 0, supports_websockets: false,
  authentication: "OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED", endpoint_resolution: "OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED",
  retry_policy: "REQUESTED_ZERO_NOT_LIVE_VERIFIED", xrml_process_relaunch: false, remote_call_count_enforced: false,
  cost_limit_enforced: false, live_execution: "NOT_VERIFIED", billing_route: "CHATGPT_ACCOUNT_NOT_VERIFIED",
  raw_output_encoding: "LAST_MESSAGE_JSON_UTF8_V1", raw_envelope_retained: false,
};
const event = { sequence: 0, state: "READY" as const, attempt: 0, at: "2026-10-07T00:00:00Z", deadline: null, outcome_unknown: false, reason: null };
const saved: ExecutionDetail = { contract: { execution_id: "exec", actor_id: "owner", created_at: event.at, packet: { packet_id: "packet" },
  specification: { provider_name: "codex_cli", model: "fixture-model", max_attempts: 1, max_output_tokens: 4000, expected_input_digest: hash } }, current: event, events: [] };
const preview = { schema_version: "CODEX_JOURNAL_PREVIEW_V1", preview_only: true, execution_allowed: false,
  execution_id: "exec", actor_id: "owner", observed_sequence: 0, contract_digest: hash, available: true, reason: null, manifest,
  binding: { surface_id: "codex_cli", adapter_id: "xrml-codex-frozen-journal", adapter_version: "2", command_shape_sha256: hash }, preview_digest: hash };
const props = { url: "/execution", executionId: "exec", actorId: "owner", sequence: 0, contractDigest: hash, runtimeIdentity: identity };
const profile = "CODEX_JOURNAL_CLI_V2" as const;
const report = { schema_version: "CODEX_CLI_COMPATIBILITY_V2", profile, runtime_identity: identity, requirements_sha256: hash,
  checked_at: "2026-10-07T00:00:00Z", cli_version: "codex-cli 0.162.0-alpha.2", status: "CONFIGURATION_OBSERVED", reason: null,
  execution_allowed: false, model_invoked: false,
  checks: ["version", "exec_help", "configuration"].map(name => ({ name, exit_code: 0, stdout_sha256: hash })) };
const observation = { schema_version: "CODEX_JOURNAL_COMPATIBILITY_V1", execution_id: "exec", actor_id: "owner", observed_sequence: 0,
  contract_digest: hash, execution_allowed: false, model_invoked: false, status: report.status, reason: null, report };
const check = () => fireEvent.click(screen.getByRole("button", { name: "CLI 설정 검사 기록 확인" }));

it("identifies V2 ChatGPT authentication and requested retry settings without claiming live enforcement", () => {
  expect(readJournalAdapterManifest(manifest)).toEqual(manifest);
  render(<JournalAdapterManifest value={manifest}/>);
  fireEvent.click(screen.getByText("고정된 전송 조건"));
  expect(screen.getByRole("heading", { name: "Codex CLI V2 실행 계획" })).toBeTruthy();
  expect(screen.getByText(/공식 CLI가 관리하는 ChatGPT 로그인/)).toBeTruthy();
  expect(screen.getByText(/요청 재시도 0회 · 스트림 재시도 0회/)).toBeTruthy();
  expect(screen.getByText(/실제 내부 재시도·외부 호출 횟수·비용 상한은 검증되지 않았습니다/)).toBeTruthy();
  expect(screen.queryByText(/^자동 재시도: 사용하지 않음/)).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

it.each([
  ["adapter_version", "1"], ["runtime_profile", "CODEX_JOURNAL_CLI_V1"], ["model_provider", "openai"],
  ["requires_openai_auth", false], ["forced_login_method", "api"], ["request_max_retries", 1], ["stream_max_retries", 1],
  ["supports_websockets", true], ["authentication", "VERIFIED"], ["retry_policy", "ENFORCED_ZERO"],
  ["raw_output_encoding", "STDOUT_ENVELOPE"], ["raw_envelope_retained", true],
  ["contract_sha256", undefined], ["request_sha256", "invalid"],
])("refuses misleading V2 profile field %s", (field, value) => {
  expect(readJournalAdapterManifest({ ...manifest, [field]: value })).toBeNull();
});

it("reads a V2 preview and matching V2 observation through existing GET-only controls", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith("codex-preview") ? preview : observation);
  render(<JournalCodexPreview url="/execution" saved={saved}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" }));
  await screen.findByText(`미리보기 SHA-256: ${hash}`);
  fireEvent.click(screen.getByText("현재 미리보기 전송 조건"));
  expect(screen.getByRole("heading", { name: "Codex CLI V2 실행 계획" })).toBeTruthy();
  check(); await screen.findByText(/실제 exec 실행 호환성을 통과했다는 뜻은 아닙니다/);
  expect(screen.getByText(/검사 대상 프로필: Codex CLI V2/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /실행 시작|승인/ })).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.map(([path, init]) => [path, init])).toEqual([
    ["/execution/codex-preview", undefined], ["/execution/codex-compatibility", undefined],
  ]);
});

it.each(["1", "3"])("rejects a V2 preview with adapter binding version %s", async adapter_version => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...preview, binding: { ...preview.binding, adapter_version } });
  render(<JournalCodexPreview url="/execution" saved={saved}/>);
  fireEvent.click(screen.getByRole("button", { name: "현재 Codex 실행 조건 확인" }));
  await screen.findByRole("alert");
  expect(screen.queryByText(/미리보기 SHA-256:/)).toBeNull();
});

it.each([
  ["CODEX_CLI_COMPATIBILITY_V1", "CODEX_JOURNAL_CLI_V1"],
  ["CODEX_CLI_COMPATIBILITY_V1", "CODEX_JOURNAL_CLI_V2"],
  ["CODEX_CLI_COMPATIBILITY_V2", "CODEX_JOURNAL_CLI_V1"],
])("rejects cross-profile observation %s / %s even with matching runtime hashes", async (schema_version, profile) => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...observation, report: { ...report, schema_version, profile } });
  render(<JournalCodexCompatibility {...props} profile="CODEX_JOURNAL_CLI_V2"/>); check();
  await screen.findByRole("alert");
  expect(screen.queryByText(/검사 CLI:/)).toBeNull();
});

it("keeps a rejected V2 observation distinct from the historical V1 rejection", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...observation, status: "CONFIGURATION_REJECTED", reason: "config_rejected",
    report: { ...report, status: "CONFIGURATION_REJECTED", reason: "config_rejected", checks: report.checks.map(check => ({ ...check, exit_code: check.name === "configuration" ? 1 : 0 })) } });
  render(<JournalCodexCompatibility {...props} profile="CODEX_JOURNAL_CLI_V2"/>); check();
  await screen.findByText(/V2 설정을 적용한 로컬 CLI 검사에 실패했습니다/);
  expect(screen.queryByText(/V1 설정을 적용한/)).toBeNull();
});

it("discards an in-flight V1 observation when the same request changes to profile V2", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<JournalCodexCompatibility {...props} profile="CODEX_JOURNAL_CLI_V1"/>); check();
  view.rerender(<JournalCodexCompatibility {...props} profile="CODEX_JOURNAL_CLI_V2"/>);
  await act(async () => finish({ ...observation, report: { ...report, schema_version: "CODEX_CLI_COMPATIBILITY_V1", profile: "CODEX_JOURNAL_CLI_V1" } }));
  expect(screen.queryByText(/검사 CLI:/)).toBeNull();
  expect(screen.queryByText(/격리된 로컬 명령·설정 목록을 확인했습니다/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledTimes(1);
});
