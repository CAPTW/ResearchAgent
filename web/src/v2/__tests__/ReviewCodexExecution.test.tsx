import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { TransportDetail, TransportList } from "../../lib/reviewTransport";
import { ReviewCodexExecution } from "../components/ReviewCodexExecution";
import { ReviewTransport } from "../components/ReviewTransport";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const guid = "00000000-0000-4000-8000-000000000001";
beforeEach(() => { vi.resetAllMocks(); vi.spyOn(crypto, "randomUUID").mockReturnValue(guid); });
const root = "/api/topics/topic/paper-bank/version/review-preparations", preparationId = "prepared-one";
const base = `${root}/${preparationId}/transport-executions`, hash = "a".repeat(64), bindingHash = "b".repeat(64), contractHash = "c".repeat(64);
const at = "2026-10-07T00:00:00Z";
const prepared = { schema_version: "PREPARED_REVIEW_REQUEST_V3", provider_name: "codex_cli", model: "fixture-model", request_sha256: hash, contract_sha256: hash,
  provider_request: { max_output_tokens: 2200, temperature: 0.2, prompt: "frozen body", schema_name: "PREPARED_PAPER_REVIEW_DRAFT_V3" },
  packet: { source_preparation: { preparation_id: preparationId, preparation_sha256: hash, selection: { start: 12, end: 16, chars: 4, sha256: hash },
    source: { run_id: "run", paper_id: "paper", artifact_ref: "run:raw/paper.txt", source_chars: 1000 }, normalization: "XRML_WHITESPACE_V1" },
    evidence_spans: [{ text: "e\u0301한🙂", span_id: "frozen-span" }] } };
const manifest = {
  schema_version: "CODEX_PREPARED_REVIEW_CLI_V2", adapter_id: "xrml-codex-prepared-review", adapter_version: "2",
  contract_sha256: hash, request_sha256: hash, model: "fixture-model", reasoning_effort: "medium",
  runtime_identity: { launcher_sha256: hash, environment_sha256: hash, workspace_sha256: hash },
  argv_shape: ["{explicit_launcher}", "exec", "--model", "fixture-model"], stdin_sha256: hash, stdin_bytes: 1200, schema_sha256: hash,
  timeout_seconds: 90, max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_final_bytes: 512000, max_review_output_bytes: 512000,
  automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false,
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
  runtime_profile: "CODEX_JOURNAL_CLI_V2", model_provider: "xrml_openai", requires_openai_auth: true, wire_api: "responses",
  forced_login_method: "chatgpt", request_max_retries: 0, stream_max_retries: 0, supports_websockets: false,
  authentication: "OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED", endpoint_resolution: "OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED",
  retry_policy: "REQUESTED_ZERO_NOT_LIVE_VERIFIED", xrml_process_relaunch: false, remote_call_count_enforced: false,
  cost_limit_enforced: false, live_execution: "NOT_VERIFIED", billing_route: "CHATGPT_ACCOUNT_NOT_VERIFIED",
  raw_output_encoding: "LAST_MESSAGE_JSON_UTF8_V1", raw_envelope_retained: false,
};
const preview = { schema_version: "CODEX_PAPER_PREVIEW_V1", enabled: true, reason: null, actor_id: "owner", preparation_id: preparationId,
  preparation_digest: hash, profile: "CODEX_PREPARED_REVIEW_CLI_V2", binding_sha256: bindingHash, prepared, manifest,
  input_origin: "FIXTURE_SYNTHETIC", execution_allowed: false };
const event = { sequence: 0, state: "READY" as const, at, deadline: null, outcome_unknown: false, reason: null, receipt_id: null, authority: null };
const ready: TransportDetail = { contract: { execution_id: "transport-one", preparation_id: preparationId, actor_id: "owner", created_at: at,
  mode: "TRUSTED_PAPER_CLI", input_origin: "FIXTURE_SYNTHETIC", specification: { request_id: guid, expected_preparation_digest: hash,
    acknowledge_unknown: false, provider_name: "codex_cli", model: "fixture-model", max_output_tokens: 2200 }, prepared, transport: manifest },
  contract_sha256: contractHash, current: event, events: [event], authority: null };
const projectedReady = { ...ready, binding_sha256: bindingHash };
const authority = { authorization_id: "server-allowance", record_sha256: hash, issuer_id: "trusted-server", policy_version: "SERVER_POLICY_V1",
  granted_at: at, expires_at: "2999-01-01T00:00:00Z", revoked_at: null, validity: "WITHIN_VALIDITY_WINDOW" as const };
const approvedEvent = { ...event, sequence: 1, state: "APPROVED" as const, deadline: authority.expires_at,
  authority: { authorization_id: authority.authorization_id, record_sha256: hash } };
const approved: TransportDetail = { ...ready, current: approvedEvent, events: [event, approvedEvent], authority };
const leasedEvent = { ...event, sequence: 2, state: "LEASED" as const, deadline: authority.expires_at,
  command: { kind: "dispatch", body: { request_id: guid, expected_sequence: 1 } } };
const leased = { ...approved, binding_sha256: bindingHash, current: leasedEvent, events: [...approved.events, leasedEvent] };
const dispatchStatus = { schema_version: "CODEX_PAPER_DISPATCH_STATUS_V1", enabled: true, reason: null,
  profile: "CODEX_PREPARED_REVIEW_CLI_V2", preparation_id: preparationId, actor_id: "owner", execution_id: ready.contract.execution_id,
  observed_sequence: 1, contract_sha256: contractHash, binding_sha256: bindingHash, input_origin: "FIXTURE_SYNTHETIC", authority, manifest };
const capabilities = { create: false, approve: false, dispatch: false, cancel: true, recover: true, save_result: true,
  reason: "paper_review_execution_not_configured", issuer_status: "NOT_CONFIGURED", provider_status: "UNREGISTERED", live_execution: "NOT_VERIFIED" };
const listing: TransportList = { preparation_digest: hash, items: [], page: 1, pageSize: 10, hasMore: false, outcome_unknown_pending: false, capabilities };
const onUpdated = vi.fn(), onBusy = vi.fn(), onPending = vi.fn();
const props = { root: base, preparationId, listing, selected: null, onUpdated, onBusy, onPending };
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
const inspect = () => fireEvent.click(screen.getByRole("button", { name: "Codex 검토 입력·실행 조건 확인" }));
const prepare = () => fireEvent.click(screen.getByRole("button", { name: "이 입력으로 Codex 검토 요청 준비" }));
const inspectDispatch = () => fireEvent.click(screen.getByRole("button", { name: "선택한 Codex 요청의 서버 허용 확인" }));
const dispatch = () => fireEvent.click(screen.getByRole("button", { name: "서버가 허용한 Codex 검토 실행" }));

it("separates lazy frozen-body preview, preparation and manual server-authorized dispatch", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(projectedReady).mockResolvedValueOnce(dispatchStatus).mockResolvedValueOnce(leased);
  const { rerender } = render(<ReviewCodexExecution {...props}/>);
  expect(api.fetchJson).not.toHaveBeenCalled(); inspect();
  await screen.findByText(/12–16 · 4자/); fireEvent.click(screen.getByText("Codex 검토에 전달할 고정 본문"));
  expect(screen.getByLabelText("Codex 검토 고정 본문").textContent).toBe("e\u0301한🙂");
  expect(screen.getByText(/논문 전체 검토 완료를 뜻하지 않습니다/)).toBeTruthy();
  prepare(); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(projectedReady));
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(`${base}/codex-requests`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: guid, expected_preparation_digest: hash, acknowledge_unknown: false, expected_binding_sha256: bindingHash });
  rerender(<ReviewCodexExecution {...props} listing={{ ...listing, items: [approved] }} selected={approved}/>);
  inspectDispatch(); await screen.findByText(/서버 승인 유효 기간 내/); dispatch();
  await waitFor(() => expect(onUpdated).toHaveBeenLastCalledWith(leased));
  expect(JSON.parse(posts()[1][1]!.body as string)).toEqual({ request_id: guid, expected_sequence: 1, expected_contract_sha256: contractHash, expected_authority_sha256: hash });
  expect(posts().every(([path]) => !path.includes("approval") && !path.includes("controls"))).toBe(true);
});

it.each([
  ["preparation_id", "other"], ["preparation_digest", "d".repeat(64)], ["profile", "CODEX_PREPARED_REVIEW_CLI_V1"],
  ["execution_allowed", true], ["binding_sha256", "invalid"], ["prepared", { ...prepared, packet: { ...prepared.packet, source_preparation: { ...prepared.packet.source_preparation, preparation_id: "other" } } }],
  ["manifest", { ...manifest, forced_login_method: "api" }],
])("rejects a malformed or detached preview %s", async (field, value) => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...preview, [field]: value }); render(<ReviewCodexExecution {...props}/>); inspect();
  await screen.findByRole("alert"); expect(screen.queryByRole("button", { name: "이 입력으로 Codex 검토 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("keeps an uncertain preparation request across disabled preview and history refresh", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new Error("lost"))
    .mockResolvedValueOnce({ ...preview, enabled: false, reason: "paper_codex_disabled", prepared: null, manifest: null, binding_sha256: null, input_origin: null })
    .mockResolvedValueOnce(projectedReady);
  const { rerender } = render(<ReviewCodexExecution {...props}/>); inspect(); await screen.findByText(/12–16 · 4자/); prepare(); await screen.findByRole("alert");
  rerender(<ReviewCodexExecution {...props} listing={{ ...listing, items: [ready] }} selected={ready}/>);
  inspect(); await screen.findByText(/이 서버에서는 Codex 논문 검토/);
  fireEvent.click(screen.getByRole("button", { name: "같은 Codex 검토 준비 요청 다시 보내기" }));
  await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(projectedReady)); expect(posts()[1]).toEqual(posts()[0]);
});

it("keeps an uncertain dispatch bound to its original execution after selection changes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(dispatchStatus).mockRejectedValueOnce(new Error("lost"))
    .mockResolvedValueOnce({ ...leased, authority: { ...authority, validity: "EXPIRED" } });
  const { rerender } = render(<ReviewCodexExecution {...props} selected={approved} listing={{ ...listing, items: [approved] }}/>);
  inspectDispatch(); await screen.findByText(/서버 승인 유효 기간 내/); dispatch(); await screen.findByRole("alert");
  rerender(<ReviewCodexExecution {...props} selected={{ ...ready, contract: { ...ready.contract, execution_id: "other" } }}/>);
  fireEvent.click(screen.getByRole("button", { name: "같은 Codex 검토 실행 요청 다시 보내기" }));
  await waitFor(() => expect(onUpdated).toHaveBeenCalled()); expect(posts()[1]).toEqual(posts()[0]);
});

it.each(["actor", "contract", "command", "authority"])("retains uncertain dispatch on mismatched success %s", async field => {
  const result = structuredClone(leased);
  if (field === "actor") result.contract.actor_id = "other";
  if (field === "contract") result.contract_sha256 = "d".repeat(64);
  if (field === "command") result.events = approved.events;
  if (field === "authority") result.authority!.record_sha256 = "d".repeat(64);
  vi.mocked(api.fetchJson).mockResolvedValueOnce(dispatchStatus).mockResolvedValueOnce(result);
  render(<ReviewCodexExecution {...props} selected={approved}/>); inspectDispatch(); await screen.findByText(/서버 승인 유효 기간 내/); dispatch();
  await screen.findByRole("alert"); expect(onUpdated).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "같은 Codex 검토 실행 요청 다시 보내기" })).toBeTruthy();
});

it.each(["NOT_YET_VALID", "EXPIRED", "REVOKED"])("does not dispatch an allowance with validity %s", async validity => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...dispatchStatus, enabled: false, reason: "paper_codex_authority_unavailable", authority: { ...authority, validity, revoked_at: validity === "REVOKED" ? at : null } });
  render(<ReviewCodexExecution {...props} selected={approved}/>); inspectDispatch();
  expect((await screen.findByRole("button", { name: "서버가 허용한 Codex 검토 실행" }) as HTMLButtonElement).disabled).toBe(true);
  expect(posts()).toHaveLength(0);
});

it("requires acknowledgement before preparing again after an unknown result", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce({ ...projectedReady, contract: { ...ready.contract, specification: { ...(ready.contract.specification as object), acknowledge_unknown: true } } });
  render(<ReviewCodexExecution {...props} listing={{ ...listing, outcome_unknown_pending: true }}/>); inspect(); await screen.findByText(/12–16 · 4자/);
  expect((screen.getByRole("button", { name: "이 입력으로 Codex 검토 요청 준비" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: /이전 결과가 불명확/ })); prepare(); await waitFor(() => expect(posts()).toHaveLength(1));
  expect(JSON.parse(posts()[0][1]!.body as string).acknowledge_unknown).toBe(true);
});

it("clears a definitively rejected command without automatic resubmission", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new api.ApiError("changed", 409, ""));
  render(<ReviewCodexExecution {...props}/>); inspect(); await screen.findByText(/12–16 · 4자/); prepare(); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "같은 Codex 검토 준비 요청 다시 보내기" })).toBeNull(); expect(posts()).toHaveLength(1);
});

it("discards a late preview when preparation scope changes", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { rerender } = render(<ReviewCodexExecution {...props}/>); inspect();
  rerender(<ReviewCodexExecution {...props} root={`${base}-other`} preparationId="other"/>);
  await act(async () => finish(preview)); expect(screen.queryByLabelText("Codex 검토 고정 본문")).toBeNull(); expect(posts()).toHaveLength(0);
});

it("polls a running Codex V2 record using GET only", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...listing, items: [leased] }).mockResolvedValueOnce({ ...leased, current: { ...leased.current, sequence: 4, state: "RESULT_RECEIVED" } });
  render(<ReviewTransport root={root} id={preparationId}/>); fireEvent.click(screen.getByText("검토 실행 조건·이력"));
  await screen.findByText(/마지막 저장 상태/); await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(2), { timeout: 1800 });
  expect(posts()).toHaveLength(0); expect(vi.mocked(api.fetchJson).mock.calls[1][0]).toBe(`${base}/transport-one`);
  expect(screen.getByRole("button", { name: "CLI 수신 결과 저장 다시 시도" })).toBeTruthy();
});

it("continues read-only polling while a received result is being published", async () => {
  const receivedEvent = { ...leasedEvent, sequence: 4, state: "RESULT_RECEIVED" as const, deadline: null };
  const received = { ...leased, current: receivedEvent, events: [...leased.events, receivedEvent] };
  const savedEvent = { ...receivedEvent, sequence: 5, state: "RESULT_SAVED" as const, receipt_id: "saved-result" };
  const saved = { ...received, current: savedEvent, events: [...received.events, savedEvent] };
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...listing, items: [received] }).mockResolvedValueOnce(saved);
  render(<ReviewTransport root={root} id={preparationId} onOpenReceipt={vi.fn()}/>);
  fireEvent.click(screen.getByText("검토 실행 조건·이력"));
  await screen.findByRole("button", { name: "CLI 수신 결과 저장 다시 시도" });
  await waitFor(() => expect(screen.getByRole("button", { name: "저장된 CLI 입력·결과 열기" })).toBeTruthy(), { timeout: 1800 });
  expect(api.fetchJson).toHaveBeenCalledTimes(2); expect(posts()).toHaveLength(0);
  expect(vi.mocked(api.fetchJson).mock.calls[1][0]).toBe(`${base}/transport-one`);
  expect(screen.queryByRole("button", { name: "CLI 수신 결과 저장 다시 시도" })).toBeNull();
});

it("offers keyboard-accessible preview and preparation without implicit execution", async () => {
  const user = userEvent.setup(); vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(projectedReady);
  render(<ReviewCodexExecution {...props}/>); await user.tab(); await user.keyboard("{Enter}"); await screen.findByText(/12–16 · 4자/);
  const target = screen.getByRole("button", { name: "이 입력으로 Codex 검토 요청 준비" });
  for (let step = 0; step < 6 && document.activeElement !== target; step++) await user.tab();
  expect(document.activeElement).toBe(target); await user.keyboard("{Enter}"); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(projectedReady));
  expect(posts().every(([path]) => path.endsWith("/codex-requests"))).toBe(true);
});
