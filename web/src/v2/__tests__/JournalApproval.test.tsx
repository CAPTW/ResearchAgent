import { createHash, webcrypto } from "node:crypto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ExecutionDetail } from "../../lib/journalExecutions";
import { JournalApproval } from "../views/JournalApproval";
import { JournalExecutionHistory } from "../views/JournalExecutionHistory";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
beforeEach(() => { vi.resetAllMocks(); vi.stubGlobal("crypto", webcrypto); });
afterEach(() => vi.unstubAllGlobals());
const hash = "a".repeat(64), bindingHash = "b".repeat(64);
const url = "/api/topics/topic/local-synthesis/job/journal-executions/exec";
const at = "2026-10-08T00:00:00Z";
const event = { sequence: 0, state: "READY" as const, attempt: 0, at, deadline: null, outcome_unknown: false, reason: null };
const saved: ExecutionDetail = { contract: { execution_id: "exec", actor_id: "owner", created_at: at, packet: { packet_id: "packet" },
  specification: { provider_name: "codex_cli", model: "fixture-model", max_attempts: 3, max_output_tokens: 4000, expected_input_digest: hash } }, current: event, events: [] };
const manifest = {
  schema_version: "CODEX_JOURNAL_CLI_V2", adapter_id: "xrml-codex-frozen-journal", adapter_version: "2",
  contract_digest: hash, contract_sha256: hash, request_sha256: hash, model: "fixture-model", reasoning_effort: "medium",
  runtime_identity: { launcher_sha256: hash, environment_sha256: hash, workspace_sha256: hash },
  argv_shape: ["{explicit_launcher}", "exec", "--model", "fixture-model"], stdin_sha256: hash, stdin_bytes: 1200,
  schema_sha256: hash, timeout_seconds: 90, max_stdout_bytes: 1048576, max_stderr_bytes: 32768, max_final_bytes: 512000,
  automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false,
  tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED",
  runtime_profile: "CODEX_JOURNAL_CLI_V2", model_provider: "xrml_openai", requires_openai_auth: true,
  wire_api: "responses", forced_login_method: "chatgpt", request_max_retries: 0, stream_max_retries: 0, supports_websockets: false,
  authentication: "OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED", endpoint_resolution: "OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED",
  retry_policy: "REQUESTED_ZERO_NOT_LIVE_VERIFIED", xrml_process_relaunch: false, remote_call_count_enforced: false,
  cost_limit_enforced: false, live_execution: "NOT_VERIFIED", billing_route: "CHATGPT_ACCOUNT_NOT_VERIFIED",
  raw_output_encoding: "LAST_MESSAGE_JSON_UTF8_V1", raw_envelope_retained: false,
};
const binding = { surface_id: "codex_cli", adapter_id: "xrml-codex-frozen-journal", adapter_version: "2", command_shape_sha256: bindingHash };
const record = { schema_version: "JOURNAL_AUTHORITY_RECORD_V1", topic_id: "topic", job_id: "job", execution_id: "exec",
  binding, registered_by: "synthetic-issuer", registered_at: at, decision_ref: "e\u0301 synthetic-only", expected_sequence: 0,
  grant: { schema_version: "JOURNAL_DISPATCH_GRANT_V1", authorization_id: "approval-one", actor_id: "owner", contract_digest: hash,
    policy_version: "SYNTHETIC_TEST_POLICY", granted_at: at, expires_at: "2026-10-08T00:05:00Z", max_attempts: 3, max_reserved_output_tokens: 12000,
    proof: { expires_at: "2026-10-08T00:04:00Z", limitations: ["합성 fixture: 실제 Provider 호출 증거 없음"] } } };
function ordered(value: unknown, normalize = false): unknown {
  if (typeof value === "string") return normalize ? value.normalize("NFC") : value;
  if (Array.isArray(value)) return value.map(item => ordered(item, normalize));
  return value && typeof value === "object" ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : 1)
    .map(([key, item]) => [normalize ? key.normalize("NFC") : key, ordered(item, normalize)])) : value;
}
const sha = (value: unknown, normalize = false) => createHash("sha256").update(JSON.stringify(ordered(value, normalize))).digest("hex");
const offer = { authorization_id: "approval-one", record_digest: sha(record, true), record_sha256: sha(record), binding,
  issuer_id: record.registered_by, policy_version: record.grant.policy_version, registered_at: at, expires_at: record.grant.expires_at,
  proof_expires_at: record.grant.proof.expires_at, proof_limitations: record.grant.proof.limitations, max_attempts: 3, max_reserved_output_tokens: 12000 };
const preview = { schema_version: "JOURNAL_APP_APPROVAL_V1", execution_id: "exec", actor_id: "owner", contract_digest: hash,
  observed_sequence: 0, approval_allowed: true, execution_allowed: false, reason: null, offer, manifest };
const success = { authority: { record, revocation: null }, execution: saved };
const updated = vi.fn(), busy = vi.fn(), pending = vi.fn();
const props = { url, saved, onUpdated: updated, onBusy: busy, onPending: pending };
const inspect = () => fireEvent.click(screen.getByRole("button", { name: "Journal 승인 후보 확인" }));
const approve = () => fireEvent.click(screen.getByRole("button", { name: "확인한 Journal 승인 등록" }));
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
async function ready() { inspect(); await screen.findByText(/발급 주체: synthetic-issuer/); }

it("requires inspection then explicit registration and never dispatches or adopts", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(success);
  render(<JournalApproval {...props}/>); expect(api.fetchJson).not.toHaveBeenCalled(); await ready();
  expect(screen.getByText(/합성 fixture: 실제 Provider 호출 증거 없음/)).toBeTruthy();
  expect(screen.getByText(/최대 시도: 3회.*12000/)).toBeTruthy();
  expect(screen.getByText(`승인 원문 SHA-256: ${offer.record_sha256}`)).toBeTruthy();
  expect(posts()).toHaveLength(0); approve();
  await waitFor(() => expect(updated).toHaveBeenCalledWith(saved));
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${url}/codex-approval`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ authorization_id: offer.authorization_id,
    expected_sequence: 0, expected_contract_digest: hash, expected_record_digest: offer.record_digest, expected_record_sha256: offer.record_sha256 });
  expect(screen.getByText(/승인 등록을 확인했습니다.*실행을 시작하지/)).toBeTruthy();
});

it.each(["journal_approval_disabled", "journal_approval_source_unavailable", "journal_approval_not_ready", "journal_dispatch_authority_expired", "journal_dispatch_budget_exhausted"])("explains disabled preview %s without an enabled registration", async reason => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...preview, approval_allowed: false, reason, offer: null, manifest: null });
  render(<JournalApproval {...props}/>); inspect(); await screen.findByRole("status");
  expect(screen.queryByRole("button", { name: "확인한 Journal 승인 등록" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each([401, 403, 404, 409, 422, 400, 410])("clears a definitive HTTP %s rejection and requires reinspection", async status => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new api.ApiError("rejected", status, ""));
  render(<JournalApproval {...props}/>); await ready(); approve(); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "같은 Journal 승인 등록 다시 확인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "확인한 Journal 승인 등록" })).toBeNull();
  expect(pending).toHaveBeenLastCalledWith(false);
});

it.each([408, 429, 500, 503, 0])("preserves exact uncertain body for status %s across disabled source and newer history", async status => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(status ? new api.ApiError("uncertain", status, "") : new Error("lost"))
    .mockResolvedValueOnce({ ...preview, observed_sequence: 1, approval_allowed: false, reason: "journal_approval_disabled", offer: null, manifest: null });
  const view = render(<JournalApproval {...props}/>); await ready(); approve(); await screen.findByRole("alert");
  expect(screen.getByText(`다시 확인할 승인: ${offer.authorization_id}`)).toBeTruthy();
  expect(screen.getByText(`다시 확인할 승인 원문 SHA-256: ${offer.record_sha256}`)).toBeTruthy();
  const next = { ...saved, current: { ...event, sequence: 1, state: "CANCELLED" as const }, events: [{ ...event, sequence: 1, state: "CANCELLED" as const }] };
  view.rerender(<JournalApproval {...props} saved={next}/>); inspect(); await screen.findByText(/이 서버에서는 Journal 승인 등록이 비활성화/);
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...success, execution: next });
  fireEvent.click(screen.getByRole("button", { name: "같은 Journal 승인 등록 다시 확인" }));
  await waitFor(() => expect(updated).toHaveBeenCalledWith(next)); expect(posts()).toHaveLength(2); expect(posts()[1]).toEqual(posts()[0]);
});

it.each(["actor", "contract", "authority", "hash", "unicode", "sequence", "binding"])("retains pending for invalid success %s", async change => {
  const response = structuredClone(success);
  if (change === "actor") response.execution.contract.actor_id = "other";
  else if (change === "contract") response.execution.contract.packet.packet_id = "other";
  else if (change === "authority") response.authority.record.grant.authorization_id = "other";
  else if (change === "hash") response.authority.record.decision_ref = "modified";
  else if (change === "unicode") response.authority.record.decision_ref = record.decision_ref.normalize("NFC");
  else if (change === "sequence") response.authority.record.expected_sequence = 1;
  else response.authority.record.binding.adapter_version = "1";
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(response);
  render(<JournalApproval {...props}/>); await ready(); approve(); await screen.findByRole("alert");
  expect(updated).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "같은 Journal 승인 등록 다시 확인" })).toBeTruthy();
  expect(pending).toHaveBeenLastCalledWith(true);
});

it.each(["execution_id", "actor_id", "observed_sequence", "execution_allowed", "manifest", "offer"])("fails closed on malformed preview %s", async field => {
  const bad = { ...preview, [field]: field === "execution_allowed" ? true : field === "observed_sequence" ? 3 : "invalid" };
  vi.mocked(api.fetchJson).mockResolvedValue(bad); render(<JournalApproval {...props}/>); inspect();
  await screen.findByRole("alert"); expect(screen.queryByRole("button", { name: "확인한 Journal 승인 등록" })).toBeNull();
});

it("keeps pending registration mounted through parent refresh, blocks new dispatch and allows safety control", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, options) => {
    if (options?.method === "POST") throw new Error("lost");
    return path === url ? saved : preview;
  });
  render(<JournalExecutionHistory url={url} onChanged={vi.fn()}/>); await screen.findByText(/최근 저장 상태/);
  await ready(); approve(); await screen.findByRole("alert");
  expect((screen.getByRole("button", { name: "Codex 실행 허용 확인·새로고침" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/최근 저장 상태/);
  expect((screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }) as HTMLButtonElement).disabled).toBe(false);
  expect(screen.getByRole("button", { name: "같은 Journal 승인 등록 다시 확인" })).toBeTruthy();
  expect(posts()).toHaveLength(1);
});

it.each(["dispatch", "approval"])("allows cancellation after lost %s response and a RUNNING refresh while preserving the exact pending body", async kind => {
  const runningEvent = { ...event, sequence: 2, state: "RUNNING" as const, attempt: 1, deadline: "2026-10-08T00:01:00Z" };
  const running: ExecutionDetail = { ...saved, current: runningEvent, events: [runningEvent] };
  const cancelledEvent = { ...runningEvent, sequence: 3, state: "CANCELLED" as const, reason: "result_acceptance_cancelled", deadline: null };
  const cancelled: ExecutionDetail = { ...saved, current: cancelledEvent, events: [runningEvent, cancelledEvent] };
  const dispatchStatus = { schema_version: "CODEX_JOURNAL_DISPATCH_STATUS_V1", enabled: true, reason: null,
    profile: "CODEX_JOURNAL_CLI_V2", execution_id: "exec", actor_id: "owner", contract_digest: hash, observed_sequence: 0,
    manifest, binding, authorities: [{ authorization_id: "existing", record_digest: offer.record_digest, status: "AVAILABLE",
      expires_at: offer.expires_at, issuer: offer.issuer_id, policy_version: offer.policy_version }], authorities_has_more: false };
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, options) => {
    if (options?.method === "POST") {
      if (path.endsWith("/control")) return { execution: cancelled, applied: true };
      throw new Error("response lost");
    }
    if (path === url) return ++reads === 1 ? saved : running;
    return path.endsWith("/codex-approval") ? preview : dispatchStatus;
  });
  render(<JournalExecutionHistory url={url} onChanged={vi.fn()}/>); await screen.findByText(/최근 저장 상태/);
  if (kind === "approval") { await ready(); approve(); }
  else {
    fireEvent.click(screen.getByRole("button", { name: "Codex 실행 허용 확인·새로고침" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "서버 실행 허용 선택" }), { target: { value: "existing" } });
    fireEvent.click(screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" }));
  }
  await screen.findByRole("alert"); const original = posts()[0];
  fireEvent.click(screen.getByRole("button", { name: "실행 이력 새로고침" }));
  await screen.findByText(/최근 저장 상태: 응답 대기 기록/);
  const cancel = screen.getByRole("button", { name: "이 요청의 결과 저장 취소" }) as HTMLButtonElement;
  expect(cancel.disabled).toBe(false);
  expect((screen.getByRole("button", { name: "접수 기한 만료 확인" }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(cancel); await screen.findByText(/최근 저장 상태: 결과 저장 취소/);
  expect(JSON.parse(posts()[1][1]!.body as string)).toMatchObject({ action: "cancel", expected_sequence: 2 });
  const retry = kind === "approval" ? "같은 Journal 승인 등록 다시 확인" : "같은 Codex 실행 요청 다시 보내기";
  fireEvent.click(screen.getByRole("button", { name: retry }));
  await waitFor(() => expect(posts()).toHaveLength(3));
  expect(posts()[2]).toEqual(original);
});

it.each(["scope", "unmount"])("ignores late registration response after %s", async change => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<JournalApproval {...props}/>); await ready(); approve();
  if (change === "unmount") view.unmount();
  else view.rerender(<JournalApproval {...props} saved={{ ...saved, contract: { ...saved.contract, actor_id: "another" } }}/>);
  await act(async () => finish(success)); expect(updated).not.toHaveBeenCalled();
});

it("supports native keyboard registration without any automatic submission", async () => {
  const user = userEvent.setup();
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(success);
  render(<JournalApproval {...props}/>); await user.tab();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Journal 승인 후보 확인" }));
  await user.keyboard("{Enter}"); await screen.findByText(/발급 주체: synthetic-issuer/);
  const button = screen.getByRole("button", { name: "확인한 Journal 승인 등록" });
  for (let step = 0; step < 5 && document.activeElement !== button; step++) await user.tab();
  expect(document.activeElement).toBe(button); expect(posts()).toHaveLength(0);
  await user.keyboard("{Enter}"); await waitFor(() => expect(updated).toHaveBeenCalledWith(saved));
});

it("rejects a late inspection response after sequence advance", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<JournalApproval {...props}/>); inspect();
  view.rerender(<JournalApproval {...props} saved={{ ...saved, current: { ...event, sequence: 2, state: "FAILED" } }}/>);
  await act(async () => finish(preview)); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "확인한 Journal 승인 등록" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("blocks new approval while an uncertain dispatch remains unresolved, with history refresh available", async () => {
  const dispatchStatus = { schema_version: "CODEX_JOURNAL_DISPATCH_STATUS_V1", enabled: true, reason: null,
    profile: "CODEX_JOURNAL_CLI_V2", execution_id: "exec", actor_id: "owner", contract_digest: hash, observed_sequence: 0,
    manifest, binding, authorities: [{ authorization_id: "existing", record_digest: offer.record_digest, status: "AVAILABLE",
      expires_at: offer.expires_at, issuer: offer.issuer_id, policy_version: offer.policy_version }], authorities_has_more: false };
  vi.mocked(api.fetchJson).mockImplementation(async (path, options) => {
    if (options?.method === "POST") throw new Error("lost dispatch response");
    return path === url ? saved : dispatchStatus;
  });
  render(<JournalExecutionHistory url={url} onChanged={vi.fn()}/>); await screen.findByText(/최근 저장 상태/);
  fireEvent.click(screen.getByRole("button", { name: "Codex 실행 허용 확인·새로고침" }));
  fireEvent.change(await screen.findByRole("combobox", { name: "서버 실행 허용 선택" }), { target: { value: "existing" } });
  fireEvent.click(screen.getByRole("button", { name: "선택한 허용으로 Codex 실행 요청" })); await screen.findByRole("alert");
  expect((screen.getByRole("button", { name: "Journal 승인 후보 확인" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "실행 이력 새로고침" }) as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByRole("button", { name: "같은 Codex 실행 요청 다시 보내기" }) as HTMLButtonElement).disabled).toBe(false);
});

it("accepts exact readback of an already revoked record without describing it as available", async () => {
  const revocation = { authorization_id: offer.authorization_id, record_digest: offer.record_digest, actor_id: "owner", at, reason: "withdrawn" };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce({ authority: { record, revocation }, execution: saved });
  render(<JournalApproval {...props}/>); await ready(); approve();
  await screen.findByText(/이미 철회된 기록이며 새 실행을 허용하지/);
  expect(updated).toHaveBeenCalledWith(saved); expect(posts()).toHaveLength(1);
});

it("matches equivalent ISO timestamp rendering while retaining original record bytes", async () => {
  const timezonePreview = { ...preview, offer: { ...offer, registered_at: at.replace("Z", "+00:00"),
    expires_at: offer.expires_at.replace("Z", "+00:00"), proof_expires_at: offer.proof_expires_at.replace("Z", "+00:00") } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(timezonePreview).mockResolvedValueOnce(success);
  render(<JournalApproval {...props}/>); await ready(); approve();
  await waitFor(() => expect(updated).toHaveBeenCalledWith(saved));
});
