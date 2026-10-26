import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { formatTimestamp } from "../appearance";
import { ReviewTransport } from "../components/ReviewTransport";

const appearance = vi.hoisted(() => ({ timeFormat: "24h" as "12h" | "24h" }));
vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
vi.mock("../AppearanceProvider", () => ({ useOptionalAppearance: () => ({ preferences: { timeFormat: appearance.timeFormat } }) }));
const root = "/api/topics/topic/paper-bank/version/review-preparations", id = "prepared/one";
const base = `${root}/prepared%2Fone/transport-executions`;
const at = "2026-10-01T08:00:00Z", hash = "a".repeat(64);
const capabilities = { create: false, approve: false, dispatch: false, cancel: true, recover: true, save_result: true, reason: "paper_review_execution_not_configured", issuer_status: "NOT_CONFIGURED", provider_status: "UNREGISTERED", live_execution: "NOT_VERIFIED" };
function detail(state = "RESULT_RECEIVED", sequence = 4, unknown = false, reason: string | null = null) {
  const event = { sequence, state, at, deadline: ["APPROVED", "LEASED", "RUNNING"].includes(state) ? "2000-01-01T00:00:00Z" : null, outcome_unknown: unknown, reason, receipt_id: state === "RESULT_SAVED" ? "receipt-one" : null, authority: null };
  return {
    contract: { schema_version: "PAPER_REVIEW_TRANSPORT_EXECUTION_V1", execution_id: "transport-one", preparation_id: id, actor_id: "owner", created_at: at, mode: "TRUSTED_PAPER_CLI", input_origin: "FIXTURE_SYNTHETIC", specification: { request_id: "original", expected_preparation_digest: hash, acknowledge_unknown: false, provider_name: "codex_cli", model: "requested-model", max_output_tokens: 2200 },
      prepared: { provider_name: "codex_cli", model: "requested-model", request_sha256: hash, contract_sha256: hash, provider_request: { max_output_tokens: 2200, temperature: 0.2, prompt: "fixed packet", schema_name: "PREPARED_PAPER_REVIEW_DRAFT_V1" }, packet: { source_preparation: { preparation_id: id, selection: { start: 12, end: 16, chars: 4, sha256: hash }, source: { run_id: "run-one", paper_id: "paper-one", artifact_ref: "run:raw/paper.txt", source_chars: 1000 }, normalization: "XRML_WHITESPACE_V1" }, evidence_spans: [{ span_id: "prepared-range", text: "e\u0301한🙂" }] } },
      transport: { schema_version: "CODEX_PREPARED_REVIEW_CLI_V1", model: "requested-model", reasoning_effort: "high", timeout_seconds: 90, automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false, tool_isolation: "REQUESTED_NOT_LIVE_VERIFIED", model_identity: "REQUESTED_NOT_INDEPENDENTLY_VERIFIED", runtime_identity: { launcher_sha256: hash, environment_sha256: hash, workspace_sha256: hash } },
    },
    events: [{ ...event, sequence: 1, state: "APPROVED", authority: { authorization_id: "approval-one", record_sha256: hash } }, event], current: event,
    authority: { authorization_id: "approval-one", record_sha256: hash, issuer_id: "fixture-issuer", policy_version: "PAPER_REVIEW_POLICY_V1", granted_at: at, expires_at: "2026-10-01T08:02:00Z", revoked_at: null as string | null, validity: "EXPIRED" },
  };
}
const listing = (items = [detail()]) => ({ items, preparation_digest: hash, page: 1, pageSize: 10, hasMore: false, outcome_unknown_pending: items.some(item => item.current.outcome_unknown), capabilities });
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");
beforeEach(() => { vi.resetAllMocks(); appearance.timeFormat = "24h"; vi.mocked(api.fetchJson).mockResolvedValue(listing() as never); });
async function open(onOpenReceipt?: (id: string) => void) {
  const view = render(<ReviewTransport root={root} id={id} onOpenReceipt={onOpenReceipt}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("검토 실행 조건·이력"));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledWith(`${base}?page=1&pageSize=10`));
  return view;
}

it("loads lazily and shows fixed input, requested settings, authority reference, and honest fixture status", async () => {
  await open();
  const selected = await screen.findByRole("region", { name: "선택한 CLI 실행 기록" });
  expect(within(selected).getByText(/고정 입력: 12–16 · 4자/)).toBeTruthy();
  expect(within(selected).getByText("요청 모델: requested-model · Provider: codex_cli")).toBeTruthy();
  expect(within(selected).getByText(/요청 reasoning: high · 제한 시간: 90초/)).toBeTruthy();
  expect(within(selected).getByText(/출력 토큰 2200 · temperature 0.2/)).toBeTruthy();
  expect(within(selected).getByText(/실제 강제 여부가 보장되지 않습니다/)).toBeTruthy();
  expect(within(selected).getByText(/합성 시험 자료.*FIXTURE_SYNTHETIC/)).toBeTruthy();
  expect(within(selected).getByText(/승인 참조: approval-one/)).toBeTruthy();
  expect(screen.getByText(/이력 조회는 새 실행이나 승인을 만들지 않습니다.*이 화면에는 실제 실행 승인을 발급하는 기능이 없습니다/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /승인|실행 시작|새 요청|철회/ })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("identifies a Codex V2 paper profile while preserving unknown outcomes and read-only controls", async () => {
  const value = detail("INTERRUPTED", 4, true, "adapter_failed");
  const versioned = { ...value, contract: { ...value.contract, transport: { ...value.contract.transport,
    schema_version: "CODEX_PREPARED_REVIEW_CLI_V2", adapter_id: "xrml-codex-prepared-review", adapter_version: "2",
    runtime_profile: "CODEX_JOURNAL_CLI_V2", model_provider: "xrml_openai", requires_openai_auth: true, wire_api: "responses",
    forced_login_method: "chatgpt", request_max_retries: 0, stream_max_retries: 0, supports_websockets: false,
    authentication: "OFFICIAL_CLI_CHATGPT_REQUIRED_NOT_VERIFIED", endpoint_resolution: "OFFICIAL_CLI_OWNED_NOT_LIVE_VERIFIED",
    retry_policy: "REQUESTED_ZERO_NOT_LIVE_VERIFIED", xrml_process_relaunch: false, remote_call_count_enforced: false,
    cost_limit_enforced: false, live_execution: "NOT_VERIFIED", billing_route: "CHATGPT_ACCOUNT_NOT_VERIFIED",
    raw_output_encoding: "LAST_MESSAGE_JSON_UTF8_V1", raw_envelope_retained: false,
  } } };
  vi.mocked(api.fetchJson).mockResolvedValue(listing([versioned]) as never);
  await open();
  await screen.findByText("Codex CLI V2 프로필");
  expect(screen.getByText(/공식 CLI가 관리하는 ChatGPT 로그인/)).toBeTruthy();
  expect(screen.getByText(/요청 재시도 0회 · 스트림 재시도 0회/)).toBeTruthy();
  expect(screen.getByText(/이 시도의 결과가 불명확합니다/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /승인|실행 시작|새 요청|철회/ })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it.each([
  ["provider_failed", "Provider가 정상 완료하지 못했다고 보고했습니다."],
  ["provider_incomplete", "Provider가 응답을 끝까지 완료하지 못했다고 보고했습니다. 검토 결과는 저장되지 않았습니다."],
  ["provider_cancelled", "Provider가 실행 취소를 보고했습니다. 사용자가 앱에서 취소한 기록과 구분됩니다."],
  ["provider_refused", "Provider가 이 검토 요청을 거절했습니다."],
  ["output_invalid", "수신한 응답을 유효한 검토 결과로 저장하지 못했습니다."],
])("explains known %s without claiming it happened before execution", async (reason, message) => {
  vi.mocked(api.fetchJson).mockResolvedValue(listing([detail("FAILED", 4, false, reason)]) as never);
  await open();
  await screen.findByText(/마지막 저장 상태: 실패/);
  expect(screen.getByText(`기록 사유: ${message} (${reason})`)).toBeTruthy();
  expect(screen.queryByText(/실행 전 실패/)).toBeNull();
  expect(screen.queryByText(/이 시도의 결과가 불명확합니다/)).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("separates unknown outcomes from a reported failure and never retries by reading", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(listing([detail("INTERRUPTED", 4, true, "adapter_failed")]) as never);
  await open();
  await screen.findByText(/이 시도의 결과가 불명확합니다/);
  expect(screen.getByText(/자동으로 다시 실행하지 않습니다/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(2));
  expect(posts()).toHaveLength(0);
});

it("shows a cross-history unknown notice even when the CLI page is empty", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing([]), outcome_unknown_pending: true } as never);
  await open();
  await screen.findByText("저장된 검토 실행 기록이 없습니다.");
  expect(screen.getByText(/합성·CLI·HTTP 이력에 결과가 불명확한 기록이 있습니다/)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(posts()).toHaveLength(0);
});

it.each([
  ["NOT_YET_VALID", "승인 유효 기간 전"], ["WITHIN_VALIDITY_WINDOW", "승인 유효 기간 내"],
  ["EXPIRED", "승인 유효 기간 만료"], ["REVOKED", "승인 철회 기록 있음"],
])("shows projected authority status %s without enabling execution", async (validity, label) => {
  const saved = detail("RESULT_SAVED", 5); saved.authority.validity = validity;
  if (validity === "REVOKED") saved.authority.revoked_at = "2026-10-01T08:01:00Z";
  vi.mocked(api.fetchJson).mockResolvedValue(listing([saved]) as never);
  await open();
  await screen.findByText(label);
  expect(screen.getByText(/발급 주체: fixture-issuer · 정책: PAPER_REVIEW_POLICY_V1/)).toBeTruthy();
  expect(screen.getByText(/시간상 유효 여부와 저장된 승인 참조이며 현재 실행 가능 여부나 실제 인증을 보증하지 않습니다/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /승인|실행 시작|새 요청|철회/ })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("does not elevate a provider-response origin into live or whole-document evidence", async () => {
  const saved = detail("RESULT_SAVED", 5); saved.contract.input_origin = "PROVIDER_RESPONSE";
  vi.mocked(api.fetchJson).mockResolvedValue(listing([saved]) as never);
  await open();
  await screen.findByText(/Provider 결과를 위한 요청.*PROVIDER_RESPONSE/);
  expect(screen.getByText(/실제 계정·모델 실행이나 과학적 타당성·논문 전체 검토가 검증됐다는 뜻은 아닙니다/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it("does not claim a provider response or completed execution for a provider-origin READY request", async () => {
  const prepared = detail("READY", 0);
  const ready = { ...prepared, contract: { ...prepared.contract, input_origin: "PROVIDER_RESPONSE" }, events: [prepared.current], authority: null };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing([]), items: [ready] } as never);
  await open();
  await screen.findByText(/마지막 저장 상태: 요청 준비됨/);
  expect(screen.getByText(/Provider 결과를 위한 요청.*PROVIDER_RESPONSE.*응답 수신 여부는 마지막 저장 상태에서 확인하세요/)).toBeTruthy();
  expect(screen.getByText("연결된 승인 기록이 없습니다.")).toBeTruthy();
  expect(screen.queryByText(/Provider 응답 기록|마지막 저장 상태: 결과 수신됨|마지막 저장 상태: 결과 저장됨/)).toBeNull();
  expect(screen.queryByRole("button", { name: "저장된 CLI 입력·결과 열기" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("saves a received result by keyboard without approval or dispatch", async () => {
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? detail("RESULT_SAVED", 5) : listing()) as never);
  await open(onOpenReceipt);
  const save = await screen.findByRole("button", { name: "CLI 수신 결과 저장 다시 시도" });
  save.focus(); await userEvent.setup().keyboard("{Enter}");
  fireEvent.click(await screen.findByRole("button", { name: "저장된 CLI 입력·결과 열기" }));
  expect(onOpenReceipt).toHaveBeenCalledWith("receipt-one");
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${base}/transport-one/controls`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 4, action: "save_result" });
});

it.each([undefined, 408, 503])("retains the exact pending local command on ambiguous status %s", async status => {
  let attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method !== "POST") return listing() as never;
    if (++attempts === 1) throw status === undefined ? new Error("response lost") : new api.ApiError("response lost", status, "{}");
    return detail("RESULT_SAVED", 5) as never;
  });
  await open(); fireEvent.click(await screen.findByRole("button", { name: "CLI 수신 결과 저장 다시 시도" }));
  await screen.findByText(/response lost/);
  expect(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "이 CLI 요청 취소" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 CLI 관리 요청 다시 보내기" }));
  await screen.findByText(/마지막 저장 상태: 결과 저장됨/);
  expect(posts()).toHaveLength(2); expect(posts()[1]).toEqual(posts()[0]);
});

it("reloads after a definitive sequence conflict and stops offering the rejected command", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "POST") throw new api.ApiError("sequence_conflict", 409, "{}");
    return listing([detail(++reads === 1 ? "RESULT_RECEIVED" : "CANCELLED", reads === 1 ? 4 : 5)]) as never;
  });
  await open(); fireEvent.click(await screen.findByRole("button", { name: "CLI 수신 결과 저장 다시 시도" }));
  await screen.findByText(/마지막 저장 상태: 취소됨/);
  expect(screen.queryByRole("button", { name: "같은 CLI 관리 요청 다시 보내기" })).toBeNull();
  expect(posts()).toHaveLength(1);
});

it("recovers an expired recorded attempt only on explicit command", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? detail("INTERRUPTED", 4, true, "deadline_expired") : listing([detail("RUNNING", 3)])) as never);
  await open();
  const recover = await screen.findByRole("button", { name: "CLI 만료 상태 확인·복구" });
  expect(posts()).toHaveLength(0);
  fireEvent.click(recover); await screen.findByText(/마지막 저장 상태: 중단됨/);
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ expected_sequence: 3, action: "recover" });
  expect(posts()).toHaveLength(1);
});

it("disables recovery before the recorded deadline and cancellation explains its storage boundary", async () => {
  const running = detail("RUNNING", 3); running.current.deadline = "2999-01-01T00:00:00Z";
  vi.mocked(api.fetchJson).mockResolvedValue(listing([running]) as never);
  await open();
  expect(await screen.findByRole("button", { name: "CLI 만료 상태 확인·복구" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/물리적 중단이나 비용 환불을 뜻하지 않습니다/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it("obeys local recovery capabilities instead of enabling controls just because a record exists", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing(), capabilities: { ...capabilities, cancel: false, recover: false, save_result: false } } as never);
  await open();
  expect(await screen.findByRole("button", { name: "CLI 수신 결과 저장 다시 시도" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "이 CLI 요청 취소" })).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(0);
});

it("distinguishes read errors from empty history and reads the next page without executing", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (++reads === 1) throw new Error("history unavailable");
    return { ...listing(path.includes("page=2") ? [] : [detail()]), hasMore: !path.includes("page=2"), page: path.includes("page=2") ? 2 : 1 } as never;
  });
  await open(); await screen.findByRole("alert");
  expect(screen.queryByText("저장된 검토 실행 기록이 없습니다.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  fireEvent.click(await screen.findByRole("button", { name: "다음 실행 기록 페이지" }));
  await screen.findByText("저장된 검토 실행 기록이 없습니다.");
  expect(api.fetchJson).toHaveBeenCalledWith(`${base}?page=2&pageSize=10`);
  expect(posts()).toHaveLength(0);
});

it("honors the selected timestamp format and preserves long contract text in a folded view", async () => {
  appearance.timeFormat = "12h";
  const view = await open();
  await screen.findByText(`마지막 저장 상태: 결과 수신됨 · 입력 기록 저장 대기 · ${formatTimestamp(at, "12h", "ko-KR")}`);
  appearance.timeFormat = "24h"; view.rerender(<ReviewTransport root={root} id={id}/>);
  await screen.findByText(`마지막 저장 상태: 결과 수신됨 · 입력 기록 저장 대기 · ${formatTimestamp(at, "24h", "ko-KR")}`);
  const raw = screen.getByText("CLI 고정 계약·상태 원문").closest("details")!;
  expect(raw.open).toBe(false);
  fireEvent.click(within(raw).getByText("CLI 고정 계약·상태 원문"));
  expect(within(raw).getByLabelText("보존된 CLI 실행 원문").textContent).toContain(hash);
  expect(screen.getByRole("region", { name: "선택한 CLI 실행 기록" }).style.overflowWrap).toBe("anywhere");
});

it("discards late local-control responses and collapses the panel on topic change", async () => {
  let resolve!: (value: never) => void;
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => init?.method === "POST" ? new Promise<never>(value => { resolve = value; }) : listing() as never);
  const view = await open(onOpenReceipt);
  fireEvent.click(await screen.findByRole("button", { name: "CLI 수신 결과 저장 다시 시도" }));
  await waitFor(() => expect(posts()).toHaveLength(1));
  view.rerender(<ReviewTransport root="/other-topic/preparations" id={id} onOpenReceipt={onOpenReceipt}/>);
  await act(async () => resolve(detail("RESULT_SAVED", 5) as never));
  expect(screen.queryByText(/receipt-one/)).toBeNull();
  expect(screen.queryByRole("button", { name: "같은 CLI 관리 요청 다시 보내기" })).toBeNull();
  expect(onOpenReceipt).not.toHaveBeenCalled();
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByText("검토 실행 조건·이력"));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledWith("/other-topic/preparations/prepared%2Fone/transport-executions?page=1&pageSize=10"));
});

const serverContractHash = "b".repeat(64);
const offlineCapability = { enabled: true, mode: "OFFLINE_SYNTHETIC_TRANSPORT", model: "synthetic-prepared-review-v2", issuer_id: "xrml-paper-offline-issuer-v1", policy_version: "PAPER_OFFLINE_SYNTHETIC_ONLY_V1", reason: null };
function offlineDetail(state = "READY", sequence = 0) {
  const value = detail(state, sequence);
  value.contract.prepared.model = offlineCapability.model;
  value.contract.prepared.provider_name = "xrml_offline_review";
  value.contract.specification.model = offlineCapability.model;
  value.contract.specification.provider_name = "xrml_offline_review";
  if (["APPROVED", "LEASED", "RUNNING"].includes(state)) value.current.deadline = "2999-01-01T00:00:00Z";
  const authority = state === "READY" ? null : { ...value.authority, issuer_id: offlineCapability.issuer_id, policy_version: offlineCapability.policy_version, expires_at: "2999-01-01T00:00:00Z", validity: state === "REVOKED" ? "REVOKED" : "WITHIN_VALIDITY_WINDOW", revoked_at: state === "REVOKED" ? at : null };
  return { ...value, contract: { ...value.contract, mode: "OFFLINE_SYNTHETIC_TRANSPORT", transport: { schema_version: "OFFLINE_PREPARED_REVIEW_TRANSPORT_V1" } }, authority, offline_eligible: true, contract_sha256: serverContractHash, events: sequence <= 1 ? [value.current] : value.events };
}
const offlineListing = (items: ReturnType<typeof offlineDetail>[] = []) => ({ ...listing([]), items, offline: offlineCapability });

it("separates fixed offline preparation, server-bound approval, start and completed-result revocation", async () => {
  let phase = "empty";
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method !== "POST") return (phase === "running" ? offlineListing([offlineDetail("RESULT_SAVED", 5)]) : offlineListing()) as never;
    if (path.endsWith("/offline-requests")) { phase = "ready"; return offlineDetail() as never; }
    if (path.endsWith("/offline-approval")) { phase = "approved"; return offlineDetail("APPROVED", 1) as never; }
    if (path.endsWith("/offline-dispatch")) { phase = "running"; return offlineDetail("RUNNING", 3) as never; }
    const saved = offlineDetail("RESULT_SAVED", 5);
    return { ...saved, authority: { ...saved.authority!, validity: "REVOKED", revoked_at: at } } as never;
  });
  await open();
  expect(await screen.findByText(/CLI·별도 프로세스·외부 전송·실제 AI 모델을 실행하지 않습니다/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "새 오프라인 연결 시험 요청 준비" }));
  const approve = await screen.findByRole("button", { name: "이 고정 요청의 오프라인 시험 승인" });
  const selected = screen.getByRole("region", { name: "선택한 오프라인 시험 기록" });
  expect(within(selected).getByText(/실행 방식: 앱 안에서 고정 합성 응답을 생성합니다/)).toBeTruthy();
  expect(within(selected).queryByText(/요청 reasoning:/)).toBeNull();
  expect(within(selected).queryByText(/undefined/)).toBeNull();
  expect(within(selected).getByRole("button", { name: "이 오프라인 요청 취소" })).toBeTruthy();
  expect(within(selected).queryByRole("button", { name: "이 CLI 요청 취소" })).toBeNull();
  expect(posts()).toHaveLength(1);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_preparation_digest: hash, acknowledge_unknown: false });
  expect(screen.queryByRole("button", { name: "승인된 오프라인 연결 시험 시작" })).toBeNull();
  approve.focus(); await userEvent.setup().keyboard("{Enter}");
  await screen.findByRole("button", { name: "승인된 오프라인 연결 시험 시작" });
  expect(posts()[1][0]).toBe(`${base}/transport-one/offline-approval`);
  expect(JSON.parse(posts()[1][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 0, expected_contract_sha256: serverContractHash });
  expect(serverContractHash).not.toBe(hash);
  expect(posts()).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "승인된 오프라인 연결 시험 시작" }));
  await screen.findByText(/마지막 저장 상태: 시도 시작 기록됨/);
  expect(posts()[2][0]).toBe(`${base}/transport-one/offline-dispatch`);
  expect(JSON.parse(posts()[2][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 1 });
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  await screen.findByText(/마지막 저장 상태: 결과 저장됨/);
  expect(screen.getByText(/철회해도 이미 저장된 결과는 유지됩니다/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "이 오프라인 시험 승인 철회" }));
  await screen.findByText("승인 철회 기록 있음");
  expect(screen.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeTruthy();
  expect(posts()[3][0]).toBe(`${base}/transport-one/offline-revocation`);
  expect(JSON.parse(posts()[3][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 5 });
  expect(posts()).toHaveLength(4);
});

it.each([
  ["prepare", "새 오프라인 연결 시험 요청 준비", "/offline-requests"],
  ["approve", "이 고정 요청의 오프라인 시험 승인", "/transport-one/offline-approval"],
  ["dispatch", "승인된 오프라인 연결 시험 시작", "/transport-one/offline-dispatch"],
  ["revoke", "이 오프라인 시험 승인 철회", "/transport-one/offline-revocation"],
])("retries the exact offline %s command after a lost response without a competing action", async (action, label, suffix) => {
  let attempts = 0;
  const initial = action === "approve" ? offlineDetail() : offlineDetail("APPROVED", 1);
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method !== "POST") return offlineListing(action === "prepare" ? [] : [initial]) as never;
    if (++attempts === 1) throw new Error("offline response lost");
    return offlineDetail(action === "prepare" ? "READY" : action === "approve" ? "APPROVED" : action === "dispatch" ? "RUNNING" : "REVOKED", action === "prepare" ? 0 : action === "approve" ? 1 : action === "dispatch" ? 3 : 2) as never;
  });
  await open(); fireEvent.click(await screen.findByRole("button", { name: label }));
  await screen.findByText(/offline response lost/);
  expect(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "새 오프라인 연결 시험 요청 준비" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 오프라인 관리 요청 다시 보내기" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "같은 오프라인 관리 요청 다시 보내기" })).toBeNull());
  expect(posts()).toHaveLength(2); expect(posts()[0][0]).toBe(base + suffix); expect(posts()[1]).toEqual(posts()[0]);
});

it.each(["missing_hash", "ineligible", "disabled"])("does not offer offline approval when %s", async condition => {
  const value = { ...offlineDetail(), offline_eligible: condition !== "ineligible", contract_sha256: condition === "missing_hash" ? undefined : serverContractHash };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...offlineListing(), items: [value], offline: { ...offlineCapability, enabled: condition !== "disabled", reason: condition === "disabled" ? "offline_disabled" : null } } as never);
  await open(); await screen.findByText(/마지막 저장 상태: 요청 준비됨/);
  expect(screen.queryByRole("button", { name: "이 고정 요청의 오프라인 시험 승인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "승인된 오프라인 연결 시험 시작" })).toBeNull();
  if (condition === "disabled") expect(screen.queryByRole("button", { name: "새 오프라인 연결 시험 요청 준비" })).toBeNull();
  else expect(screen.getByText(/현재 오프라인 시험 대상으로 확인되지 않아 새 승인·시작을 제공하지 않습니다/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it("requires cross-history unknown acknowledgment and refuses a second active offline preparation", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? offlineDetail() : { ...offlineListing(), outcome_unknown_pending: true }) as never);
  await open();
  const prepare = await screen.findByRole("button", { name: "새 오프라인 연결 시험 요청 준비" });
  expect(prepare).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("checkbox", { name: /이전 결과가 불명확함을 확인/ }));
  fireEvent.click(prepare);
  await screen.findByRole("button", { name: "이 고정 요청의 오프라인 시험 승인" });
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ acknowledge_unknown: true });
  expect(screen.getByRole("button", { name: "새 오프라인 연결 시험 요청 준비" })).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(1);
});

it("reloads an offline contract conflict without retaining an approval command", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "POST") throw new api.ApiError("contract_changed", 409, "{}");
    return offlineListing([offlineDetail(++reads === 1 ? "READY" : "CANCELLED", reads - 1)]) as never;
  });
  await open(); fireEvent.click(await screen.findByRole("button", { name: "이 고정 요청의 오프라인 시험 승인" }));
  await screen.findByText(/마지막 저장 상태: 취소됨/);
  expect(screen.getByText("검토 관리 요청 오류: contract_changed")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "같은 오프라인 관리 요청 다시 보내기" })).toBeNull();
  expect(posts()).toHaveLength(1);
});

it("explains a changed execution and requires an explicit fresh revocation against the reloaded sequence", async () => {
  let reads = 0, writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method !== "POST") return offlineListing([offlineDetail(++reads === 1 ? "RESULT_RECEIVED" : "RESULT_SAVED", reads === 1 ? 4 : 5)]) as never;
    if (++writes === 1) throw new api.ApiError("review_execution_changed", 409, '{"detail":"review_execution_changed"}');
    const saved = offlineDetail("RESULT_SAVED", 5);
    return { ...saved, authority: { ...saved.authority!, validity: "REVOKED", revoked_at: at } } as never;
  });
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "이 오프라인 시험 승인 철회" }));
  await screen.findByText(/저장 상태가 바뀌어 요청을 적용하지 않았습니다\. 최신 이력을 확인한 뒤 작업을 다시 선택하세요\./);
  await screen.findByText(/마지막 저장 상태: 결과 저장됨/);
  expect(screen.queryByRole("button", { name: "같은 오프라인 관리 요청 다시 보내기" })).toBeNull();
  expect(posts()).toHaveLength(1);
  const rejected = JSON.parse(posts()[0][1]!.body as string);
  expect(rejected).toMatchObject({ expected_sequence: 4 });
  fireEvent.click(screen.getByRole("button", { name: "이 오프라인 시험 승인 철회" }));
  await screen.findByText("승인 철회 기록 있음");
  expect(posts()).toHaveLength(2);
  const resubmitted = JSON.parse(posts()[1][1]!.body as string);
  expect(posts()[1][0]).toBe(`${base}/transport-one/offline-revocation`);
  expect(resubmitted).toEqual({ request_id: expect.any(String), expected_sequence: 5 });
  expect(resubmitted.request_id).not.toBe(rejected.request_id);
});

it("keeps controls blocked when the execution-conflict reload fails without claiming it succeeded", async () => {
  let reads = 0;
  let rejectReload!: (reason: Error) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "POST") throw new api.ApiError("review_execution_changed", 409, '{}');
    if (++reads === 1) return offlineListing([offlineDetail("RESULT_RECEIVED", 4)]) as never;
    return new Promise<never>((_resolve, reject) => { rejectReload = reject; });
  });
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "이 오프라인 시험 승인 철회" }));
  await screen.findByText(/저장 상태가 바뀌어 요청을 적용하지 않았습니다\. 최신 이력을 확인한 뒤 작업을 다시 선택하세요\./);
  expect(screen.getByRole("button", { name: "오프라인 수신 결과 저장 다시 시도" })).toHaveProperty("disabled", true);
  await act(async () => rejectReload(new Error("history unavailable")));
  await screen.findByText("검토 실행 이력 조회 오류: history unavailable");
  expect(screen.getByRole("button", { name: "오프라인 수신 결과 저장 다시 시도" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "이 오프라인 요청 취소" })).toHaveProperty("disabled", true);
  expect(screen.queryByRole("button", { name: "이 오프라인 시험 승인 철회" })).toBeNull();
  expect(screen.queryByRole("button", { name: "같은 오프라인 관리 요청 다시 보내기" })).toBeNull();
  expect(screen.queryByText(/최신 이력.*불러왔|최신 이력.*조회했/)).toBeNull();
  expect(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })).toHaveProperty("disabled", false);
  expect(posts()).toHaveLength(1);
});

it("prevents duplicate offline submission and ignores its response after a topic change", async () => {
  let resolve!: (value: never) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => init?.method === "POST" ? new Promise<never>(value => { resolve = value; }) : offlineListing() as never);
  const view = await open();
  const prepare = await screen.findByRole("button", { name: "새 오프라인 연결 시험 요청 준비" });
  fireEvent.click(prepare); fireEvent.click(prepare);
  expect(posts()).toHaveLength(1);
  view.rerender(<ReviewTransport root="/other-topic/preparations" id={id}/>);
  await act(async () => resolve(offlineDetail() as never));
  expect(screen.queryByRole("button", { name: "이 고정 요청의 오프라인 시험 승인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "같은 오프라인 관리 요청 다시 보내기" })).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
});

function httpDetail(state = "RESULT_RECEIVED", sequence = 4, unknown = false) {
  const value = detail(state, sequence, unknown, unknown ? "adapter_failed" : null);
  return { ...value, authority: state === "READY" ? null : value.authority, contract: { ...value.contract, mode: "TRUSTED_PAPER_HTTP",
    specification: { ...value.contract.specification, provider_name: "openai" },
    prepared: { ...value.contract.prepared, provider_name: "openai" },
    transport: { schema_version: "OPENAI_PREPARED_REVIEW_WIRE_V1", adapter_id: "xrml-openai-prepared-review", adapter_version: "1",
      endpoint: "https://offline.invalid/v1/responses", model: "requested-model", timeout_seconds: 45,
      request_sha256: hash, body_sha256: hash, body_bytes: 6400, max_response_bytes: 4_000_000, max_review_output_bytes: 262144,
      automatic_retry: false, output_token_limit_enforced: false, temperature_enforced: false,
      token_limit_policy: "REQUESTED_NOT_LIVE_VERIFIED", store: false, background: false, stream: false,
      truncation: "disabled", redirects: false, environment_proxy: false },
  } };
}

it("shows HTTP history and requested wire settings without CLI or live-execution claims", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing([]), items: [httpDetail("READY", 0)] } as never);
  await open();
  const selected = await screen.findByRole("region", { name: "선택한 HTTP 실행 기록" });
  expect(within(selected).getByText("요청 모델: requested-model · Provider: openai")).toBeTruthy();
  expect(within(selected).getByText("요청 대상: https://offline.invalid/v1/responses")).toBeTruthy();
  expect(within(selected).getByText(/HTTP 제한 시간: 45초/)).toBeTruthy();
  expect(within(selected).getByText(/요청 본문: 6400바이트 · HTTP 응답 읽기 한도: 4000000바이트 · 검토 결과 한도: 262144바이트/)).toBeTruthy();
  expect(within(selected).getByText(/요청 설정: 응답 저장 사용 안 함 · 백그라운드 사용 안 함 · 스트리밍 사용 안 함 · 입력 자동 잘라내기 사용 안 함/)).toBeTruthy();
  expect(within(selected).getByText(/로컬 전송 설정: 리다이렉트 사용 안 함 · 환경 프록시 사용 안 함 · 자동 재시도 사용 안 함/)).toBeTruthy();
  expect(within(selected).getByText(/출력 토큰 2200 · temperature 0.2/)).toBeTruthy();
  expect(within(selected).getByText(/요청값의 실제 강제 여부가 보장되지 않습니다/)).toBeTruthy();
  expect(within(selected).getByText(/서버의 실제 적용·실제 모델·계정·비용은 독립적으로 확인되지 않았습니다/)).toBeTruthy();
  expect(within(selected).queryByText(/요청 reasoning|도구 제한|undefined/)).toBeNull();
  expect(within(selected).queryByText(/CLI/)).toBeNull();
  expect(within(selected).getByText("HTTP 고정 계약·상태 원문").closest("details")).toHaveProperty("open", false);
  expect(screen.queryByRole("button", { name: /승인|실행 시작|새 요청|철회/ })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("shows Anthropic Messages history without inventing Responses settings or subscription authentication", async () => {
  const value = httpDetail("READY", 0);
  const { store: _store, background: _background, truncation: _truncation, ...transport } = value.contract.transport;
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing([]), items: [{ ...value, contract: { ...value.contract,
    prepared: { ...value.contract.prepared, provider_name: "anthropic" },
    transport: { ...transport, schema_version: "ANTHROPIC_PREPARED_REVIEW_WIRE_V1", endpoint: "https://api.anthropic.com/v1/messages", api_version: "2023-06-01", temperature_policy: "OMITTED_PROVIDER_DEFAULT" },
  } }] } as never);
  await open();
  const selected = await screen.findByRole("region", { name: "선택한 HTTP 실행 기록" });
  expect(within(selected).getByText(/Anthropic Messages API · API 버전: 2023-06-01 · 스트리밍 사용 안 함/)).toBeTruthy();
  expect(within(selected).getByText(/Claude 구독 로그인과 별도인 API 키 경로/)).toBeTruthy();
  expect(within(selected).getByText(/고정 요청 온도: 0.2 · 전송하지 않으며 Provider 기본값을 사용합니다/)).toBeTruthy();
  expect(within(selected).queryByText(/요청 설정: 응답 저장|백그라운드|입력 자동 잘라내기/)).toBeNull();
  expect(screen.queryByRole("button", { name: /승인|실행 시작|새 요청|철회/ })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("saves an HTTP received result by keyboard and opens its receipt without execution", async () => {
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) =>
    (init?.method === "POST" ? httpDetail("RESULT_SAVED", 5) : { ...listing([]), items: [httpDetail()] }) as never);
  await open(onOpenReceipt);
  const save = await screen.findByRole("button", { name: "HTTP 수신 결과 저장 다시 시도" });
  save.focus(); await userEvent.setup().keyboard("{Enter}");
  fireEvent.click(await screen.findByRole("button", { name: "저장된 HTTP 입력·결과 열기" }));
  expect(onOpenReceipt).toHaveBeenCalledWith("receipt-one");
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${base}/transport-one/controls`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 4, action: "save_result" });
});

it.each([
  ["READY", "이 HTTP 요청 취소", "cancel"],
  ["RUNNING", "HTTP 만료 상태 확인·복구", "recover"],
])("keeps HTTP %s controls on the existing local endpoint", async (state, label, action) => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) =>
    (init?.method === "POST" ? httpDetail("CANCELLED", 5) : { ...listing([]), items: [httpDetail(state)] }) as never);
  await open();
  fireEvent.click(await screen.findByRole("button", { name: label }));
  await screen.findByText(/마지막 저장 상태: 취소됨/);
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${base}/transport-one/controls`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 4, action });
});

it("replays the same HTTP save command after a lost response without invoking a provider", async () => {
  let attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method !== "POST") return { ...listing([]), items: [httpDetail()] } as never;
    if (++attempts === 1) throw new Error("HTTP control response lost");
    return httpDetail("RESULT_SAVED", 5) as never;
  });
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "HTTP 수신 결과 저장 다시 시도" }));
  await screen.findByText(/HTTP control response lost/);
  expect(screen.getByRole("button", { name: "이 HTTP 요청 취소" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 HTTP 관리 요청 다시 보내기" }));
  await screen.findByText(/마지막 저장 상태: 결과 저장됨/);
  expect(posts()).toHaveLength(2);
  expect(posts()[1]).toEqual(posts()[0]);
  expect(posts()[0][0]).toBe(`${base}/transport-one/controls`);
});

it("does not turn an HTTP record into an offline approval target", async () => {
  const value = { ...httpDetail("READY", 0), offline_eligible: true, contract_sha256: serverContractHash };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...offlineListing(), items: [value] } as never);
  await open();
  await screen.findByRole("region", { name: "선택한 HTTP 실행 기록" });
  expect(screen.queryByRole("button", { name: "이 고정 요청의 오프라인 시험 승인" })).toBeNull();
  expect(screen.queryByRole("button", { name: "승인된 오프라인 연결 시험 시작" })).toBeNull();
  expect(screen.queryByRole("button", { name: "이 오프라인 시험 승인 철회" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("keeps mixed HTTP, CLI and offline history labels tied to each selected record", async () => {
  const values = [httpDetail(), detail(), offlineDetail()].map((value, index) => ({
    ...value, contract: { ...value.contract, execution_id: `mixed-${index}` },
  }));
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.includes("?")
    ? { ...listing([]), items: values }
    : values.find(value => path.endsWith(`/${value.contract.execution_id}`))) as never);
  await open();
  await screen.findByRole("region", { name: "선택한 HTTP 실행 기록" });
  fireEvent.click(screen.getByRole("button", { name: "CLI 실행 기록 열기 · mixed-1" }));
  const cli = await screen.findByRole("region", { name: "선택한 CLI 실행 기록" });
  expect(within(cli).getByText(/요청 reasoning: high/)).toBeTruthy();
  expect(within(cli).queryByText(/요청 대상:/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "오프라인 시험 기록 열기 · mixed-2" }));
  const offline = await screen.findByRole("region", { name: "선택한 오프라인 시험 기록" });
  expect(within(offline).getByText(/실행 방식: 앱 안에서 고정 합성 응답을 생성합니다/)).toBeTruthy();
  expect(within(offline).queryByText(/HTTP 제한 시간|요청 reasoning:/)).toBeNull();
  expect(posts()).toHaveLength(0);
});
