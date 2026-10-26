import { StrictMode } from "react";
import { webcrypto } from "node:crypto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { AnthropicTrialPanel } from "../components/AnthropicTrialPanel";
import type { AnthropicTrial, TrialEnvelope } from "../../lib/anthropicTrial";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/settings/model-connections/anthropic_api/first-trial";
const hash = "a".repeat(64), raw = '<img src=x onerror="alert(1)"> e\u0301';
function prepared(changes: Partial<AnthropicTrial> = {}): AnthropicTrial {
  return { trial_id: "trial-fixture", binding_sha256: hash, state: "PREPARED", model: "claude-opus-5-5",
    max_output_tokens: 2200, credential_revision: 2, expires_at: "2099-10-08T06:30:00Z", origin: "FIXTURE_SYNTHETIC",
    source: { title: "공개 논문 합성 발췌", pdf_sha256: "b".repeat(64), page: 2, start: 0, end: 6,
      excerpt_sha256: "c".repeat(64), excerpt: "한글 e\u0301🙂" }, raw_capture_available: false, actual_model: null,
    response_id: null, usage: null, draft: null, http_status: null, error_code: null, attempts: 0, can_execute: true,
    scientific_support: "NOT_ASSESSED", full_document_review: "NOT_ESTABLISHED", ...changes };
}
const envelope = (trial: AnthropicTrial | null = prepared(), changes: Partial<TrialEnvelope> = {}): TrialEnvelope => ({ enabled: true, reason: null, trial, ...changes });
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");
const execute = () => screen.getByRole("button", { name: "확인한 발췌로 1회 시험 실행" });
const refresh = () => screen.getByRole("button", { name: "Anthropic 시험 상태 새로고침" });
const cancel = () => screen.getByRole("button", { name: "Anthropic 시험 취소" });
const finished = (state: AnthropicTrial["state"] = "STRUCTURE_VALID") => prepared({ state, attempts: 1, can_execute: false,
  raw_capture_available: true, actual_model: "reported-model", response_id: "synthetic-response", http_status: 200,
  usage: { input_tokens: 10, output_tokens: 20, cache_read_input_tokens: 3 }, draft: state === "STRUCTURE_VALID" ? { summary: "발췌의 구조적 응답" } : null });
async function open() { const view = render(<AnthropicTrialPanel/>); await screen.findByText("공개 논문 합성 발췌"); return view; }
beforeEach(() => { vi.resetAllMocks(); vi.stubGlobal("crypto", webcrypto); vi.mocked(api.fetchJson).mockResolvedValue(envelope()); });

const outputDiagnostic = (): NonNullable<AnthropicTrial["output_diagnostic"]> => ({ schema_version: "ANTHROPIC_TRIAL_OUTPUT_DIAGNOSTIC_V1", source: "SAVED_RAW_RESPONSE",
  raw_response_sha256: "f".repeat(64), reported_model: "response-haiku", response_id: "msg_saved",
  usage: { input_tokens: 4530, output_tokens: 162 },
  reason_codes: ["PAPER_ID_MISMATCH", "SOURCE_HASH_MISMATCH", "MISSING_CITATIONS"] });

it("reads rejected response metadata and reasons without changing state or enabling execution", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope({ ...finished("STRUCTURE_INVALID"), actual_model: null,
    response_id: null, usage: null, output_diagnostic: outputDiagnostic() } as AnthropicTrial));
  const view = await open();
  expect(screen.getByRole("region", { name: "응답 검증 진단" })).toBeTruthy();
  expect(screen.getByText("응답의 논문 ID가 선택한 논문과 다릅니다.")).toBeTruthy();
  expect(screen.getByText("응답의 본문 해시가 전송한 발췌와 다릅니다.")).toBeTruthy();
  expect(screen.getByText("인용 ID가 없거나 비어 있습니다.")).toBeTruthy();
  expect(screen.getByText(/Provider 보고 모델: response-haiku.*msg_saved/)).toBeTruthy();
  expect(screen.getByText(/비캐시 입력.*4530.*출력.*162/)).toBeTruthy();
  expect(screen.getByText(/저장된 원시 응답.*원본 이력.*바꾸지/)).toBeTruthy();
  expect(screen.queryByText(/구조 검증된 초안 보기/)).toBeNull();
  fireEvent.click(refresh()); await screen.findByRole("status");
  expect(execute()).toHaveProperty("disabled", true);
  view.unmount(); await open();
  expect(screen.getByText(/Provider 보고 모델: response-haiku/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it.each([
  { reason_codes: ["untrusted-server-text"] }, { reason_codes: [] },
  { reason_codes: ["PAPER_ID_MISMATCH", "PAPER_ID_MISMATCH"] },
  { usage: { input_tokens: -1 } }, { usage: { invented_tokens: 1 } },
  { raw_response_sha256: "not-a-hash" }, { reported_model: "bad\nmodel" },
  { source: "LIVE_REEXECUTION" },
])("rejects malformed derived diagnostics without exposing arbitrary reasons", async change => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope({ ...finished("STRUCTURE_INVALID"),
    output_diagnostic: { ...outputDiagnostic(), ...change } } as AnthropicTrial));
  render(<AnthropicTrialPanel/>); await screen.findByRole("alert");
  expect(screen.queryByRole("region", { name: "응답 검증 진단" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it.each(["PREPARED", "STRUCTURE_VALID", "CANCELLED"] as const)("rejects output diagnostics attached to %s", async state => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope({ ...(state === "PREPARED" ? prepared() : finished(state)),
    output_diagnostic: outputDiagnostic() } as AnthropicTrial));
  render(<AnthropicTrialPanel/>); await screen.findByRole("alert");
  expect(posts()).toHaveLength(0);
});

it("refuses a self-consistent capture whose hash differs from the displayed diagnostic", async () => {
  const digest = Buffer.from(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(raw))).toString("hex");
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith("/capture")
    ? { raw_response: raw, sha256: digest, trial_id: "trial-fixture", binding_sha256: hash }
    : envelope({ ...finished("STRUCTURE_INVALID"), output_diagnostic: outputDiagnostic() }));
  await open(); fireEvent.click(screen.getByRole("button", { name: "저장된 원시 응답 보기" }));
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("저장된 원시 응답")).toBeNull();
  expect(screen.getByText(/Provider 보고 모델: response-haiku/)).toBeTruthy();
  expect(execute()).toHaveProperty("disabled", true); expect(posts()).toHaveLength(0);
});

it.each([
  ["SCHEMA_COMPLEXITY", /응답 형식이 너무 복잡/],
  ["INSUFFICIENT_CREDITS", /API 크레딧 부족/],
] as const)("explains saved %s without changing the historical state or resending", async (diagnostic, label) => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope({ ...finished("UNKNOWN"), http_status: 400, provider_diagnostic: diagnostic }));
  await open();
  expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByText("Provider가 요청을 거절함 · 자동 재실행하지 않음")).toBeTruthy();
  expect(screen.getByText(/기존 저장 상태: UNKNOWN/)).toBeTruthy();
  expect(execute()).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(0);
});

it("shows the bound inactivity limits and saved transport milestones without enabling another attempt", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope(finished("UNKNOWN") as AnthropicTrial, { trial: {
    ...finished("UNKNOWN"), http_timeouts: { connect: 10, read: 300, write: 30, pool: 10 },
    transport_trace: [{ event: "http11.receive_response_headers.failed", elapsed_ms: 300120 }],
  } }));
  await open();
  expect(screen.getByText(/응답 읽기 300초/)).toBeTruthy();
  expect(screen.getByText(/전체 실행 시간의 상한/)).toBeTruthy();
  fireEvent.click(screen.getByText("저장된 통신 단계 보기"));
  expect(screen.getByText(/응답 헤더.*실패.*300.120초/)).toBeTruthy();
  expect(execute()).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(0);
});

it.each([
  { http_timeouts: { connect: 10, read: 0, write: 30, pool: 10 } },
  { transport_trace: [{ event: "request-secret", elapsed_ms: 1 }] },
  { transport_trace: [{ event: "http11.receive_response_headers.started", elapsed_ms: -1 }] },
])("does not offer execution for malformed timeout/transport data", async changes => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope(prepared(changes)));
  render(<AnthropicTrialPanel/>);
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "확인한 발췌로 1회 시험 실행" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("rejects a returned timeout policy change even when the server repeats the prior binding", async () => {
  const limits = { connect: 10, read: 300, write: 30, pool: 10 };
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST"
    ? envelope({ ...finished(), http_timeouts: { ...limits, read: 600 } })
    : envelope(prepared({ http_timeouts: limits })));
  await open(); fireEvent.click(execute()); await screen.findByRole("alert");
  expect(screen.getByText(/응답 미확인 요청/)).toBeTruthy();
  expect(screen.getByText(/응답 읽기 300초/)).toBeTruthy();
  expect(execute()).toHaveProperty("disabled", true); expect(posts()).toHaveLength(1);
});

it("previews source, model, limits and synthetic boundaries without automatic execution", async () => {
  await open();
  expect(screen.getByText("한글 e\u0301🙂")).toBeTruthy();
  expect(screen.getByText(/합성 시험.*실제 모델 실행의 증거가 아닙니다/)).toBeTruthy();
  expect(screen.getByText(/논문 전체 검토가 아닙니다/)).toBeTruthy();
  expect(screen.getByText(/thinking.*포함/)).toBeTruthy();
  expect(screen.getByText(/자동 재실행.*자동 채택/)).toBeTruthy();
  expect(screen.getByText(/claude-opus-5-5/)).toBeTruthy();
  expect(execute()).toHaveProperty("disabled", false); expect(posts()).toHaveLength(0);
});

it("requires explicit keyboard action and sends only the inspected binding and intent header once", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST" ? envelope(finished()) : envelope());
  await open(); const user = userEvent.setup(); execute().focus(); await user.keyboard("{Enter}");
  await screen.findByText("구조 검증 통과 · 과학적 타당성 미평가");
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(root + "/execute");
  expect(posts()[0][1]?.headers).toMatchObject({ "X-XRML-Trial-Action": "1" });
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ expected_binding_sha256: hash });
  expect(screen.getByText(/Provider 보고 모델: reported-model/)).toBeTruthy();
  expect(screen.getByText(/비캐시 입력.*10/)).toBeTruthy();
  expect(screen.getByText(/캐시 읽기.*3/)).toBeTruthy();
  expect(screen.getByText(/^선택한 발췌만 전달하며.*과학적 타당성은 미평가/)).toBeTruthy();
  expect(execute()).toHaveProperty("disabled", true);
});

it.each([new Error("lost"), new api.ApiError("failed", 503, "{}"), new api.ApiError("timeout", 408, "{}"), new api.ApiError("limit", 429, "{}")])(
  "retains the frozen pending binding after uncertain response, refreshing without an execute retry", async failure => {
    let reads = 0;
    vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => {
      if (init?.method === "POST") throw failure;
      return ++reads === 1 ? envelope() : envelope(null, { enabled: false, reason: "anthropic_trial_disabled" });
    });
    await open(); fireEvent.click(execute()); await screen.findByRole("alert");
    expect(screen.getByText(/응답 미확인 요청/)).toBeTruthy();
    expect(execute()).toHaveProperty("disabled", true);
    fireEvent.click(refresh()); await screen.findByText(/이 서버에서는.*비활성화/);
    expect(screen.getByText(/응답 미확인 요청/)).toBeTruthy();
    expect(screen.getAllByText(hash).length).toBeGreaterThan(0); expect(posts()).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /재실행|실행 재시도/ })).toBeNull();
  });

it("resolves a lost response by reading the matching saved result without another POST", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => {
    if (init?.method === "POST") throw new Error("lost response");
    return ++reads === 1 ? envelope() : envelope(finished());
  });
  await open(); fireEvent.click(execute()); await screen.findByRole("alert");
  fireEvent.click(refresh()); await screen.findByText("구조 검증 통과 · 과학적 타당성 미평가");
  expect(screen.queryByText(/응답 미확인 요청/)).toBeNull(); expect(posts()).toHaveLength(1);
});

it("rejects a success response for another binding and preserves the original pending body", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST"
    ? envelope(finished() as AnthropicTrial | null, { trial: { ...finished(), binding_sha256: "d".repeat(64) } }) : envelope());
  await open(); fireEvent.click(execute()); await screen.findByRole("alert");
  expect(screen.getByText(/응답 미확인 요청/)).toBeTruthy();
  expect(screen.queryByText("구조 검증 통과 · 과학적 타당성 미평가")).toBeNull();
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ expected_binding_sha256: hash });
});

it("requires reinspection after a definitive rejection without turning it into empty success", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => {
    if (init?.method === "POST") throw new api.ApiError("changed", 409, "{}"); return envelope();
  });
  await open(); fireEvent.click(execute()); await screen.findByRole("alert");
  expect(execute()).toHaveProperty("disabled", true); expect(screen.queryByText(/응답 미확인 요청/)).toBeNull();
  fireEvent.click(refresh()); await waitFor(() => expect(execute()).toHaveProperty("disabled", false));
  expect(posts()).toHaveLength(1);
});

it("allows safety cancellation after lost response and preserves its exact binding", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (url, init) => {
    if (url === root + "/execute") throw new Error("lost");
    if (init?.method === "POST") return envelope(prepared({ state: "CANCELLED", can_execute: false }));
    return envelope();
  });
  await open(); fireEvent.click(execute()); await screen.findByRole("alert"); fireEvent.click(cancel());
  await screen.findByText("취소됨 · 새 실행 없음");
  expect(posts()).toHaveLength(2); expect(posts()[1][0]).toBe(root + "/cancel");
  expect(posts()[1][1]?.body).toBe(posts()[0][1]?.body); expect(execute()).toHaveProperty("disabled", true);
});

it.each(["STRUCTURE_VALID", "UNKNOWN"] as const)("does not claim cancellation when %s was already recorded on the server", async state => {
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST" ? envelope(finished(state)) : envelope());
  await open(); fireEvent.click(cancel());
  await screen.findByText(/이미 종료 상태가 기록되어 취소로 변경하지 않았습니다/);
  expect(screen.queryByText(/^취소 기록을 확인했습니다/)).toBeNull();
  expect(screen.getByText(state === "STRUCTURE_VALID" ? "구조 검증 통과 · 과학적 타당성 미평가" : "실행 결과 불명 · 자동 재실행하지 않음")).toBeTruthy();
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toBe(root + "/cancel");
});

it("lets refresh and cancel run while execution waits and ignores the stale completion after cancellation", async () => {
  let resolveExecution!: (value: unknown) => void, reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (url, init) => {
    if (url === root + "/execute") return new Promise(resolve => { resolveExecution = resolve; });
    if (init?.method === "POST") return envelope(prepared({ state: "CANCELLED", attempts: 1, can_execute: false }));
    return ++reads === 1 ? envelope() : envelope(prepared({ state: "CLAIMED", attempts: 1, can_execute: false }));
  });
  await open(); fireEvent.click(execute()); fireEvent.click(execute()); expect(posts()).toHaveLength(1);
  fireEvent.click(refresh()); await screen.findByText("시도 시작됨 · 결과 대기");
  fireEvent.click(cancel()); await screen.findByText("취소됨 · 새 실행 없음");
  await act(async () => resolveExecution(envelope(finished())));
  expect(screen.getByText("취소됨 · 새 실행 없음")).toBeTruthy();
  expect(screen.queryByText("구조 검증 통과 · 과학적 타당성 미평가")).toBeNull(); expect(posts()).toHaveLength(2);
});

it.each(["CAPTURED", "STRUCTURE_INVALID", "INCOMPLETE", "REFUSED", "UNKNOWN", "EXPIRED"] as const)("shows %s honestly and never enables reexecution", async state => {
  vi.mocked(api.fetchJson).mockResolvedValue(envelope(finished(state)));
  await open(); expect(execute()).toHaveProperty("disabled", true); expect(posts()).toHaveLength(0);
  expect(screen.getByText(/과학적.*미평가/)).toBeTruthy();
});

it.each([
  envelope(null, { enabled: false, reason: "anthropic_trial_disabled" }),
  envelope(prepared({ expires_at: "2000-01-01T00:00:00Z" })),
  envelope(prepared({ can_execute: false })),
])("does not execute a disabled or expired preview", async value => {
  vi.mocked(api.fetchJson).mockResolvedValue(value); render(<AnthropicTrialPanel/>);
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.queryByText("Anthropic 시험 상태 확인 중")).toBeNull());
  const button = screen.queryByRole("button", { name: "확인한 발췌로 1회 시험 실행" });
  if (button) expect(button).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(0);
});

it("reveals only explicitly requested, hash-verified raw capture as escaped text", async () => {
  const digest = Buffer.from(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(raw))).toString("hex");
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith("/capture")
    ? { raw_response: raw, sha256: digest, trial_id: "trial-fixture", binding_sha256: hash } : envelope(finished()));
  await open(); expect(api.fetchJson).toHaveBeenCalledTimes(1); expect(screen.queryByText(raw)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "저장된 원시 응답 보기" }));
  await screen.findByText(raw); expect(document.querySelector("img")).toBeNull(); expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "원시 응답 숨기기" })); expect(screen.queryByText(raw)).toBeNull();
});

it.each(["hash", "binding", "unavailable"])("does not reveal %s capture as the displayed trial output", async kind => {
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (!url.endsWith("/capture")) return envelope(finished());
    if (kind === "unavailable") throw new api.ApiError("unavailable", 409, "{}");
    return { raw_response: raw, sha256: "0".repeat(64), trial_id: "trial-fixture", binding_sha256: kind === "binding" ? "d".repeat(64) : hash };
  });
  await open(); fireEvent.click(screen.getByRole("button", { name: "저장된 원시 응답 보기" })); await screen.findByRole("alert");
  expect(screen.queryByText(raw)).toBeNull(); expect(posts()).toHaveLength(0);
});

it("ignores late read and execution responses on unmount and never executes on remount", async () => {
  let resolveExecution!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST"
    ? new Promise(resolve => { resolveExecution = resolve; }) : envelope());
  const view = await open(); fireEvent.click(execute()); view.unmount();
  render(<AnthropicTrialPanel/>); await screen.findByText("공개 논문 합성 발췌");
  await act(async () => resolveExecution(envelope(finished())));
  expect(screen.queryByText("구조 검증 통과 · 과학적 타당성 미평가")).toBeNull(); expect(posts()).toHaveLength(1);
});

it("ignores superseded StrictMode reads", async () => {
  const pending: Array<(value: unknown) => void> = [];
  vi.mocked(api.fetchJson).mockImplementation(async () => new Promise(resolve => pending.push(resolve)));
  render(<StrictMode><AnthropicTrialPanel/></StrictMode>); await waitFor(() => expect(pending).toHaveLength(2));
  await act(async () => pending[1](envelope(finished()))); await act(async () => pending[0](envelope()));
  expect(screen.getByText("구조 검증 통과 · 과학적 타당성 미평가")).toBeTruthy(); expect(execute()).toHaveProperty("disabled", true);
});

it("does not apply an old execution response after inspection switched to a different frozen trial", async () => {
  let resolveExecution!: (value: unknown) => void, reads = 0;
  const other = prepared({ trial_id: "other-trial", binding_sha256: "d".repeat(64),
    source: { ...prepared().source, title: "다른 고정 발췌" } });
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST"
    ? new Promise(resolve => { resolveExecution = resolve; }) : ++reads === 1 ? envelope() : envelope(other));
  await open(); fireEvent.click(execute()); fireEvent.click(refresh()); await screen.findByText("다른 고정 발췌");
  await act(async () => resolveExecution(envelope(finished())));
  expect(screen.getByText("다른 고정 발췌")).toBeTruthy();
  expect(screen.queryByText("구조 검증 통과 · 과학적 타당성 미평가")).toBeNull();
  expect(screen.getByText(/응답 미확인 요청/)).toBeTruthy(); expect(execute()).toHaveProperty("disabled", true);
});

it("ignores a raw capture response superseded by a new trial inspection", async () => {
  let resolveRaw!: (value: unknown) => void, reads = 0;
  const digest = Buffer.from(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(raw))).toString("hex");
  const other = { ...finished(), trial_id: "other-trial", binding_sha256: "d".repeat(64) };
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith("/capture")
    ? new Promise(resolve => { resolveRaw = resolve; }) : ++reads === 1 ? envelope(finished()) : envelope(other));
  await open(); fireEvent.click(screen.getByRole("button", { name: "저장된 원시 응답 보기" })); fireEvent.click(refresh());
  await screen.findByText("other-trial");
  await act(async () => resolveRaw({ raw_response: raw, sha256: digest, trial_id: "trial-fixture", binding_sha256: hash }));
  expect(screen.queryByText(raw)).toBeNull(); expect(posts()).toHaveLength(0);
});

it("rejects a mutation response that changes the source while copying the original binding hash", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST"
    ? envelope({ ...finished(), source: { ...finished().source, title: "바뀐 입력" } }) : envelope());
  await open(); fireEvent.click(execute()); await screen.findByRole("alert");
  expect(screen.queryByText("바뀐 입력")).toBeNull(); expect(screen.getByText(/응답 미확인 요청/)).toBeTruthy();
});

it.each([envelope(prepared({ max_output_tokens: 0 })), envelope(prepared({ attempts: 1, can_execute: true })),
  envelope(prepared({ source: { ...prepared().source, end: 0 } })), envelope(prepared({ scientific_support: "VERIFIED" as never })),
  envelope(prepared({ usage: { input_tokens: -1 } }))])("rejects malformed preview instead of presenting an executable success", async value => {
  vi.mocked(api.fetchJson).mockResolvedValue(value); render(<AnthropicTrialPanel/>); await screen.findByRole("alert");
  expect(screen.queryByText("공개 논문 합성 발췌")).toBeNull(); expect(posts()).toHaveLength(0);
});
