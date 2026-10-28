import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewDocumentPlan } from "../components/ReviewDocumentPlan";

vi.mock("../../lib/api", async original => ({...await original<typeof api>(), fetchJson: vi.fn()}));
const source = {run_id: "run", paper_id: "paper", source_digest: "a".repeat(64), source_chars: 90000, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 90000, extraction_truncation: "UNKNOWN"};
const settings = {default_connector_id: "anthropic_api", max_output_tokens: 2200, safety_tokens: 8192,
  choices: [{connector_id: "anthropic_api", display_name: "Claude API", provider_name: "anthropic", model: "claude-haiku-5-5", context_tokens: 1000000, docs_url: "https://platform.claude.com/docs", budget_source: "DOCUMENTED"}]};
const plan = {schema_version: "PAPER_DOCUMENT_PLAN_V1", plan_id: "rplan_" + "c".repeat(64), plan_sha256: "c".repeat(64), created_at: "2026-10-18T01:00:00Z", source, topic_id: "topic", paper_version_id: "version", actor_id: "owner", request_id: "request",
  budget: {provider_name: "anthropic", model: "claude-haiku-5-5", context_tokens: 1000000, input_tokens: 989608, max_output_tokens: 2200, safety_tokens: 8192, estimator: "UTF8_BYTES_CONSERVATIVE_V1"},
  budget_source: "DOCUMENTED", mode: "WHOLE_EXTRACTED_TEXT", outline: [], full_document_review: "NOT_ESTABLISHED",
  units: [{index: 0, start: 0, end: 90000, label: "whole", estimated_input_tokens: 98000, preparation_id: null, expected_preparation_id: "rprep_" + "d".repeat(64), receipts: [], state: "PLANNED_NOT_EXECUTED"}]};
const busy = vi.fn(), openPrep = vi.fn(), openResult = vi.fn();
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("/configuration")) return settings as never;
    if (url.includes("?page=")) return {items: [plan], hasMore: false} as never;
    if (url.endsWith("/prepare")) return {schema_version: "PAPER_REVIEW_PREPARATION_V3", preparation_id: "rprep_" + "d".repeat(64), topic_id: "topic", paper_version_id: "version", actor_id: "owner", request_id: "request", state: "PREPARED_NOT_EXECUTED", execution: {started: false}, source, selection: {start: 0, end: 90000, text: "a".repeat(90000)}, analysis_unit: {plan_id: plan.plan_id, plan_sha256: plan.plan_sha256, unit_index: 0, unit_count: 1, budget: plan.budget}} as never;
    return (options?.method === "POST" ? {...plan, request_id: JSON.parse(options.body as string).request_id} : plan) as never;
  });
});
async function open() {
  const view = render(<ReviewDocumentPlan root="/api/topics/topic/paper-bank/version/review-preparations" source={source} onBusyChange={busy} onOpenPreparation={openPrep} onOpenReceipt={openResult}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("모델 예산으로 전문·절별 분석 준비"));
  await screen.findByLabelText("계획 모델 ID");
  return view;
}
it("plans the whole selected source by model tokens without a 12000 character range or model dispatch", async () => {
  await open();
  expect(screen.getByLabelText("모델 컨텍스트 토큰")).toHaveProperty("value", "1000000");
  fireEvent.click(screen.getByRole("button", {name: "전문 분석 계획 저장"}));
  await screen.findByText("전체 추출 본문 1개 요청");
  const body = JSON.parse(posts()[0][1]!.body as string);
  expect(body.input_tokens).toBe(989608); expect(body.expected_source_digest).toBe(source.source_digest);
  expect(body).not.toHaveProperty("end"); expect(body).not.toHaveProperty("approval");
  expect(posts()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", {name: "구간 1 분석 요청 준비"}));
  await waitFor(() => expect(openPrep).toHaveBeenCalledWith("rprep_" + "d".repeat(64)));
  expect(posts()).toHaveLength(2);
  expect(posts().every(([url]) => url.includes("document-plans"))).toBe(true);
});
it("rejects overflow and requires a new budget after changing the model", async () => {
  await open();
  fireEvent.change(screen.getByLabelText("요청당 입력 토큰 예산"), {target: {value: "999999"}});
  expect(screen.getByRole("button", {name: "전문 분석 계획 저장"})).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("계획 모델 ID"), {target: {value: "new-model"}});
  expect(screen.getByLabelText("모델 컨텍스트 토큰")).toHaveProperty("value", "");
  expect(posts()).toHaveLength(0);
});
it("preserves unknown save body across collapse and retries the same local request", async () => {
  await open();
  let failed = false;
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST" && !failed) { failed = true; throw new Error("lost response"); }
    return previous(url, options);
  });
  fireEvent.click(screen.getByRole("button", {name: "전문 분석 계획 저장"}));
  await screen.findByText(/lost response/);
  expect(screen.getByRole("button", {name: "저장된 모델 설정 확인"}).matches(":disabled")).toBe(true);
  expect(screen.getByRole("button", {name: "선택 연결의 저장 모델 적용"}).matches(":disabled")).toBe(true);
  fireEvent.click(screen.getByText("모델 예산으로 전문·절별 분석 준비"));
  await waitFor(() => expect(screen.getByText("모델 예산으로 전문·절별 분석 준비").closest("details")?.open).toBe(false));
  fireEvent.click(screen.getByText("모델 예산으로 전문·절별 분석 준비"));
  fireEvent.click(await screen.findByRole("button", {name: "같은 전문 계획 요청 확인"}));
  await screen.findByText("전체 추출 본문 1개 요청");
  expect(posts()[0][1]!.body).toBe(posts()[1][1]!.body);
});
it("opens linked synthetic results and refreshes with GET only, using keyboard", async () => {
  const saved = {...plan, units: [{...plan.units[0], preparation_id: "rprep_" + "d".repeat(64), receipts: [{receipt_id: "receipt", input_origin: "FIXTURE_SYNTHETIC", validated_response: true, status: "needs_review"}]}]};
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => url.endsWith("/" + plan.plan_id) ? saved as never : previous(url, options));
  await open();
  const button = screen.getByRole("button", {name: /claude-haiku-5-5 · 1개 분석 구간/}); button.focus();
  await userEvent.keyboard("{Enter}");
  fireEvent.click(await screen.findByRole("button", {name: "구간 1 근거·결과 열기"}));
  expect(openResult).toHaveBeenCalledWith("receipt");
  expect(screen.getByText(/합성 시험/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name: "이 계획 결과 다시 조회"}));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledWith("/api/topics/topic/paper-bank/version/review-preparations/document-plans/" + plan.plan_id));
  expect(posts()).toHaveLength(0);
});
it("keeps model input after a rejected save and provides history without a source", async () => {
  const view = await open();
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") throw new api.ApiError("source_changed", 409, "");
    return previous(url, options);
  });
  fireEvent.click(screen.getByRole("button", {name: "전문 분석 계획 저장"}));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "claude-haiku-5-5");
  view.rerender(<ReviewDocumentPlan root="/api/topics/topic/paper-bank/version/review-preparations" onBusyChange={busy} onOpenPreparation={openPrep}/>);
  expect(screen.getByRole("button", {name: "전문 분석 계획 저장"})).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", {name: /claude-haiku-5-5 · 1개 분석 구간/}));
  await screen.findByText("전체 추출 본문 1개 요청");
});

it("retains the exact pending request when an ACK belongs to another source or request", async () => {
  await open();
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  let wrong = true;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST" && wrong) { wrong = false; return {...plan, request_id: "another-request"} as never; }
    return previous(url, options);
  });
  fireEvent.click(screen.getByRole("button", {name: "전문 분석 계획 저장"}));
  await screen.findByText(/document_plan_response_mismatch/);
  expect(screen.queryByRole("region", {name: "저장된 전문 분석 계획"})).toBeNull();
  fireEvent.click(screen.getByRole("button", {name: "같은 전문 계획 요청 확인"}));
  await screen.findByText("전체 추출 본문 1개 요청");
  expect(posts()[0][1]!.body).toBe(posts()[1][1]!.body);
});

it("does not open a mismatched unit ACK and keeps the same unit for recovery", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", {name: /claude-haiku-5-5 · 1개 분석 구간/}));
  await screen.findByText("전체 추출 본문 1개 요청");
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  let wrong = true;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    const value = await previous(url, options);
    if (url.endsWith("/prepare") && wrong) { wrong = false; return {...value as object, preparation_id: "rprep_" + "e".repeat(64)} as never; }
    return value;
  });
  fireEvent.click(screen.getByRole("button", {name: "구간 1 분석 요청 준비"}));
  await screen.findByText(/document_plan_unit_response_mismatch/);
  expect(openPrep).not.toHaveBeenCalled();
  expect(screen.getByRole("button", {name: "저장된 모델 설정 확인"}).matches(":disabled")).toBe(true);
  expect(screen.getByRole("button", {name: "선택 연결의 저장 모델 적용"}).matches(":disabled")).toBe(true);
  fireEvent.click(screen.getByRole("button", {name: "같은 분석 구간 준비 확인"}));
  await waitFor(() => expect(openPrep).toHaveBeenCalledWith("rprep_" + "d".repeat(64)));
  expect(posts()[0][0]).toBe(posts()[1][0]);
});

it("refreshes stored choices without overwriting a draft and explicitly applies only to the next plan", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", {name: /claude-haiku-5-5 · 1개 분석 구간/}));
  const saved = await screen.findByRole("region", {name: "저장된 전문 분석 계획"});
  const preserved = saved.textContent;
  fireEvent.change(screen.getByLabelText("계획 모델 ID"), {target: {value: "my-unsaved-model"}});
  for (const [label, value] of [["모델 컨텍스트 토큰", "200000"], ["요청당 입력 토큰 예산", "180000"], ["출력 토큰 예약", "1500"], ["추정 오차 여유 토큰", "4096"]]) {
    fireEvent.change(screen.getByLabelText(label), {target: {value}});
  }
  const changed = {...settings, choices: [{...settings.choices[0], model: "new-stored-model", context_tokens: 500000}]};
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => url.endsWith("/configuration") ? changed as never : previous(url, options));
  fireEvent.click(screen.getByRole("button", {name: "저장된 모델 설정 확인"}));
  await screen.findByText("최근 확인한 저장 모델: new-stored-model");
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "my-unsaved-model");
  expect(screen.getByLabelText("모델 컨텍스트 토큰")).toHaveProperty("value", "200000");
  expect(screen.getByLabelText("요청당 입력 토큰 예산")).toHaveProperty("value", "180000");
  expect(screen.getByLabelText("출력 토큰 예약")).toHaveProperty("value", "1500");
  expect(screen.getByLabelText("추정 오차 여유 토큰")).toHaveProperty("value", "4096");
  expect(saved.textContent).toBe(preserved); expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", {name: "선택 연결의 저장 모델 적용"}));
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "new-stored-model");
  expect(screen.getByLabelText("모델 컨텍스트 토큰")).toHaveProperty("value", "500000");
  expect(screen.getByLabelText("요청당 입력 토큰 예산")).toHaveProperty("value", "489608");
  expect(screen.getByLabelText("출력 토큰 예약")).toHaveProperty("value", "2200");
  expect(screen.getByLabelText("추정 오차 여유 토큰")).toHaveProperty("value", "8192");
  expect(saved.textContent).toBe(preserved); expect(posts()).toHaveLength(0);
  expect(within(saved).getByText(/anthropic · claude-haiku-5-5/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name: "전문 계획 이력 새로고침"}));
  await waitFor(() => expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url.includes("?page=")).length).toBe(2));
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url.endsWith("/configuration"))).toHaveLength(2);
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "new-stored-model");
});

it("retains the last checked Settings and unsaved input when explicit metadata refresh fails", async () => {
  await open();
  fireEvent.change(screen.getByLabelText("계획 모델 ID"), {target: {value: "unsaved-model"}});
  vi.mocked(api.fetchJson).mockRejectedValue(new Error("settings_unavailable"));
  fireEvent.click(screen.getByRole("button", {name: "저장된 모델 설정 확인"}));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "unsaved-model");
  expect(screen.getByLabelText("모델 컨텍스트 토큰")).toHaveProperty("value", "");
  expect(screen.getByText("최근 확인한 저장 모델: claude-haiku-5-5")).toBeTruthy();
  expect(screen.queryByText(/저장된 모델 설정을 확인했습니다/)).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("keeps a removed connector visible without silently switching or enabling new plans", async () => {
  await open();
  vi.mocked(api.fetchJson).mockResolvedValue({...settings, default_connector_id: "openai_api", choices: [{...settings.choices[0], connector_id: "openai_api", display_name: "OpenAI API", provider_name: "openai", model: "other-model", context_tokens: null}]} as never);
  fireEvent.click(screen.getByRole("button", {name: "저장된 모델 설정 확인"}));
  await screen.findByText(/선택한 연결이 최근 확인한 설정 목록에 없습니다/);
  expect(screen.getByLabelText("분석 연결")).toHaveProperty("value", "anthropic_api");
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "claude-haiku-5-5");
  expect(screen.getByRole("button", {name: "선택 연결의 저장 모델 적용"})).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", {name: "전문 분석 계획 저장"})).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(0);
});

const localCapability = { schema_version: "OLLAMA_LOCAL_CAPABILITY_V1", connector_id: "ollama_local", provider_name: "ollama",
  protocol_schema: "OLLAMA_PREPARED_REVIEW_WIRE_V1", synthetic_protocol_available: true, document_planning_available: true,
  execution_available: false, locality: "LOCALITY_NOT_VERIFIED", runtime_presence: "NOT_CHECKED", model_identity: "NOT_VERIFIED",
  context_capacity: "NOT_VERIFIED", context_request_limit_tokens: 1000000 };
const localSettings = { ...settings, default_connector_id: "ollama_local", choices: [{ connector_id: "ollama_local", display_name: "Ollama · 로컬 런타임",
  provider_name: "ollama", model: "fixture-local:small", context_tokens: null, budget_source: "USER_REQUIRED", docs_url: "https://docs.ollama.com/",
  plan_schema_version: "PAPER_DOCUMENT_PLAN_V2", max_context_tokens: 1000000, local_capability: localCapability }] };
const localPlan = { ...plan, schema_version: "PAPER_DOCUMENT_PLAN_V2", plan_id: "rplan_" + "f".repeat(64), plan_sha256: "f".repeat(64), local_capability: localCapability, budget_source: "USER_CONFIGURED_NOT_VERIFIED",
  budget: { ...plan.budget, provider_name: "ollama", model: "fixture-local:small", context_tokens: 131072, input_tokens: 120000 } };
function useLocalPlans() {
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url.endsWith("/configuration")) return localSettings as never;
    if (url.includes("?page=")) return { items: [localPlan, plan], hasMore: false } as never;
    if (url.endsWith("/" + plan.plan_id)) return plan as never;
    if (url.endsWith("/prepare")) return { schema_version: "PAPER_REVIEW_PREPARATION_V3", preparation_id: localPlan.units[0].expected_preparation_id,
      topic_id: "topic", paper_version_id: "version", actor_id: "owner", state: "PREPARED_NOT_EXECUTED", execution: { started: false }, source,
      selection: { start: 0, end: source.source_chars, text: "a".repeat(source.source_chars) },
      analysis_unit: { plan_id: localPlan.plan_id, plan_sha256: localPlan.plan_sha256, unit_index: 0, unit_count: 1, budget: localPlan.budget } } as never;
    return (options?.method === "POST" ? { ...localPlan, request_id: JSON.parse(options.body as string).request_id } : localPlan) as never;
  });
}
function enterLocalBudget() {
  for (const [label, value] of [["모델 컨텍스트 토큰", "131072"], ["요청당 입력 토큰 예산", "120000"], ["출력 토큰 예약", "2200"], ["추정 오차 여유 토큰", "8192"]])
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
it("requires explicit local budgets and stores V2 plus a prepared unit without any execution endpoint", async () => {
  useLocalPlans(); await open();
  for (const label of ["모델 컨텍스트 토큰", "요청당 입력 토큰 예산", "출력 토큰 예약", "추정 오차 여유 토큰"])
    expect(screen.getByLabelText(label)).toHaveProperty("value", "");
  expect(screen.getByRole("button", { name: "전문 분석 계획 저장" })).toHaveProperty("disabled", true);
  expect(screen.getByText("로컬 실행 미검증 · 문서 계획만 가능")).toBeTruthy();
  enterLocalBudget();
  fireEvent.change(screen.getByLabelText("모델 컨텍스트 토큰"), { target: { value: "1000001" } });
  expect(screen.getByRole("button", { name: "전문 분석 계획 저장" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("모델 컨텍스트 토큰"), { target: { value: "131072" } });
  fireEvent.click(screen.getByRole("button", { name: "전문 분석 계획 저장" }));
  const saved = await screen.findByRole("region", { name: "저장된 전문 분석 계획" });
  expect(within(saved).getByText("로컬 실행 미검증 · 문서 계획만 가능")).toBeTruthy();
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ connector_id: "ollama_local", model: "fixture-local:small", context_tokens: 131072, input_tokens: 120000, max_output_tokens: 2200, safety_tokens: 8192 });
  fireEvent.click(within(saved).getByRole("button", { name: "구간 1 분석 요청 준비" }));
  await waitFor(() => expect(openPrep).toHaveBeenCalledWith(localPlan.units[0].expected_preparation_id));
  expect(posts()).toHaveLength(2);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([url]) => url.startsWith("/api/topics/topic/paper-bank/version/review-preparations/document-plans"))).toBe(true);
});

it("keeps frozen V2 limits when Settings or the next model changes and reads legacy V1 independently", async () => {
  useLocalPlans(); await open();
  fireEvent.click(screen.getByRole("button", { name: /fixture-local:small · 1개 분석 구간/ }));
  const saved = await screen.findByRole("region", { name: "저장된 전문 분석 계획" });
  const frozen = saved.textContent;
  enterLocalBudget();
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => url.endsWith("/configuration")
    ? { ...settings, choices: [...settings.choices, { ...localSettings.choices[0], model: "changed-local" }] } as never : previous(url, options));
  fireEvent.click(screen.getByRole("button", { name: "저장된 모델 설정 확인" }));
  await screen.findByText("최근 확인한 저장 모델: changed-local");
  expect(screen.getByLabelText("계획 모델 ID")).toHaveProperty("value", "fixture-local:small");
  expect(screen.getByLabelText("모델 컨텍스트 토큰")).toHaveProperty("value", "131072");
  fireEvent.click(screen.getByRole("button", { name: "선택 연결의 저장 모델 적용" }));
  expect(screen.getByLabelText("출력 토큰 예약")).toHaveProperty("value", "");
  expect(saved.textContent).toBe(frozen);
  fireEvent.change(screen.getByLabelText("분석 연결"), { target: { value: "anthropic_api" } });
  expect(saved.textContent).toBe(frozen);
  fireEvent.click(screen.getByRole("button", { name: /claude-haiku-5-5 · 1개 분석 구간/ }));
  await waitFor(() => expect(within(saved).queryByText("로컬 실행 미검증 · 문서 계획만 가능")).toBeNull());
  expect(within(saved).getByText(/anthropic · claude-haiku-5-5/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
});

it.each([
  ["missing snapshot", undefined], ["execution claim", { ...localCapability, execution_available: true }],
  ["verified locality claim", { ...localCapability, locality: "VERIFIED" }],
])("retains the same local request after a V2 response with %s", async (_name, capability) => {
  useLocalPlans(); await open(); enterLocalBudget();
  const previous = vi.mocked(api.fetchJson).getMockImplementation()!;
  let corrupt = true;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    const result = await previous(url, options);
    if (options?.method === "POST" && corrupt) { corrupt = false; return { ...result as object, local_capability: capability } as never; }
    return result;
  });
  fireEvent.click(screen.getByRole("button", { name: "전문 분석 계획 저장" }));
  await screen.findByText(/document_plan_local_capability_invalid/);
  expect(screen.queryByRole("region", { name: "저장된 전문 분석 계획" })).toBeNull();
  expect(screen.getByRole("button", { name: "저장된 모델 설정 확인" }).matches(":disabled")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "같은 전문 계획 요청 확인" }));
  await screen.findByRole("region", { name: "저장된 전문 분석 계획" });
  expect(posts()[1][1]!.body).toBe(posts()[0][1]!.body);
});

it("rejects a local configuration that presents an assumed context capacity", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith("/configuration")
    ? { ...localSettings, choices: [{ ...localSettings.choices[0], context_tokens: 131072 }] } as never
    : { items: [], hasMore: false } as never);
  render(<ReviewDocumentPlan root="/api/topics/topic/paper-bank/version/review-preparations" source={source} onBusyChange={busy} onOpenPreparation={openPrep}/>);
  fireEvent.click(screen.getByText("모델 예산으로 전문·절별 분석 준비"));
  await screen.findByText(/document_plan_local_configuration_invalid/);
  expect(screen.queryByRole("button", { name: "전문 분석 계획 저장" })).toBeNull();
  expect(posts()).toHaveLength(0);
});
