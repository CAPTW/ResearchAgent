import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { TransportDetail, TransportList } from "../../lib/reviewTransport";
import { readAnthropicNativeApproved, readAnthropicNativeDispatched, readAnthropicNativeRevoked, readAnthropicNativeStatus } from "../../lib/reviewAnthropicNative";
import { ReviewTransport } from "../components/ReviewTransport";
import { ReviewAnthropicNative } from "../components/ReviewAnthropicNative";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/paper-bank/version/review-preparations", preparationId = "prepared-one";
const base = `${root}/${preparationId}/transport-executions`, guid = "00000000-0000-4000-8000-000000000001";
const hash = "a".repeat(64), bindingHash = "b".repeat(64), contractHash = "c".repeat(64), at = "2026-10-13T00:00:00Z", model = "fixture-native-model";
const prepared = { schema_version: "PREPARED_REVIEW_REQUEST_V3", provider_name: "anthropic", model, request_sha256: hash, contract_sha256: hash,
  provider_request: { max_output_tokens: 2200, temperature: 0.2 }, packet: {
    source_preparation: { preparation_id: preparationId, preparation_sha256: hash, normalization: "XRML_WHITESPACE_V1",
      selection: { start: 12, end: 16, chars: 4, sha256: hash }, source: { run_id: "run", paper_id: "paper", artifact_ref: "run:raw/paper.txt", source_chars: 1000 } },
    evidence_spans: [{ span_id: "span", text: "e\u0301한🙂" }] } };
const manifest = { schema_version: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2", adapter_id: "xrml-anthropic-prepared-review", adapter_version: "2",
  provider_name: "anthropic", model, endpoint: "https://api.anthropic.com/v1/messages", api_version: "2023-06-01", method: "POST",
  execution_surface: "NATIVE_HTTP", schema_compiler: "ANTHROPIC_JSON_SCHEMA_V2", shared_schema_context: "ORIGINAL_LOCAL_SCHEMA_IN_SYSTEM",
  schema_constraints: "LOCAL_ORIGINAL_VALIDATION", body_sha256: hash, body_bytes: 1400, request_sha256: hash, contract_sha256: "e".repeat(64),
  local_schema_sha256: hash, wire_schema_sha256: hash, effective_system_sha256: hash, timeout_seconds: 60, timeout_semantics: "PER_OPERATION_INACTIVITY",
  max_response_bytes: 4000000, max_review_output_bytes: 512000, stream: false, tools: [], automatic_retry: false, redirects: false, environment_proxy: false,
  temperature_policy: "OMITTED_PROVIDER_DEFAULT", output_token_limit_enforced: false, temperature_enforced: false,
  raw_output_encoding: "CONTENT_TEXT_JSON_UTF8_V1", raw_envelope_retained: false, credential_binding: { connector_id: "anthropic_api", revision: 1, storage: "LOCAL_DPAPI" } };
const preview = { schema_version: "ANTHROPIC_PAPER_NATIVE_PREVIEW_V1", enabled: true, reason: null, actor_id: "owner", preparation_id: preparationId,
  preparation_digest: hash, profile: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2", binding_sha256: bindingHash, prepared, manifest, input_origin: "PROVIDER_RESPONSE", execution_allowed: false };
const event = { sequence: 0, state: "READY" as const, at, deadline: null, outcome_unknown: false, reason: null, receipt_id: null, authority: null };
const specification = { request_id: guid, expected_preparation_digest: hash, acknowledge_unknown: false, provider_name: "anthropic", model, max_output_tokens: 2200 };
type NativeDetail = TransportDetail & { binding_sha256: string };
const ready: NativeDetail = { contract: { execution_id: "native-one", preparation_id: preparationId, actor_id: "owner", created_at: at,
  mode: "TRUSTED_PAPER_HTTP", input_origin: "PROVIDER_RESPONSE", prepared, transport: manifest, specification }, current: event, events: [event],
  authority: null, contract_sha256: contractHash, binding_sha256: bindingHash, offline_eligible: false };
const authority = { authorization_id: "native-server-approval", record_sha256: hash, issuer_id: "trusted-server", policy_version: "bounded-native-v1",
  granted_at: at, expires_at: "2999-01-01T00:00:00Z", revoked_at: null, validity: "WITHIN_VALIDITY_WINDOW" as const };
const offer = { authorization_id: authority.authorization_id, record_sha256: hash, issuer_id: authority.issuer_id, policy_version: authority.policy_version,
  input_origin: "PROVIDER_RESPONSE" as const, expires_at: authority.expires_at, max_attempts: 1, reserved_output_tokens: 2200 };
const approval = { schema_version: "PAPER_APP_APPROVAL_V1", execution_id: "native-one", actor_id: "owner", contract_sha256: contractHash,
  observed_sequence: 0, approval_allowed: true, execution_allowed: false, reason: null, offer };
const approvedEvent = { ...event, sequence: 1, state: "APPROVED" as const, deadline: authority.expires_at,
  authority: { authorization_id: authority.authorization_id, record_sha256: hash }, command: { kind: "control", body: { request_id: guid, expected_sequence: 0, action: "approve" } } };
const approved: NativeDetail = { ...ready, current: approvedEvent, events: [event, approvedEvent], authority };
const leasedEvent = { ...event, sequence: 2, state: "LEASED" as const, deadline: authority.expires_at, command: { kind: "dispatch", body: { request_id: guid, expected_sequence: 1 } } };
const leased: NativeDetail = { ...approved, current: leasedEvent, events: [...approved.events, leasedEvent] };
const receivedEvent = { ...event, sequence: 3, state: "RESULT_RECEIVED" as const };
const received: NativeDetail = { ...leased, current: receivedEvent, events: [...leased.events, receivedEvent] };
const savedEvent = { ...event, sequence: 4, state: "RESULT_SAVED" as const, receipt_id: "native-receipt" };
const saved: NativeDetail = { ...received, current: savedEvent, events: [...received.events, savedEvent] };
const status = { schema_version: "ANTHROPIC_PAPER_NATIVE_DISPATCH_STATUS_V1", enabled: true, reason: null, actor_id: "owner", preparation_id: preparationId,
  profile: "ANTHROPIC_PREPARED_REVIEW_WIRE_V2", execution_id: "native-one", observed_sequence: 1, contract_sha256: contractHash,
  binding_sha256: bindingHash, input_origin: "PROVIDER_RESPONSE", authority, manifest };
const listing: TransportList = { preparation_digest: hash, items: [], page: 1, pageSize: 10, hasMore: false, outcome_unknown_pending: false,
  capabilities: { create: false, approve: false, dispatch: false, cancel: true, recover: true, save_result: true,
    reason: "paper_review_execution_not_configured", issuer_status: "NOT_CONFIGURED", provider_status: "UNREGISTERED", live_execution: "NOT_VERIFIED" } };
const onBusy = vi.fn(), onPending = vi.fn(), onUpdated = vi.fn();
const props = { root: base, preparationId, listing, selected: null, onBusy, onPending, onUpdated };
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
async function open() { fireEvent.click(screen.getByText("검토 실행 조건·이력")); await waitFor(() => expect(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" })).toHaveProperty("disabled", false)); }
async function inspect() { fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" })); await screen.findByText(/전송할 선택 범위: 12–16/); }
async function inspectApproval() { fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" })); await screen.findByRole("button", { name: "확인한 Anthropic Native 승인 등록" }); }
async function inspectStatus() { fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 실행 조건 확인" })); await screen.findByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" }); }
beforeEach(() => { vi.resetAllMocks(); vi.spyOn(crypto, "randomUUID").mockReturnValue(guid); vi.mocked(api.fetchJson).mockResolvedValue(listing); });

it("provides a separate explicit Native Anthropic inspection without any automatic native request", async () => {
  render(<ReviewTransport root={root} id={preparationId}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("검토 실행 조건·이력"));
  await screen.findByRole("button", { name: "Anthropic Native 전송 조건 확인" });
  expect(screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" })).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url.includes("anthropic-native"))).toHaveLength(0);
});

const decisionHash = "f".repeat(64), firstOutputLabel = "첫 출력 실행 승인 — 이 요청의 출력 검증 실적이 아닙니다";
const firstAuthority = { ...authority, authorization_kind: "OWNER_FIRST_OUTPUT" as const, decision_sha256: decisionHash };
const firstOffer = { ...offer, authorization_kind: "OWNER_FIRST_OUTPUT" as const, decision_sha256: decisionHash };
const firstApproved = { ...approved, authority: firstAuthority };

const localDecisionOffer = { ...approval, approval_allowed: false, reason: "paper_approval_source_unavailable", offer: null,
  local_owner_decision_allowed: true, local_owner_binding_sha256: bindingHash };
const localDecisionBody = { request_id: guid, expected_sequence: 0, expected_contract_sha256: contractHash, expected_binding_sha256: bindingHash };
const localDecisionReceipt = { schema_version: "PAPER_LOCAL_ANTHROPIC_DECISION_RECEIPT_V1", execution_id: ready.contract.execution_id,
  actor_id: ready.contract.actor_id, contract_sha256: contractHash, binding_sha256: bindingHash, command: localDecisionBody,
  decision_sha256: decisionHash, record_sha256: hash, created_at: at, execution_allowed: false };
it("requires explicit local owner consent before creating a decision and never automatically registers or executes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(localDecisionOffer).mockResolvedValueOnce(localDecisionReceipt);
  render(<ReviewAnthropicNative {...props} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" }));
  const button = await screen.findByRole("button", { name: "이 요청의 외부 전송·1회 실행 승인 기록 만들기" });
  expect(button).toHaveProperty("disabled", true); expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("checkbox", { name: /고정된 본문과 요청 모델/ }));
  button.focus(); await userEvent.keyboard("{Enter}");
  await screen.findByText(/로컬 사용자의 승인 결정을 저장했습니다/);
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(`${base}/native-one/anthropic-native-owner-decision`);
  expect(JSON.parse(String(posts()[0][1]?.body))).toEqual(localDecisionBody);
  expect(onUpdated).not.toHaveBeenCalled(); expect(screen.queryByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" })).toBeNull();
});

it("preserves the exact local owner command after a lost response until explicit retry", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(localDecisionOffer).mockRejectedValueOnce(new Error("lost"))
    .mockResolvedValueOnce(localDecisionReceipt);
  render(<ReviewAnthropicNative {...props} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" }));
  await screen.findByRole("button", { name: "이 요청의 외부 전송·1회 실행 승인 기록 만들기" });
  fireEvent.click(screen.getByRole("checkbox", { name: /고정된 본문과 요청 모델/ }));
  fireEvent.click(screen.getByRole("button", { name: "이 요청의 외부 전송·1회 실행 승인 기록 만들기" }));
  const retry = await screen.findByRole("button", { name: "같은 Anthropic Native 승인 결정 요청 다시 확인" });
  expect(posts()).toHaveLength(1); fireEvent.click(retry);
  await screen.findByText(/로컬 사용자의 승인 결정을 저장했습니다/);
  expect(posts()).toHaveLength(2); expect(posts()[1][1]?.body).toBe(posts()[0][1]?.body);
});

it.each(["execution_id", "binding_sha256", "contract_sha256"])("retains a local owner pending command for a mismatched %s receipt", async field => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(localDecisionOffer).mockResolvedValueOnce({ ...localDecisionReceipt, [field]: "wrong" });
  render(<ReviewAnthropicNative {...props} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" }));
  await screen.findByRole("button", { name: "이 요청의 외부 전송·1회 실행 승인 기록 만들기" });
  fireEvent.click(screen.getByRole("checkbox", { name: /고정된 본문과 요청 모델/ }));
  fireEvent.click(screen.getByRole("button", { name: "이 요청의 외부 전송·1회 실행 승인 기록 만들기" }));
  await screen.findByRole("button", { name: "같은 Anthropic Native 승인 결정 요청 다시 확인" });
  expect(screen.queryByText(/로컬 사용자의 승인 결정을 저장했습니다/)).toBeNull(); expect(onUpdated).not.toHaveBeenCalled();
});

it.each([
  { local_owner_decision_allowed: true, local_owner_binding_sha256: undefined },
  { local_owner_decision_allowed: "true", local_owner_binding_sha256: bindingHash },
  { local_owner_decision_allowed: true, local_owner_binding_sha256: "f".repeat(64) },
])("rejects an incomplete or mismatched local decision capability %j", async changed => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...localDecisionOffer, ...changed });
  render(<ReviewAnthropicNative {...props} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "이 요청의 외부 전송·1회 실행 승인 기록 만들기" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("accepts a frozen V4 document budget and refuses a model mismatch", async () => {
  const document = { ...prepared, schema_version: "PREPARED_REVIEW_REQUEST_V4", packet: { ...prepared.packet, packet_version: "ai-review-packet-v5",
    source_preparation: { ...prepared.packet.source_preparation, schema_version: "REVIEW_PREPARATION_BINDING_V3",
      analysis_unit: { schema_version: "PAPER_ANALYSIS_UNIT_V1", plan_id: "rplan_" + hash, plan_sha256: hash, unit_index: 0, unit_count: 1,
        budget: { provider_name: "anthropic", model, max_output_tokens: 2200, input_tokens: 10000, context_tokens: 16000, safety_tokens: 2000,
          estimator: "UTF8_BYTES_CONSERVATIVE_V1" } } } } };
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...preview, prepared: document }).mockResolvedValueOnce({ ...preview, prepared: {
    ...document, packet: { ...document.packet, source_preparation: { ...document.packet.source_preparation,
      analysis_unit: { ...document.packet.source_preparation.analysis_unit, budget: { ...document.packet.source_preparation.analysis_unit.budget, model: "different" } } } } } });
  render(<ReviewAnthropicNative {...props}/>); await inspect();
  expect(screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })).toHaveProperty("disabled", false);
  fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" })); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("distinguishes a first-output offer and registered history without implying an already verified output", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...approval, offer: firstOffer }).mockResolvedValueOnce(firstApproved);
  const view = render(<ReviewAnthropicNative {...props} selected={ready}/>); await inspectApproval();
  expect(screen.getByText(firstOutputLabel)).toBeTruthy(); expect(screen.getByText(`결정 SHA-256: ${decisionHash}`)).toBeTruthy(); expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "확인한 Anthropic Native 승인 등록" })); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(firstApproved));
  view.rerender(<ReviewAnthropicNative {...props} selected={firstApproved}/>);
  expect(screen.getByText(firstOutputLabel)).toBeTruthy(); expect(screen.getByText(`결정 SHA-256: ${decisionHash}`)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" })).toBeNull(); expect(posts()).toHaveLength(1);
});

it("keeps legacy approval histories unmarked", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(approval); const view = render(<ReviewAnthropicNative {...props} selected={ready}/>); await inspectApproval();
  expect(screen.queryByText(firstOutputLabel)).toBeNull(); expect(screen.getByRole("button", { name: "확인한 Anthropic Native 승인 등록" })).toHaveProperty("disabled", false);
  view.rerender(<ReviewAnthropicNative {...props} selected={approved}/>); expect(screen.queryByText(firstOutputLabel)).toBeNull();
});

const invalidMarkers: [string, Record<string, unknown>][] = [
  ["missing decision", { authorization_kind: "OWNER_FIRST_OUTPUT" }],
  ["missing kind", { decision_sha256: decisionHash }],
  ["unknown kind", { authorization_kind: "PREVIOUS_SUCCESS", decision_sha256: decisionHash }],
  ["invalid digest", { authorization_kind: "OWNER_FIRST_OUTPUT", decision_sha256: "invalid" }],
  ["null marker", { authorization_kind: null, decision_sha256: null }],
];
it.each(invalidMarkers)("rejects a partial or invalid first-output offer: %s", async (_name, marker) => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...approval, offer: { ...offer, ...marker } }); render(<ReviewAnthropicNative {...props} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" })); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "확인한 Anthropic Native 승인 등록" })).toBeNull(); expect(posts()).toHaveLength(0);
});
it.each(invalidMarkers)("does not enable Native controls from invalid stored authorization metadata: %s", (_name, marker) => {
  render(<ReviewAnthropicNative {...props} selected={{ ...approved, authority: { ...authority, ...marker } }}/>);
  expect(screen.queryByRole("button", { name: "선택한 Native 요청의 실행 조건 확인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Anthropic Native 승인 철회" })).toBeNull(); expect(api.fetchJson).not.toHaveBeenCalled();
});

it.each(["removed", "changed"])("rejects %s first-output decision binding across approval, status, dispatch and revocation ACKs", change => {
  const altered = change === "removed" ? authority : { ...firstAuthority, decision_sha256: "d".repeat(64) };
  const body = { request_id: guid, expected_sequence: 0, expected_contract_sha256: contractHash, expected_authority_sha256: hash };
  expect(readAnthropicNativeApproved({ ...approved, authority: altered }, ready, body, firstOffer)).toBeNull();
  expect(readAnthropicNativeStatus({ ...status, authority: altered }, firstApproved)).toBeNull();
  expect(readAnthropicNativeDispatched({ ...leased, authority: altered }, firstApproved, { ...body, expected_sequence: 1 })).toBeNull();
  expect(readAnthropicNativeRevoked({ ...saved, authority: { ...altered, validity: "REVOKED", revoked_at: at } },
    { ...saved, authority: firstAuthority }, { ...body, expected_sequence: 4 })).toBeNull();
});

it("keeps Native preview, preparation, trusted approval and dispatch separate, then reads the saved receipt", async () => {
  let current: NativeDetail | null = null;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("anthropic-native-preview")) return preview as never;
    if (options?.method === "POST") {
      current = url.endsWith("requests") ? ready : url.endsWith("approval") ? approved : leased; return current as never;
    }
    if (url.endsWith("anthropic-native-approval")) return approval as never;
    if (url.endsWith("anthropic-native-status")) return status as never;
    if (url.endsWith("native-one")) { current = saved; return saved as never; }
    return { ...listing, items: current ? [current] : [] } as never;
  });
  const onOpenReceipt = vi.fn(); render(<ReviewTransport root={root} id={preparationId} onOpenReceipt={onOpenReceipt}/>);
  await open(); await inspect(); expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByText("Anthropic Native로 전송할 고정 본문")); expect(screen.getByLabelText("Anthropic Native 전송 본문").textContent).toBe("e\u0301한🙂");
  expect(screen.getByText(/저장 키: anthropic_api · revision 1/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })); await screen.findByText("선택한 Native 요청: native-one · 고정 저장 키 revision 1");
  expect(posts()).toHaveLength(1); await inspectApproval(); expect(posts()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "확인한 Anthropic Native 승인 등록" })); await screen.findByText(/서버 승인 등록을 확인했습니다/);
  expect(posts()).toHaveLength(2); await inspectStatus(); expect(posts()).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "저장된 HTTP 입력·결과 열기" })).toBeTruthy(), { timeout: 1800 });
  fireEvent.click(screen.getByRole("button", { name: "저장된 HTTP 입력·결과 열기" })); expect(onOpenReceipt).toHaveBeenCalledWith("native-receipt");
  expect(posts()).toHaveLength(3);
  for (const [, options] of posts()) expect(options?.headers).toEqual({ "X-XRML-Paper-Anthropic-Action": "1" });
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: guid, expected_preparation_digest: hash, expected_binding_sha256: bindingHash, acknowledge_unknown: false });
});

it("shows default-disabled availability without implying empty success or exposing credential entry", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...preview, enabled: false, reason: "paper_anthropic_native_disabled", prepared: null, manifest: null, binding_sha256: null, input_origin: null });
  render(<ReviewAnthropicNative {...props}/>); expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" })); await screen.findByText(/Native 연결이 비활성화/);
  expect(screen.queryByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })).toBeNull(); expect(screen.queryByRole("textbox")).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each([403, 500])("reports failed Native read %s as an error", async code => {
  vi.mocked(api.fetchJson).mockRejectedValue(new api.ApiError("blocked", code, "")); render(<ReviewAnthropicNative {...props}/>);
  fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" })); const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(code === 403 ? /로컬 같은 출처의 \/app.*관리자/ : /조건을 확인하지 못했습니다/);
  expect(screen.queryByText(/Native 연결이 비활성화/)).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each([
  ["mock surface", { ...preview, manifest: { ...manifest, execution_surface: "MOCK_TRANSPORT" } }],
  ["synthetic source", { ...preview, input_origin: "FIXTURE_SYNTHETIC" }],
  ["wrong digest", { ...preview, preparation_digest: "d".repeat(64) }],
  ["zero revision", { ...preview, manifest: { ...manifest, credential_binding: { ...manifest.credential_binding, revision: 0 } } }],
  ["boolean revision", { ...preview, manifest: { ...manifest, credential_binding: { ...manifest.credential_binding, revision: true } } }],
  ["environment credential", { ...preview, manifest: { ...manifest, credential_binding: { ...manifest.credential_binding, storage: "ENVIRONMENT" } } }],
  ["unknown credential property", { ...preview, manifest: { ...manifest, credential_binding: { ...manifest.credential_binding, path: "not-a-public-field" } } }],
  ["model mismatch", { ...preview, manifest: { ...manifest, model: "other-model" } }],
  ["total timeout", { ...preview, manifest: { ...manifest, timeout_semantics: "TOTAL" } }],
  ["system hash", { ...preview, manifest: { ...manifest, effective_system_sha256: "invalid" } }],
])("fails closed on %s preview", async (_field, invalid) => {
  vi.mocked(api.fetchJson).mockResolvedValue(invalid); render(<ReviewAnthropicNative {...props}/>);
  fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" })); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each([
  ["mock issuer", { ...approval, offer: { ...offer, issuer_id: "xrml-anthropic-mock-issuer-v1" } }],
  ["mock policy", { ...approval, offer: { ...offer, policy_version: "PAPER_ANTHROPIC_MOCK_ONLY_V1" } }],
  ["synthetic source", { ...approval, offer: { ...offer, input_origin: "FIXTURE_SYNTHETIC" } }],
  ["different actor", { ...approval, actor_id: "someone-else" }],
  ["stale sequence", { ...approval, observed_sequence: 1 }],
])("never registers a mismatched or synthetic %s approval", async (_field, invalid) => {
  vi.mocked(api.fetchJson).mockResolvedValue(invalid); render(<ReviewAnthropicNative {...props} selected={ready}/>);
  fireEvent.click(screen.getByRole("button", { name: "선택한 Native 요청의 승인 등록 조건 확인" })); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "확인한 Anthropic Native 승인 등록" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it("never offers Native controls on an existing synthetic execution", () => {
  const mock: TransportDetail = { ...approved, contract: { ...approved.contract, input_origin: "FIXTURE_SYNTHETIC", transport: { ...manifest, execution_surface: "MOCK_TRANSPORT" } } };
  render(<ReviewAnthropicNative {...props} selected={mock}/>);
  expect(screen.queryByRole("button", { name: "선택한 Native 요청의 실행 조건 확인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Anthropic Native 승인 철회" })).toBeNull(); expect(api.fetchJson).not.toHaveBeenCalled();
});

it("blocks new dispatch when the stored key changes while retaining saved-history controls", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...status, enabled: false, reason: "paper_anthropic_native_credential_changed" });
  render(<ReviewAnthropicNative {...props} selected={approved}/>); await inspectStatus();
  expect(screen.getByText(/저장된 API 키가 바뀌었거나 삭제되어/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Anthropic Native 승인 철회" })).toHaveProperty("disabled", false); expect(posts()).toHaveLength(0);
});

it.each(["prepare", "approve", "dispatch"] as const)("preserves exact lost %s commands across refreshed state without automatic mutation", async kind => {
  const selected = kind === "prepare" ? null : kind === "approve" ? ready : approved;
  const response = kind === "prepare" ? ready : kind === "approve" ? approved : leased;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(kind === "prepare" ? preview : kind === "approve" ? approval : status)
    .mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce(response);
  const view = render(<ReviewAnthropicNative {...props} selected={selected}/>);
  if (kind === "prepare") await inspect(); else if (kind === "approve") await inspectApproval(); else await inspectStatus();
  const label = kind === "prepare" ? "이 구간으로 Anthropic Native 요청 준비" : kind === "approve" ? "확인한 Anthropic Native 승인 등록" : "서버가 허용한 Anthropic Native 검토 실행";
  fireEvent.click(screen.getByRole("button", { name: label })); await screen.findByRole("alert"); expect(posts()).toHaveLength(1);
  view.rerender(<ReviewAnthropicNative {...props} listing={{ ...listing, preparation_digest: "d".repeat(64) }} selected={response}/>);
  fireEvent.click(screen.getByRole("button", { name: `같은 Anthropic Native ${kind === "prepare" ? "준비" : kind === "approve" ? "승인" : "실행"} 요청 다시 확인` }));
  await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(response)); expect(posts()).toHaveLength(2); expect(posts()[1]).toEqual(posts()[0]);
});

it("keeps pending preparation from changing selected identity and blocks rapid duplicate preparation", async () => {
  let finish!: (value: unknown) => void; const waiting = new Promise(resolve => { finish = resolve; });
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(waiting);
  render(<ReviewAnthropicNative {...props}/>); await inspect(); const button = screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" });
  fireEvent.click(button); fireEvent.click(button); expect(posts()).toHaveLength(1); expect(onPending).toHaveBeenCalledWith(true, undefined);
  await act(async () => finish(ready)); expect(onUpdated).toHaveBeenCalledTimes(1);
});

it.each(["actor", "manifest", "command", "regression", "binding"])("retains pending when a Native approval ACK has wrong %s", async field => {
  const wrong = structuredClone(approved);
  if (field === "actor") wrong.contract.actor_id = "other";
  if (field === "manifest") wrong.contract.transport.credential_binding = { ...manifest.credential_binding, revision: 2 };
  if (field === "command") wrong.events[1].command = { kind: "control", body: { request_id: "other", expected_sequence: 0, action: "approve" } };
  if (field === "regression") { wrong.current.sequence = -1; }
  if (field === "binding") wrong.binding_sha256 = "d".repeat(64);
  vi.mocked(api.fetchJson).mockResolvedValueOnce(approval).mockResolvedValueOnce(wrong);
  render(<ReviewAnthropicNative {...props} selected={ready}/>); await inspectApproval();
  fireEvent.click(screen.getByRole("button", { name: "확인한 Anthropic Native 승인 등록" })); await screen.findByRole("alert");
  expect(onUpdated).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "같은 Anthropic Native 승인 요청 다시 확인" })).toBeTruthy();
});

it("discards a rejected changed-binding request and requires explicit inspection", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockRejectedValueOnce(new api.ApiError("binding_changed", 409, ""));
  render(<ReviewAnthropicNative {...props}/>); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })); await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "같은 Anthropic Native 준비 요청 다시 확인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })).toBeNull(); expect(posts()).toHaveLength(1);
});

it.each(["read", "save"] as const)("ignores a late Native %s after the topic/preparation scope changes", async kind => {
  let finish!: (value: unknown) => void; const waiting = new Promise(resolve => { finish = resolve; });
  if (kind === "read") vi.mocked(api.fetchJson).mockResolvedValueOnce(waiting); else vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(waiting);
  const view = render(<ReviewAnthropicNative {...props}/>);
  if (kind === "read") fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 전송 조건 확인" }));
  else { await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" })); }
  view.rerender(<ReviewAnthropicNative {...props} root="/other-topic" preparationId="other"/>);
  await act(async () => finish(kind === "read" ? preview : ready)); expect(onUpdated).not.toHaveBeenCalled();
  expect(screen.queryByText(/전송할 선택 범위:/)).toBeNull(); expect(screen.queryByRole("alert")).toBeNull();
});

it("revokes a terminal authority without a configured preview or key and preserves the saved receipt", async () => {
  const revoked = { ...saved, authority: { ...authority, validity: "REVOKED", revoked_at: at } };
  vi.mocked(api.fetchJson).mockResolvedValue(revoked); render(<ReviewAnthropicNative {...props} selected={saved}/>);
  fireEvent.click(screen.getByRole("button", { name: "Anthropic Native 승인 철회" })); await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(revoked));
  expect(api.fetchJson).toHaveBeenCalledTimes(1); expect(posts()[0][0]).toBe(`${base}/native-one/anthropic-native-revocation`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: guid, expected_sequence: 4, expected_contract_sha256: contractHash, expected_authority_sha256: hash });
});

it("continues Native received-result GET polling until local publication finishes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...listing, items: [received] }).mockResolvedValueOnce(saved);
  render(<ReviewTransport root={root} id={preparationId} onOpenReceipt={vi.fn()}/>); await open();
  await screen.findByRole("button", { name: "HTTP 수신 결과 저장 다시 시도" });
  await waitFor(() => expect(screen.getByRole("button", { name: "저장된 HTTP 입력·결과 열기" })).toBeTruthy(), { timeout: 1800 }); expect(posts()).toHaveLength(0);
});

it("allows same-execution read and cancel after a lost Native dispatch, then replays the original body", async () => {
  const runningEvent = { ...leasedEvent, sequence: 3, state: "RUNNING" as const, command: null, outcome_unknown: true };
  const running: NativeDetail = { ...leased, current: runningEvent, events: [...leased.events, runningEvent] };
  let current = approved, dispatches = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("anthropic-native-status")) return status as never;
    if (url.endsWith("anthropic-native-dispatch")) {
      if (++dispatches === 1) { current = running; throw new Error("response lost"); }
      return current as never;
    }
    if (url.endsWith("controls")) {
      const body = JSON.parse(options!.body as string), cancelledEvent = { ...runningEvent, sequence: 4, state: "CANCELLED" as const, deadline: null, command: { kind: "control", body } };
      current = { ...running, current: cancelledEvent, events: [...running.events, cancelledEvent] }; return current as never;
    }
    if (url.endsWith("native-one")) return current as never;
    return { ...listing, items: [current] } as never;
  });
  render(<ReviewTransport root={root} id={preparationId}/>); await open(); await inspectStatus();
  fireEvent.click(screen.getByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" })); await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "HTTP 실행 기록 열기 · native-one" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Anthropic 구조화 요청 확인" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })); await screen.findByText(/마지막 저장 상태: 시도 시작 기록됨/);
  fireEvent.click(screen.getByRole("button", { name: "이 HTTP 요청 취소" })); await screen.findByText(/마지막 저장 상태: 취소됨/);
  const cancellation = posts().find(([url]) => url.endsWith("controls")); expect(JSON.parse(cancellation![1]!.body as string).expected_sequence).toBe(3);
  fireEvent.click(screen.getByRole("button", { name: "같은 Anthropic Native 실행 요청 다시 확인" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "같은 Anthropic Native 실행 요청 다시 확인" })).toBeNull());
  const dispatch = posts().filter(([url]) => url.endsWith("anthropic-native-dispatch")); expect(dispatch).toHaveLength(2); expect(dispatch[1]).toEqual(dispatch[0]);
  expect(screen.getByText(/마지막 저장 상태: 취소됨/)).toBeTruthy();
});

it("requires unknown-outcome acknowledgement before a separate preparation and never auto-dispatches", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(preview); render(<ReviewAnthropicNative {...props} listing={{ ...listing, outcome_unknown_pending: true }}/>); await inspect();
  const button = screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" }); expect(button).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("checkbox")); expect(button).toHaveProperty("disabled", false); expect(posts()).toHaveLength(0);
});

it("checks approval expiry again at the actual dispatch click", async () => {
  const expires = "2999-01-01T00:00:00Z";
  vi.mocked(api.fetchJson).mockResolvedValue(status); render(<ReviewAnthropicNative {...props} selected={approved}/>); await inspectStatus();
  const button = screen.getByRole("button", { name: "서버가 허용한 Anthropic Native 검토 실행" }); expect(button).toHaveProperty("disabled", false);
  const now = vi.spyOn(Date, "now").mockReturnValue(Date.parse(expires) + 1);
  try { fireEvent.click(button); expect(posts()).toHaveLength(0); expect(screen.getByRole("alert").textContent).toMatch(/승인 유효 시간이 지났습니다/); }
  finally { now.mockRestore(); }
});

it("requires explicit keyboard preparation and keeps the success notice bound to the acknowledged history", async () => {
  const user = userEvent.setup(); vi.mocked(api.fetchJson).mockResolvedValueOnce(preview).mockResolvedValueOnce(ready);
  const view = render(<ReviewAnthropicNative {...props}/>); await user.tab(); await user.keyboard("{Enter}"); await screen.findByText(/전송할 선택 범위:/);
  const button = screen.getByRole("button", { name: "이 구간으로 Anthropic Native 요청 준비" }); button.focus(); await user.keyboard(" ");
  await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(ready)); view.rerender(<ReviewAnthropicNative {...props} selected={ready}/>);
  expect(screen.getByText(/Native 요청 준비를 저장했습니다/)).toBeTruthy();
  view.rerender(<ReviewAnthropicNative {...props} selected={{ ...saved, contract: { ...saved.contract, execution_id: "other" } }}/>);
  expect(screen.queryByText(/Native 요청 준비를 저장했습니다/)).toBeNull(); expect(posts()).toHaveLength(1);
});
