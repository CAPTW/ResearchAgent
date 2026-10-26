import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { TransportDetail, TransportList } from "../../lib/reviewTransport";
import { PaperApproval } from "../components/PaperApproval";
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
const offer = { authorization_id: authority.authorization_id, record_sha256: hash, issuer_id: authority.issuer_id,
  policy_version: authority.policy_version, input_origin: "FIXTURE_SYNTHETIC", expires_at: authority.expires_at,
  max_attempts: 1, reserved_output_tokens: 2200 };
const shown = { schema_version: "PAPER_APP_APPROVAL_V1", execution_id: ready.contract.execution_id, actor_id: "owner",
  contract_sha256: contractHash, observed_sequence: 0, approval_allowed: true, execution_allowed: false, reason: null, offer };
const saved = { ...approved, binding_sha256: bindingHash, events: [event, { ...approvedEvent,
  command: { kind: "control", body: { request_id: guid, expected_sequence: 0, action: "approve" } } }] };
const result = { ...saved, current: saved.events[1] };
const updated = vi.fn(), busy = vi.fn(), pending = vi.fn();
const props = { root: base, selected: projectedReady, onUpdated: updated, onBusy: busy, onPending: pending };
const inspect = () => fireEvent.click(screen.getByRole("button", { name: "선택한 논문 요청의 승인 등록 조건 확인" }));
const register = () => fireEvent.click(screen.getByRole("button", { name: "확인한 논문 승인 기록 등록" }));
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");

it("registers the observed source after explicit inspection without dispatch", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(shown).mockResolvedValueOnce(result);
  render(<PaperApproval {...props}/>); expect(posts()).toHaveLength(0); inspect();
  await screen.findByText(/합성 시험 자료/);
  expect(screen.getByText(/trusted-server/)).toBeTruthy();
  screen.getByRole("button", { name: "확인한 논문 승인 기록 등록" }).focus();
  await userEvent.keyboard("{Enter}");
  await waitFor(() => expect(updated).toHaveBeenCalledWith(result));
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${base}/transport-one/codex-approval`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: guid, expected_sequence: 0,
    expected_contract_sha256: contractHash, expected_authority_sha256: hash });
  expect(screen.getByText(/실행은 시작하지 않았습니다/)).toBeTruthy();
});

it("accepts the unprojected approval API history after exact contract and command matching", async () => {
  const { binding_sha256: _binding, contract_sha256: _contract, ...raw } = result;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(shown).mockResolvedValueOnce(raw);
  render(<PaperApproval {...props}/>); inspect(); await screen.findByText(/trusted-server/); register();
  await waitFor(() => expect(updated).toHaveBeenCalledWith({ ...raw, contract_sha256: contractHash }));
  expect(screen.queryByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" })).toBeNull();
});

it.each(["actor_id", "execution_id", "contract_sha256", "observed_sequence", "execution_allowed", "origin", "limit", "positive_limit", "positive_attempts"])("rejects a detached or invalid preview: %s", async field => {
  const value = structuredClone(shown);
  if (field === "origin") value.offer.input_origin = "PROVIDER_RESPONSE";
  else if (field === "limit") value.offer.reserved_output_tokens = -1;
  else if (field === "positive_limit") value.offer.reserved_output_tokens = 1;
  else if (field === "positive_attempts") value.offer.max_attempts = 2;
  else Object.assign(value, { [field]: field === "observed_sequence" ? 1 : field === "execution_allowed" ? true : "wrong" });
  vi.mocked(api.fetchJson).mockResolvedValue(value); render(<PaperApproval {...props}/>); inspect();
  await screen.findByRole("alert"); expect(posts()).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "확인한 논문 승인 기록 등록" })).toBeNull();
});

it.each(["paper_approval_disabled", "paper_approval_source_unavailable", "paper_review_authority_expired"])("explains unavailable registration %s", async reason => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...shown, approval_allowed: false, reason, offer: null });
  render(<PaperApproval {...props}/>); inspect(); await screen.findByText(/승인 등록 불가/);
  expect(posts()).toHaveLength(0);
});

it("retains an exact unknown request through refresh and disabled source", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(shown).mockRejectedValueOnce(new Error("lost"))
    .mockResolvedValueOnce({ ...shown, observed_sequence: 1, approval_allowed: false, offer: null, reason: "paper_approval_disabled" })
    .mockResolvedValueOnce(result);
  const view = render(<PaperApproval {...props}/>); inspect(); await screen.findByText(/trusted-server/); register();
  await screen.findByRole("alert"); view.rerender(<PaperApproval {...props} selected={result}/>); inspect();
  await screen.findByText(/승인 등록 불가/);
  fireEvent.click(screen.getByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" }));
  await waitFor(() => expect(updated).toHaveBeenCalledWith(result));
  expect(posts()[1]).toEqual(posts()[0]); expect(pending).toHaveBeenLastCalledWith(false);
});

it.each(["contract", "authority", "command", "contract_hash", "binding_hash", "malformed_binding"])("keeps pending for mismatched success: %s", async kind => {
  const changed = structuredClone(result);
  if (kind === "contract") changed.contract.actor_id = "other";
  else if (kind === "authority") changed.authority!.record_sha256 = "d".repeat(64);
  else if (kind === "contract_hash") changed.contract_sha256 = "d".repeat(64);
  else if (kind === "binding_hash") changed.binding_sha256 = "d".repeat(64);
  else if (kind === "malformed_binding") changed.binding_sha256 = "invalid";
  else {
    const approved = changed.events[1];
    if (!("command" in approved)) throw new Error("fixture missing approval command");
    approved.command.body.request_id = "other";
  }
  vi.mocked(api.fetchJson).mockResolvedValueOnce(shown).mockResolvedValueOnce(changed);
  render(<PaperApproval {...props}/>); inspect(); await screen.findByText(/trusted-server/); register();
  await screen.findByRole("alert"); expect(updated).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" })).toBeTruthy();
});

it.each([401, 403, 409])("requires fresh inspection after definite rejection %s", async status => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(shown).mockRejectedValueOnce(new api.ApiError("denied", status, ""));
  render(<PaperApproval {...props}/>); inspect(); await screen.findByText(/trusted-server/); register();
  await screen.findByRole("alert"); expect(screen.queryByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" })).toBeNull();
  expect(pending).toHaveBeenLastCalledWith(false); expect(updated).not.toHaveBeenCalled();
});

it("ignores a late response after execution scope change", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(shown).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<PaperApproval {...props}/>); inspect(); await screen.findByText(/trusted-server/); register();
  view.rerender(<PaperApproval {...props} selected={{ ...projectedReady, contract: { ...ready.contract, execution_id: "other" } }}/>);
  await act(async () => finish(result)); expect(updated).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" })).toBeNull();
});

it("keeps pending through parent refresh, blocks new dispatch and allows cancellation", async () => {
  const cancelledEvent = { ...event, sequence: 1, state: "CANCELLED" as const };
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("/controls")) return { ...projectedReady, current: cancelledEvent, events: [event, cancelledEvent] } as never;
    if (options?.method === "POST") throw new Error("lost");
    if (url.endsWith("/codex-approval")) return shown as never;
    return { ...listing, items: [projectedReady] } as never;
  });
  render(<ReviewTransport root={root} id={preparationId}/>);
  const details = screen.getByText("검토 실행 조건·이력").closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  await screen.findByText(/마지막 저장 상태/);
  inspect(); await screen.findByText(/trusted-server/); register(); await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" }) as HTMLButtonElement).disabled).toBe(false));
  expect((screen.getByRole("button", { name: "선택한 Codex 요청의 서버 허용 확인" }) as HTMLButtonElement).disabled).toBe(true);
  // A missing registration response must not lock the user's safety controls.
  expect((screen.getByRole("button", { name: "이 CLI 요청 취소" }) as HTMLButtonElement).disabled).toBe(false);
  expect(posts()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "이 CLI 요청 취소" }));
  await screen.findByText(/마지막 저장 상태: 취소됨/);
  expect(JSON.parse(posts()[1][1]!.body as string)).toMatchObject({ action: "cancel", expected_sequence: 0 });
  expect(screen.getByRole("button", { name: "같은 논문 승인 등록 요청 다시 확인" })).toBeTruthy();
});
