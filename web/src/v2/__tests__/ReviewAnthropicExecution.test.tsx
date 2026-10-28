import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { TransportDetail, TransportList } from "../../lib/reviewTransport";
import { ReviewTransport } from "../components/ReviewTransport";
import { ReviewAnthropicExecution } from "../components/ReviewAnthropicExecution";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const guid = "00000000-0000-4000-8000-000000000001", hash = "a".repeat(64), previewHash = "b".repeat(64), contractHash = "c".repeat(64);
const root = "/api/topics/topic/paper-bank/version/review-preparations", preparationId = "prepared-one", base = `${root}/${preparationId}/transport-executions`;
const at = "2026-10-11T00:00:00Z", model = "synthetic-anthropic-structured-model";
const prepared = { schema_version: "PREPARED_REVIEW_REQUEST_V3", provider_name: "anthropic", model, request_sha256: hash, contract_sha256: hash,
  provider_request: { max_output_tokens: 2200, temperature: 0.2, schema_name: "PREPARED_PAPER_REVIEW_DRAFT_V3" }, packet: {
    source_preparation: { preparation_id: preparationId, preparation_sha256: hash, normalization: "XRML_WHITESPACE_V1",
      selection: { start: 12, end: 16, chars: 4, sha256: hash }, source: { run_id: "run", paper_id: "paper", artifact_ref: "run:raw/paper.txt", source_chars: 1000 } },
    evidence_spans: [{ span_id: "selected-span", text: "e\u0301한🙂" }] } };
const manifest = { schema_version: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2", adapter_id: "xrml-anthropic-prepared-review", adapter_version: "2",
  provider_name: "anthropic", model, endpoint: "https://api.anthropic.com/v1/messages", api_version: "2023-06-01", method: "POST",
  execution_surface: "MOCK_TRANSPORT", schema_compiler: "ANTHROPIC_JSON_SCHEMA_V2", shared_schema_context: "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM",
  schema_constraints: "LOCAL_ORIGINAL_VALIDATION", body_sha256: hash, body_bytes: 1400, request_sha256: hash, contract_sha256: hash,
  local_schema_sha256: hash, wire_schema_sha256: hash, effective_system_sha256: hash, timeout_seconds: 60, timeout_semantics: "PER_OPERATION_INACTIVITY", max_response_bytes: 4000000, max_review_output_bytes: 512000,
  stream: false, tools: [], automatic_retry: false, redirects: false, environment_proxy: false,
  temperature_policy: "OMITTED_PROVIDER_DEFAULT", output_token_limit_enforced: false, temperature_enforced: false,
  request_metadata: "LOCAL_BINDING_ONLY_NOT_TRANSMITTED", raw_output_encoding: "CONTENT_TEXT_JSON_UTF8_V1", raw_envelope_retained: false };
const preview = { enabled: true, mode: "ANTHROPIC_STRUCTURED_OFFLINE", reason: null, preview: {
  schema_version: "PAPER_ANTHROPIC_OFFLINE_PREVIEW_V1", preview_sha256: previewHash, preparation_digest: hash, prepared, transport: manifest,
  specification: { provider_name: "anthropic", model, max_output_tokens: 2200 }, input_origin: "FIXTURE_SYNTHETIC", execution: "NOT_STARTED" } };
const event = { sequence: 0, state: "READY" as const, at, deadline: null, outcome_unknown: false, reason: null, receipt_id: null, authority: null };
const specification = { request_id: guid, expected_preparation_digest: hash, acknowledge_unknown: false, provider_name: "anthropic", model, max_output_tokens: 2200 };
const ready: TransportDetail = { contract: { execution_id: "anthropic-one", preparation_id: preparationId, actor_id: "owner", created_at: at,
  mode: "TRUSTED_PAPER_HTTP", input_origin: "FIXTURE_SYNTHETIC", prepared, transport: manifest,
  specification },
  current: event, events: [event], authority: null, contract_sha256: contractHash, offline_eligible: false };
const authority = { authorization_id: "synthetic-approval", record_sha256: hash, issuer_id: "xrml-anthropic-mock-issuer-v1", policy_version: "PAPER_ANTHROPIC_MOCK_ONLY_V1",
  granted_at: at, expires_at: "2999-01-01T00:00:00Z", revoked_at: null, validity: "WITHIN_VALIDITY_WINDOW" as const };
const approvedEvent = { ...event, sequence: 1, state: "APPROVED" as const, deadline: authority.expires_at, authority: { authorization_id: authority.authorization_id, record_sha256: hash }, command: { kind: "control", body: { request_id: guid, expected_sequence: 0, action: "approve" } } };
const approved: TransportDetail = { ...ready, current: approvedEvent, events: [event, approvedEvent], authority };
const leasedEvent = { ...event, sequence: 2, state: "LEASED" as const, deadline: authority.expires_at, command: { kind: "dispatch", body: { request_id: guid, expected_sequence: 1 } } };
const leased: TransportDetail = { ...approved, current: leasedEvent, events: [...approved.events, leasedEvent] };
const receivedEvent = { ...event, sequence: 4, state: "RESULT_RECEIVED" as const };
const received: TransportDetail = { ...leased, current: receivedEvent, events: [...leased.events, receivedEvent] };
const savedEvent = { ...event, sequence: 5, state: "RESULT_SAVED" as const, receipt_id: "input-result" };
const saved: TransportDetail = { ...received, current: savedEvent, events: [...received.events, savedEvent] };
const capabilities = { create: false, approve: false, dispatch: false, cancel: true, recover: true, save_result: true, reason: "paper_review_execution_not_configured", issuer_status: "NOT_CONFIGURED", provider_status: "UNREGISTERED", live_execution: "NOT_VERIFIED" };
const listing: TransportList = { preparation_digest: hash, items: [], page: 1, pageSize: 10, hasMore: false, outcome_unknown_pending: false, capabilities };
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
const onBusy = vi.fn(), onPending = vi.fn(), onUpdated = vi.fn();
const props = { root: base, preparationId, listing, selected: null, onBusy, onPending, onUpdated };
async function open() { fireEvent.click(screen.getByText("검토 실행 조건·이력")); await screen.findByRole("button", { name: "Codex 검토 입력·실행 조건 확인" }); }
async function inspect() {
  const button = screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" });
  // The panel mounts before its parent's initial history request enables actions.
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
  await screen.findByText(/선택 범위: 12–16/);
}
beforeEach(() => { vi.resetAllMocks(); vi.spyOn(crypto, "randomUUID").mockReturnValue(guid); vi.mocked(api.fetchJson).mockImplementation(async url => (url.endsWith("anthropic-preview") ? preview : listing) as never); });

it("waits for initial history readiness before inspecting the synthetic preview", async () => {
  let finishHistory!: (value: TransportList) => void;
  const initialHistory = new Promise<TransportList>(resolve => { finishHistory = resolve; });
  vi.mocked(api.fetchJson).mockImplementation(async url =>
    (url.endsWith("anthropic-preview") ? preview : await initialHistory) as never);
  render(<ReviewTransport root={root} id={preparationId}/>); await open();
  expect(screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" })).toHaveProperty("disabled", true);
  expect(api.fetchJson).toHaveBeenCalledTimes(1); expect(posts()).toHaveLength(0);

  const inspection = inspect();
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url.endsWith("anthropic-preview"))).toHaveLength(0);
  await act(async () => finishHistory(listing));
  await inspection;
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url.endsWith("anthropic-preview"))).toHaveLength(1);
  expect(posts()).toHaveLength(0);
});

it("keeps preview, preparation, synthetic approval and execution separate, then opens the saved receipt", async () => {
  let detail: TransportDetail | null = null;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("anthropic-preview")) return preview as never;
    if (options?.method === "POST") {
      if (url.endsWith("anthropic-requests")) detail = ready;
      else if (url.endsWith("anthropic-offline-approval")) detail = approved;
      else if (url.endsWith("anthropic-offline-dispatch")) detail = leased;
      return detail as never;
    }
    if (url.endsWith("anthropic-one")) { detail = saved; return saved as never; }
    return { ...listing, items: detail ? [detail] : [] } as never;
  });
  const onOpenReceipt = vi.fn(); render(<ReviewTransport root={root} id={preparationId} onOpenReceipt={onOpenReceipt}/>);
  expect(api.fetchJson).not.toHaveBeenCalled(); await open(); expect(posts()).toHaveLength(0);
  await inspect(); expect(posts()).toHaveLength(0); fireEvent.click(screen.getByText("Anthropic 검토에 사용할 고정 본문"));
  expect(screen.getByLabelText("Anthropic 합성 검토 고정 본문").textContent).toBe("e\u0301한🙂");
  fireEvent.click(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" }));
  await screen.findByRole("button", { name: "합성 시험 승인" }); expect(posts()).toHaveLength(1);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: guid, expected_preparation_digest: hash, acknowledge_unknown: false, expected_preview_sha256: previewHash });
  fireEvent.click(screen.getByRole("button", { name: "합성 시험 승인" })); await screen.findByRole("button", { name: "합성 응답 시험 실행" }); expect(posts()).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "합성 응답 시험 실행" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "저장된 HTTP 입력·결과 열기" })).toBeTruthy(), { timeout: 1800 });
  fireEvent.click(screen.getByRole("button", { name: "저장된 HTTP 입력·결과 열기" })); expect(onOpenReceipt).toHaveBeenCalledWith("input-result"); expect(posts()).toHaveLength(3);
});

it("is default denied and does not inspect or create requests on mount", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ enabled: false, mode: "ANTHROPIC_STRUCTURED_OFFLINE", reason: "paper_anthropic_offline_disabled", preview: null });
  render(<ReviewAnthropicExecution {...props}/>); expect(api.fetchJson).not.toHaveBeenCalled(); expect(screen.queryByRole("status")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" })); await screen.findByText(/합성 시험이 비활성화/);
  expect(screen.queryByRole("button", { name: "이 구간으로 합성 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each([
  ["surface", { ...preview.preview, transport: { ...manifest, execution_surface: "NATIVE_HTTP" } }],
  ["digest", { ...preview.preview, preparation_digest: "d".repeat(64) }],
  ["schema", { ...preview.preview, transport: { ...manifest, schema_compiler: "ANTHROPIC_JSON_SCHEMA_V1" } }],
  ["system", { ...preview.preview, transport: { ...manifest, effective_system_sha256: "invalid" } }],
  ["model", { ...preview.preview, prepared: { ...prepared, model: "real-model" } }],
  ["range", { ...preview.preview, prepared: { ...prepared, packet: { ...prepared.packet, source_preparation: { ...prepared.packet.source_preparation, preparation_id: "other" } } } }],
])("rejects a mismatched preview %s without preparing", async (_field, invalid) => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...preview, preview: invalid }); render(<ReviewAnthropicExecution {...props}/>);
  fireEvent.click(screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" })); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "이 구간으로 합성 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("shows preparation success only while its acknowledged execution is selected", async () => {
  const nextId = "00000000-0000-4000-8000-000000000002";
  vi.mocked(crypto.randomUUID).mockReturnValue(nextId);
  const nextReady: TransportDetail = { ...ready, contract: { ...ready.contract, execution_id: "anthropic-two",
    specification: { ...specification, request_id: nextId } } };
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.endsWith("anthropic-preview")) return preview as never;
    if (url.endsWith("anthropic-requests") || url.endsWith("anthropic-two")) return nextReady as never;
    if (url.endsWith("anthropic-one")) return saved as never;
    return { ...listing, items: [saved] } as never;
  });
  render(<ReviewTransport root={root} id={preparationId}/>); await open(); await inspect();
  fireEvent.click(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" }));
  const success = "합성 요청 준비를 저장했습니다. 승인과 실행은 아직 수행하지 않았습니다.";
  await screen.findByText(success);
  expect(screen.getByText("선택한 합성 요청: anthropic-two")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "HTTP 실행 기록 열기 · anthropic-one" }));
  await screen.findByText("선택한 합성 요청: anthropic-one");
  expect(screen.queryByText(success)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "HTTP 실행 기록 열기 · anthropic-two" }));
  await screen.findByText("선택한 합성 요청: anthropic-two");
  expect(screen.getByText(success)).toBeTruthy();
});

it("keeps a lost preparation response pending and freezes its command across parent refresh", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new Error("lost")).mockResolvedValueOnce(ready);
  const view = render(<ReviewAnthropicExecution {...props}/>); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" })); await screen.findByRole("alert");
  expect(onPending).toHaveBeenLastCalledWith(true, undefined); expect(posts()).toHaveLength(1);
  view.rerender(<ReviewAnthropicExecution {...props} listing={{ ...listing, preparation_digest: "d".repeat(64), items: [ready] }} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "같은 합성 준비 요청 다시 보내기" })); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(ready));
  expect(posts()[0]).toEqual(posts()[1]); expect(onPending).toHaveBeenLastCalledWith(false, undefined);
});

it("sends only one preparation while its response is pending", async () => {
  let finish!: (value: unknown) => void;
  const pending = new Promise(resolve => { finish = resolve; });
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(pending);
  render(<ReviewAnthropicExecution {...props}/>); await inspect();
  const prepare = screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" }); fireEvent.click(prepare); fireEvent.click(prepare);
  expect(posts()).toHaveLength(1); expect(screen.getByRole("button", { name: "같은 합성 준비 요청 다시 보내기" })).toHaveProperty("disabled", true);
  await act(async () => finish(ready)); expect(onUpdated).toHaveBeenCalledTimes(1);
});

it("keeps approval and dispatch replays bound to exact frozen execution and sequence", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new Error("lost")).mockResolvedValueOnce(approved);
  const view = render(<ReviewAnthropicExecution {...props} listing={{ ...listing, items: [ready] }} selected={ready}/>); await inspect();
  fireEvent.click(screen.getByRole("button", { name: "합성 시험 승인" })); await screen.findByRole("alert");
  view.rerender(<ReviewAnthropicExecution {...props} listing={{ ...listing, items: [approved] }} selected={approved}/>);
  fireEvent.click(screen.getByRole("button", { name: "같은 합성 승인 요청 다시 보내기" })); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(approved));
  expect(posts()[0]).toEqual(posts()[1]); expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: guid, expected_sequence: 0, expected_contract_sha256: contractHash });
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("lost")).mockResolvedValueOnce(leased);
  fireEvent.click(screen.getByRole("button", { name: "합성 응답 시험 실행" })); await screen.findByRole("alert");
  view.rerender(<ReviewAnthropicExecution {...props} selected={leased}/>);
  fireEvent.click(screen.getByRole("button", { name: "같은 합성 실행 요청 다시 보내기" })); await waitFor(() => expect(onUpdated).toHaveBeenLastCalledWith(leased));
  expect(posts()[2]).toEqual(posts()[3]); expect(JSON.parse(posts()[2][1]!.body as string)).toEqual({ request_id: guid, expected_sequence: 1 });
});

it.each(["actor", "command", "surface", "contract"])("does not acknowledge an unrelated success %s", async field => {
  const wrong = structuredClone(approved);
  if (field === "actor") wrong.contract.actor_id = "other";
  if (field === "command") wrong.events[1].command = { kind: "control", body: { request_id: "other", expected_sequence: 0, action: "approve" } };
  if (field === "surface") wrong.contract.transport.execution_surface = "NATIVE_HTTP";
  if (field === "contract") wrong.contract_sha256 = "d".repeat(64);
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(wrong);
  render(<ReviewAnthropicExecution {...props} selected={ready}/>); await inspect(); fireEvent.click(screen.getByRole("button", { name: "합성 시험 승인" }));
  await screen.findByRole("alert"); expect(onUpdated).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "같은 합성 승인 요청 다시 보내기" })).toBeTruthy();
});

it("requires explicit reinspection after a definitive changed-preview rejection", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new api.ApiError("preview changed", 409, ""));
  render(<ReviewAnthropicExecution {...props}/>); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" }));
  await screen.findByRole("alert"); expect(screen.queryByRole("button", { name: "같은 합성 준비 요청 다시 보내기" })).toBeNull();
  expect(screen.queryByRole("button", { name: "이 구간으로 합성 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(1);
});

it("never exposes synthetic execution actions for native V2 history", async () => {
  const native = { ...approved, contract: { ...approved.contract, transport: { ...manifest, execution_surface: "NATIVE_HTTP" } } };
  render(<ReviewAnthropicExecution {...props} selected={native}/>); await inspect();
  expect(screen.queryByRole("button", { name: "합성 시험 승인" })).toBeNull(); expect(screen.queryByRole("button", { name: "합성 응답 시험 실행" })).toBeNull();
  expect(screen.queryByRole("button", { name: "합성 시험 승인 철회" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("shows native V2 compiler provenance without offering mock actions or Responses-only claims", async () => {
  const native = { ...saved, contract: { ...saved.contract, input_origin: "PROVIDER_RESPONSE", transport: { ...manifest, execution_surface: "NATIVE_HTTP" } } };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing, items: [native] });
  render(<ReviewTransport root={root} id={preparationId}/>); await open();
  await screen.findByText(/Messages V2 구조화 계약/); expect(screen.getByText(/총 실행 시간 상한이 아닙니다/)).toBeTruthy();
  expect(screen.queryByText(/백그라운드/)).toBeNull(); expect(screen.queryByText(/MockTransport 합성 응답 기록/)).toBeNull();
  expect(screen.queryByRole("button", { name: "합성 응답 시험 실행" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("does not treat a different issuer's mock record as an executable synthetic request", async () => {
  render(<ReviewAnthropicExecution {...props} selected={{ ...approved, authority: { ...authority, issuer_id: "another-issuer" } }}/>); await inspect();
  expect(screen.queryByRole("button", { name: "합성 응답 시험 실행" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each(["EXPIRED", "REVOKED", "NOT_YET_VALID"])("cannot dispatch an authority with %s validity", async validity => {
  render(<ReviewAnthropicExecution {...props} selected={{ ...approved, authority: { ...authority, validity: validity as typeof authority.validity } }}/>); await inspect();
  expect(screen.getByRole("button", { name: "합성 응답 시험 실행" })).toHaveProperty("disabled", true); expect(posts()).toHaveLength(0);
});

it("rechecks the deadline at the manual dispatch click", async () => {
  const near = { ...approved, current: { ...approved.current, deadline: new Date(Date.now() + 60000).toISOString() } };
  render(<ReviewAnthropicExecution {...props} selected={near}/>); await inspect();
  expect(screen.getByRole("button", { name: "합성 응답 시험 실행" })).toHaveProperty("disabled", false);
  const now = vi.spyOn(Date, "now").mockReturnValue(Date.parse(near.current.deadline!) + 1);
  try { fireEvent.click(screen.getByRole("button", { name: "합성 응답 시험 실행" }));
    expect(screen.getByRole("alert").textContent).toMatch(/유효 시간이 지났습니다/); expect(posts()).toHaveLength(0);
  } finally { now.mockRestore(); }
});

it("records explicit revocation without hiding previously saved results", async () => {
  const revoked = { ...saved, authority: { ...authority, validity: "REVOKED", revoked_at: at } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(revoked);
  render(<ReviewAnthropicExecution {...props} selected={saved}/>); await inspect(); fireEvent.click(screen.getByRole("button", { name: "합성 시험 승인 철회" }));
  await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(revoked)); expect(posts()[0][0]).toBe(`${base}/anthropic-one/anthropic-offline-revocation`);
  expect(screen.getByText(/합성 시험 승인 철회를 확인했습니다/)).toBeTruthy();
});

it("requires unknown-outcome acknowledgement and prevents a second active request", async () => {
  const view = render(<ReviewAnthropicExecution {...props} listing={{ ...listing, outcome_unknown_pending: true }}/>); await inspect();
  expect(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("checkbox", { name: /이전 결과가 불명확함/ }));
  expect(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" })).toHaveProperty("disabled", false);
  view.rerender(<ReviewAnthropicExecution {...props} listing={{ ...listing, items: [ready], outcome_unknown_pending: true }} selected={ready}/>);
  expect(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" })).toHaveProperty("disabled", true); expect(posts()).toHaveLength(0);
});

it.each(["read", "save"])("ignores a late %s response after preparation scope changes", async phase => {
  let finish!: (value: unknown) => void;
  const pending = new Promise(resolve => { finish = resolve; });
  if (phase === "read") vi.mocked(api.fetchJson).mockResolvedValueOnce(pending); else vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(pending);
  const view = render(<ReviewAnthropicExecution {...props}/>);
  if (phase === "read") fireEvent.click(screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" }));
  else { await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" })); }
  view.rerender(<ReviewAnthropicExecution {...props} root={`${base}-other`} preparationId="other"/>);
  await act(async () => finish(phase === "read" ? preview : ready)); expect(onUpdated).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "이 구간으로 합성 요청 준비" })).toBeNull(); expect(onPending).toHaveBeenLastCalledWith(false);
});

it("locks parent selection and other preparation paths while retaining same-request recovery after a lost response", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("anthropic-preview")) return preview as never;
    if (options?.method === "POST") throw new Error("lost");
    return { ...listing, items: [ready] } as never;
  });
  render(<ReviewTransport root={root} id={preparationId}/>); await open(); await inspect();
  fireEvent.click(screen.getByRole("button", { name: "합성 시험 승인" })); await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: /HTTP 실행 기록 열기 ·/ })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })).toHaveProperty("disabled", false);
  expect(screen.getByRole("button", { name: "이 HTTP 요청 취소" })).toHaveProperty("disabled", false);
  expect(screen.getByRole("button", { name: "Codex 검토 입력·실행 조건 확인" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "같은 합성 승인 요청 다시 보내기" })).toHaveProperty("disabled", false);
});

it("reads and cancels the current request after a lost dispatch response, then replays the original command", async () => {
  const dispatchId = "00000000-0000-4000-8000-000000000002", cancelId = "00000000-0000-4000-8000-000000000003";
  vi.mocked(crypto.randomUUID).mockReturnValueOnce(dispatchId).mockReturnValueOnce(cancelId);
  const acceptedDispatch = { ...leasedEvent, command: { kind: "dispatch", body: { request_id: dispatchId, expected_sequence: 1 } } };
  const runningEvent = { ...acceptedDispatch, state: "RUNNING" as const, sequence: 3, command: null, outcome_unknown: true };
  const running: TransportDetail = { ...approved, current: runningEvent, events: [...approved.events, acceptedDispatch, runningEvent] };
  let current = approved, dispatches = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("anthropic-preview")) return preview as never;
    if (url.endsWith("anthropic-offline-dispatch")) {
      if (++dispatches === 1) { current = running; throw new Error("response lost after launch"); }
      return current as never;
    }
    if (url.endsWith("controls")) {
      const body = JSON.parse(options!.body as string);
      const cancelledEvent = { ...runningEvent, sequence: 4, state: "CANCELLED" as const, command: { kind: "control", body }, deadline: null };
      current = { ...current, current: cancelledEvent, events: [...current.events, cancelledEvent] }; return current as never;
    }
    if (url.endsWith("anthropic-one")) return current as never;
    return { ...listing, items: [current] } as never;
  });
  render(<ReviewTransport root={root} id={preparationId}/>); await open(); await inspect();
  fireEvent.click(screen.getByRole("button", { name: "합성 응답 시험 실행" })); await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "HTTP 실행 기록 열기 · anthropic-one" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  await screen.findByText(/마지막 저장 상태: 시도 시작 기록됨/);
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url.includes("?page="))).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "이 HTTP 요청 취소" })); await screen.findByText(/마지막 저장 상태: 취소됨/);
  const cancel = posts().find(([url]) => url.endsWith("controls")); expect(JSON.parse(cancel![1]!.body as string).expected_sequence).toBe(3);
  fireEvent.click(screen.getByRole("button", { name: "같은 합성 실행 요청 다시 보내기" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "같은 합성 실행 요청 다시 보내기" })).toBeNull());
  const dispatch = posts().filter(([url]) => url.endsWith("anthropic-offline-dispatch")); expect(dispatch).toHaveLength(2); expect(dispatch[1]).toEqual(dispatch[0]);
  expect(screen.getByText(/마지막 저장 상태: 취소됨/)).toBeTruthy();
});

it.each(["identity", "sequence"])("retains the same request and both pending commands after a mismatched local recovery %s ACK", async field => {
  const dispatchId = "00000000-0000-4000-8000-000000000002", cancelId = "00000000-0000-4000-8000-000000000003";
  vi.mocked(crypto.randomUUID).mockReturnValueOnce(dispatchId).mockReturnValueOnce(cancelId);
  const acceptedDispatch = { ...leasedEvent, command: { kind: "dispatch", body: { request_id: dispatchId, expected_sequence: 1 } } };
  const runningEvent = { ...acceptedDispatch, state: "RUNNING" as const, sequence: 3, command: null, outcome_unknown: true };
  const running: TransportDetail = { ...approved, current: runningEvent, events: [...approved.events, acceptedDispatch, runningEvent] };
  let current = approved, dispatches = 0, cancellations = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("anthropic-preview")) return preview as never;
    if (url.endsWith("anthropic-offline-dispatch")) {
      if (++dispatches === 1) { current = running; throw new Error("response lost after launch"); }
      return current as never;
    }
    if (url.endsWith("controls")) {
      const body = JSON.parse(options!.body as string);
      const cancelledEvent = { ...runningEvent, sequence: 4, state: "CANCELLED" as const, command: { kind: "control", body }, deadline: null };
      current = { ...running, current: cancelledEvent, events: [...running.events, cancelledEvent] };
      if (++cancellations === 1) {
        const wrong = structuredClone(current);
        if (field === "identity") wrong.contract.execution_id = "another-execution";
        else wrong.current.sequence = 2;
        return wrong as never;
      }
      return current as never;
    }
    if (url.endsWith("anthropic-one")) return current as never;
    return { ...listing, items: [current] } as never;
  });
  render(<ReviewTransport root={root} id={preparationId}/>); await open(); await inspect();
  fireEvent.click(screen.getByRole("button", { name: "합성 응답 시험 실행" })); await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })); await screen.findByText(/마지막 저장 상태: 시도 시작 기록됨/);
  fireEvent.click(screen.getByRole("button", { name: "이 HTTP 요청 취소" }));
  const retry = await screen.findByRole("button", { name: "같은 HTTP 관리 요청 다시 보내기" });
  expect(screen.getByText(/마지막 저장 상태: 시도 시작 기록됨/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "HTTP 실행 기록 열기 · another-execution" })).toBeNull();
  expect(screen.getByRole("button", { name: "같은 합성 실행 요청 다시 보내기" })).toHaveProperty("disabled", true);
  fireEvent.click(retry); await screen.findByText(/마지막 저장 상태: 취소됨/);
  const cancels = posts().filter(([url]) => url.endsWith("controls")); expect(cancels).toHaveLength(2); expect(cancels[1]).toEqual(cancels[0]);
  fireEvent.click(screen.getByRole("button", { name: "같은 합성 실행 요청 다시 보내기" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "같은 합성 실행 요청 다시 보내기" })).toBeNull());
  const dispatch = posts().filter(([url]) => url.endsWith("anthropic-offline-dispatch")); expect(dispatch).toHaveLength(2); expect(dispatch[1]).toEqual(dispatch[0]);
});

it("continues GET polling from received to saved without automatically executing again", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...listing, items: [received] }).mockResolvedValueOnce(saved);
  render(<ReviewTransport root={root} id={preparationId} onOpenReceipt={vi.fn()}/>); await open();
  await screen.findByRole("button", { name: "HTTP 수신 결과 저장 다시 시도" });
  await waitFor(() => expect(screen.getByRole("button", { name: "저장된 HTTP 입력·결과 열기" })).toBeTruthy(), { timeout: 1800 });
  expect(posts()).toHaveLength(0); expect(vi.mocked(api.fetchJson).mock.calls[1][0]).toBe(`${base}/anthropic-one`);
});

it("supports keyboard preview and preparation with explicit actions only", async () => {
  const user = userEvent.setup(); vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(ready);
  render(<ReviewAnthropicExecution {...props}/>); await user.tab(); await user.keyboard("{Enter}"); await screen.findByText(/선택 범위: 12–16/);
  const target = screen.getByRole("button", { name: "이 구간으로 합성 요청 준비" });
  for (let step = 0; step < 5 && document.activeElement !== target; step++) await user.tab();
  expect(document.activeElement).toBe(target); await user.keyboard(" "); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(ready));
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(`${base}/anthropic-requests`);
});
