import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ExecutionDetail } from "../../lib/journalExecutions";
import { JournalCodexDispatch } from "../views/JournalCodexDispatch";
import { JournalExecutionHistory } from "../views/JournalExecutionHistory";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
beforeEach(() => { vi.resetAllMocks(); vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001"); });
const hash = "a".repeat(64), bindingHash = "b".repeat(64), authorityHash = "c".repeat(64);
const url = "/api/topics/topic/local-synthesis/job/journal-executions/exec";
const manifest = {
  schema_version: "CODEX_JOURNAL_CLI_V2", adapter_id: "xrml-codex-frozen-journal", adapter_version: "2",
  contract_digest: hash, contract_sha256: hash, request_sha256: hash, model: "fixture-model", reasoning_effort: "medium",
  runtime_identity: { launcher_sha256: hash, environment_sha256: hash, workspace_sha256: hash },
  argv_shape: ["{explicit_launcher}", "exec", "--model", "fixture-model"],
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
  specification: { provider_name: "codex_cli", model: "fixture-model", max_attempts: 3, max_output_tokens: 4000, expected_input_digest: hash } }, current: event, events: [] };
const authority = { authorization_id: "server-allowance", record_digest: authorityHash, status: "AVAILABLE",
  expires_at: "2026-10-07T00:05:00Z", issuer: "trusted-server", policy_version: "SERVER_POLICY_V1" };
const status = { schema_version: "CODEX_JOURNAL_DISPATCH_STATUS_V1", enabled: true, reason: null,
  profile: "CODEX_JOURNAL_CLI_V2", execution_id: "exec", actor_id: "owner", contract_digest: hash, observed_sequence: 0,
  manifest, binding: { surface_id: "codex_cli", adapter_id: "xrml-codex-frozen-journal", adapter_version: "2", command_shape_sha256: bindingHash },
  authorities: [authority], authorities_has_more: false };
const receipt = { schema_version: "JOURNAL_DISPATCH_RECEIPT_V2" as const, actor_id: "owner", reserved_output_tokens: 4000,
  request: { request_id: "00000000-0000-4000-8000-000000000001", expected_sequence: 0, acknowledge_unknown: false },
  binding: status.binding, authority: { authorization_id: authority.authorization_id, record_digest: authorityHash },
  grant: { authorization_id: authority.authorization_id, expires_at: authority.expires_at, max_attempts: 3, max_reserved_output_tokens: 12000 } };
const leasedEvent = { ...event, state: "LEASED" as const, sequence: 1, attempt: 1, dispatch: receipt };
const leased: ExecutionDetail = { ...saved, current: leasedEvent, events: [leasedEvent] };
const updated = vi.fn(), busy = vi.fn();
const props = { url, saved, onUpdated: updated, onBusy: busy };
const view = () => render(<JournalCodexDispatch {...props}/>);
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
const inspect = () => fireEvent.click(screen.getByRole("button", { name: /Codex 실행 허용 확인/ }));
const select = async () => fireEvent.change(await screen.findByRole("combobox", { name: "서버 실행 허용 선택" }), { target: { value: authority.authorization_id } });
const dispatch = () => fireEvent.click(screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }));

it("lazily reads server status, requires explicit authority selection and sends no browser approval", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockResolvedValueOnce({ execution: leased });
  view(); expect(api.fetchJson).not.toHaveBeenCalled(); inspect();
  expect((await screen.findByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }) as HTMLButtonElement).disabled).toBe(true);
  await select(); expect(screen.getByText(/현재 서버가 실행에 사용할 전송 조건/)).toBeTruthy();
  dispatch(); await waitFor(() => expect(updated).toHaveBeenCalledWith(leased));
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${url}/codex-dispatch`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 0,
    acknowledge_unknown: false, authorization_id: authority.authorization_id, expected_authority_digest: authorityHash, expected_binding_digest: bindingHash });
  expect(screen.queryByRole("button", { name: /승인 기록|권한 발급|후보 채택/ })).toBeNull();
  expect(screen.getByText(/실제 계정·모델·과금 경로를 검증했다는 뜻은 아닙니다/)).toBeTruthy();
});

it.each([
  ["execution_id", "other"], ["actor_id", "other"], ["observed_sequence", 1], ["profile", "CODEX_JOURNAL_CLI_V1"],
  ["binding", { ...status.binding, adapter_version: "1" }], ["manifest", { ...manifest, contract_digest: "d".repeat(64) }],
  ["authorities", [{ ...authority, record_digest: "invalid" }]], ["authorities", [authority, authority]],
])("fails closed on malformed or detached status %s", async (field, value) => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...status, [field]: value }); view(); inspect();
  await screen.findByRole("alert"); expect(posts()).toHaveLength(0);
  expect(screen.queryByRole("combobox")).toBeNull();
});

it("shows default disabled status and expired/revoked/mismatched allowances without enabling execution", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...status, enabled: false, reason: "journal_codex_dispatch_disabled", manifest: null, binding: null,
    authorities: ["EXPIRED", "REVOKED", "MISMATCH"].map(value => ({ ...authority, status: value, authorization_id: value })) });
  view(); inspect(); await screen.findByText(/이 서버에서는 Codex 실행이 비활성화/);
  expect(screen.getByText(/기한 만료/)).toBeTruthy(); expect(screen.getByText(/철회됨/)).toBeTruthy(); expect(screen.getByText(/현재 실행 조건과 불일치/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }) as HTMLButtonElement).disabled).toBe(true);
  expect(posts()).toHaveLength(0);
});

it("preserves an uncertain request across status refresh, sequence change and disabled runtime for exact replay", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockRejectedValueOnce(new Error("response lost"))
    .mockResolvedValueOnce({ ...status, enabled: false, reason: "journal_codex_dispatch_disabled", manifest: null, binding: null, observed_sequence: 1, authorities: [] })
    .mockResolvedValueOnce({ execution: leased });
  const { rerender } = view(); inspect(); await select(); dispatch(); await screen.findByRole("alert");
  rerender(<JournalCodexDispatch {...props} saved={leased}/>); inspect();
  await screen.findByText(/이 서버에서는 Codex 실행이 비활성화/);
  fireEvent.click(screen.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" }));
  await waitFor(() => expect(updated).toHaveBeenCalledWith(leased));
  expect(posts()).toHaveLength(2); expect(posts()[1]).toEqual(posts()[0]);
});

it("retains uncertain dispatch during the parent's execution history refresh", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, options) => {
    if (options?.method === "POST") throw new Error("response lost");
    if (path === url) return (++reads === 1 ? saved : leased) as never;
    return status as never;
  });
  render(<JournalExecutionHistory url={url} onChanged={vi.fn()}/>);
  await screen.findByText(/최근 저장 상태/); inspect(); await select(); dispatch(); await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/최근 저장 상태: 호출 선점 기록/);
  expect(screen.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toBeTruthy();
  expect(posts()).toHaveLength(1);
});

it("requires explicit unknown-outcome acknowledgement only for a new attempt", async () => {
  const unknown: ExecutionDetail = { ...saved, current: { ...event, state: "INTERRUPTED", sequence: 3, attempt: 1, outcome_unknown: true } };
  const retryEvent = { ...leasedEvent, sequence: 4, attempt: 2, dispatch: { ...receipt, request: { ...receipt.request, expected_sequence: 3, acknowledge_unknown: true } } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...status, observed_sequence: 3 }).mockResolvedValueOnce({ execution: { ...unknown, current: retryEvent, events: [retryEvent] } });
  render(<JournalCodexDispatch {...props} saved={unknown}/>); inspect(); await select();
  expect((screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: /이전 시도의 결과가 불명확/ })); dispatch();
  await waitFor(() => expect(posts()).toHaveLength(1));
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ expected_sequence: 3, acknowledge_unknown: true });
});

it("invalidates a selected allowance when refreshed binding changes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockResolvedValueOnce({ ...status, binding: { ...status.binding, command_shape_sha256: "d".repeat(64) } });
  view(); inspect(); await select(); inspect();
  await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe(""));
  expect((screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }) as HTMLButtonElement).disabled).toBe(true);
});

it("keeps exhausted authority visible but nonselectable even when request attempts remain", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...status, authorities: [{ ...authority, status: "BUDGET_EXHAUSTED" }] });
  view(); inspect(); await screen.findByText("허용된 시도·예약 한도 소진");
  expect((screen.getByRole("option", { name: authority.authorization_id }) as HTMLOptionElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }) as HTMLButtonElement).disabled).toBe(true);
  expect(posts()).toHaveLength(0);
});

it("clears a definitively rejected request and requires status inspection again", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockRejectedValueOnce(new api.ApiError("journal_authority_revoked", 409, ""));
  view(); inspect(); await select(); dispatch(); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toBeNull();
  expect(posts()).toHaveLength(1); expect(vi.mocked(api.fetchJson).mock.calls).toHaveLength(2);
});

it.each(["actor", "missing_receipt", "authority", "binding", "request"]) ("keeps the same request after a mismatched success response: %s", async field => {
  const result = structuredClone(leased);
  if (field === "actor") result.contract.actor_id = "other";
  else if (field === "missing_receipt") result.events = [];
  else {
    const changed = structuredClone(receipt);
    if (field === "authority") changed.authority.record_digest = "d".repeat(64);
    if (field === "binding") changed.binding.command_shape_sha256 = "d".repeat(64);
    if (field === "request") changed.request.expected_sequence = 1;
    result.events = [{ ...leasedEvent, dispatch: changed }];
  }
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockResolvedValueOnce({ execution: result });
  view(); inspect(); await select(); dispatch(); await screen.findByRole("alert");
  expect(updated).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toBeTruthy();
});

it("discards a late write response when changing execution scope", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { rerender } = view(); inspect(); await select(); dispatch();
  rerender(<JournalCodexDispatch {...props} url={`${url}-other`} saved={{ ...saved, contract: { ...saved.contract, execution_id: "other" } }}/>);
  await act(async () => finish({ execution: leased }));
  expect(updated).not.toHaveBeenCalled(); expect(screen.queryByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" })).toBeNull();
  expect(posts()).toHaveLength(1);
});

it("polls only stored execution history while Codex is active, never dispatching on a timer", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(leased).mockResolvedValueOnce({ ...leased, current: { ...leased.current, state: "SUCCEEDED", sequence: 3 } });
  render(<JournalExecutionHistory url={url} onChanged={updated}/>);
  await screen.findByText(/최근 저장 상태/);
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(2), { timeout: 1800 });
  expect(posts()).toHaveLength(0);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([path]) => path === url)).toBe(true);
});

it("uses keyboard-accessible native controls and blocks reads/writes during another history action", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(status); const { rerender } = view(); inspect(); await select();
  rerender(<JournalCodexDispatch {...props} disabled/>);
  const region = screen.getByRole("region", { name: "서버 허용 Codex 실행" });
  for (const button of within(region).getAllByRole("button")) expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole("combobox")).toBeNull();
});

it("can inspect, choose and submit with keyboard focus without automatic dispatch", async () => {
  const user = userEvent.setup();
  vi.mocked(api.fetchJson).mockResolvedValueOnce(status).mockResolvedValueOnce({ execution: leased }); view();
  await user.tab(); expect(document.activeElement).toBe(screen.getByRole("button", { name: /Codex 실행 허용 확인/ }));
  await user.keyboard("{Enter}");
  const selectControl = await screen.findByRole("combobox", { name: "서버 실행 허용 선택" });
  for (let step = 0; step < 4 && document.activeElement !== selectControl; step++) await user.tab();
  expect(document.activeElement).toBe(selectControl);
  await user.selectOptions(selectControl, authority.authorization_id); expect(posts()).toHaveLength(0);
  await user.tab(); expect(document.activeElement).toBe(screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }));
  await user.keyboard("{Enter}"); await waitFor(() => expect(updated).toHaveBeenCalledWith(leased));
});

it("rejects a late status read after the current execution sequence changes", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { rerender } = view(); inspect(); rerender(<JournalCodexDispatch {...props} saved={leased}/>);
  await act(async () => finish(status)); await screen.findByRole("alert");
  expect(screen.queryByRole("combobox")).toBeNull(); expect(posts()).toHaveLength(0);
});
