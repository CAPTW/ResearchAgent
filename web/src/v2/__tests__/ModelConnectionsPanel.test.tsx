import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ModelConnections, ModelConnectionsUpdate, ModelConnector } from "../../lib/modelConnections";
import * as modelApi from "../../lib/modelConnections";
import { ModelConnectionsPanel } from "../components/ModelConnectionsPanel";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
vi.mock("../../lib/modelConnections", async original => ({ ...await original<typeof modelApi>(), getModelCredential: vi.fn() }));
vi.mock("../components/AnthropicTrialPanel", () => ({ AnthropicTrialPanel: () => <section aria-label="Anthropic 1회 시험"/> }));
const endpoint = "/settings/model-connections";
const connector = (connector_id: string, display_name: string, extra: Partial<ModelConnector> = {}): ModelConnector => ({
  connector_id, display_name, auth_mode: "subscription_cli", provider_name: connector_id, model: "",
  adapter_available: false, credential_env: null, credential_present: null, cli_command: "official login",
  login_instructions: "공식 CLI에서 직접 로그인합니다.", docs_url: "https://example.invalid/docs",
  availability_reason: "XRML 실행 어댑터는 아직 미구현입니다.", ...extra,
});
const initial: ModelConnections = { revision: 0, default_connector_id: "codex_subscription", effective_connector_id: "codex_subscription", environment_override: null, execution_allowed: false, connectors: [
  connector("codex_subscription", "Codex · ChatGPT 구독", { adapter_available: true, cli_command: "codex login", availability_reason: "논문 검토·Journal 어댑터 준비됨. 실제 호출 검증은 별도입니다." }),
  connector("claude_subscription", "Claude Code · 구독", { adapter_available: true, cli_command: "claude auth login", availability_reason: "논문 검토·Journal CLI 어댑터 준비됨. 실제 호출·계정 과금 경로·관리 설정 격리 검증은 아직 미완료입니다." }),
  connector("antigravity_subscription", "Antigravity · Google 계정", { adapter_available: true, cli_command: "agy", availability_reason: "논문 검토·Journal CLI 어댑터 준비됨. 실제 호출·전역 설정 및 Windows 격리 검증은 아직 미완료입니다." }),
  connector("grok_subscription", "Grok Build · 구독 계정", { adapter_available: true, cli_command: "grok login", availability_reason: "논문 검토·Journal ACP 어댑터 준비됨. CLI 내부 도구의 완전 차단은 보장되지 않습니다. 실제 호출·계정 과금 경로·Windows 격리 검증은 아직 미완료입니다." }),
  connector("openai_api", "OpenAI · API 키", { auth_mode: "api_key", adapter_available: true, credential_env: "OPENAI_API_KEY", credential_present: true, cli_command: null, availability_reason: "논문 검토·Journal 어댑터 준비됨." }),
  connector("xai_api", "Grok · xAI API 키", { auth_mode: "api_key", adapter_available: true, credential_env: "XAI_API_KEY", credential_present: false, cli_command: null, availability_reason: "논문 검토·Journal Responses 어댑터 준비됨. API 키 경로이며 실제 호출·요금·모델 검증은 별도입니다." }),
] };
const emptyModels = Object.fromEntries(initial.connectors.map(item => [item.connector_id, ""]));
const puts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "PUT");
const updated = (body: ModelConnectionsUpdate, revision = 1): ModelConnections => ({ ...initial, revision, default_connector_id: body.default_connector_id, effective_connector_id: body.default_connector_id, connectors: initial.connectors.map(item => ({ ...item, model: body.models[item.connector_id] })) });
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(modelApi.getModelCredential).mockImplementation(async connectorId => ({ connector_id: connectorId, revision: 0,
    storage_available: true, stored: false, environment_present: connectorId === "openai_api",
    source: connectorId === "openai_api" ? "environment" : "none", execution_allowed: false }));
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "PUT" ? updated(JSON.parse(init.body as string)) : structuredClone(initial)) as never);
});
async function open() {
  const view = render(<ModelConnectionsPanel/>);
  await screen.findByRole("combobox", { name: "기본 모델 연결" });
  return view;
}
const model = (name = "Codex · ChatGPT 구독") => screen.getByRole("textbox", { name: `${name} 모델` });
const save = () => screen.getByRole("button", { name: "모델 연결 설정 저장" });
const refresh = () => screen.getByRole("button", { name: "모델 연결 상태 새로고침" });

it("shows subscription and API configuration with separate key editors and no login/execution actions", async () => {
  await open();
  expect(screen.getByRole("combobox", { name: "기본 모델 연결" })).toHaveProperty("value", "codex_subscription");
  expect(screen.getByText(/저장은 로그인·모델 실행·실행 권한 승인이 아닙니다/)).toBeTruthy();
  expect(screen.getByText("실행 권한: 미등록")).toBeTruthy();
  expect(screen.getByText(/실제 파이프라인 실행에는 모델 선택이 필요합니다/)).toBeTruthy();
  for (const item of initial.connectors) {
    const card = screen.getByRole("group", { name: item.display_name });
    expect(within(card).getByRole("textbox", { name: `${item.display_name} 모델` })).toHaveProperty("value", "");
    expect(within(card).getByRole("link", { name: `${item.display_name} 공식 안내` })).toHaveProperty("href", item.docs_url);
    expect(within(card).getByText(item.availability_reason)).toBeTruthy();
    expect(within(card).getByText(item.adapter_available ? "연결 어댑터 지원" : "연결 어댑터 미구현")).toBeTruthy();
    if (item.cli_command) expect(within(card).getByText(item.cli_command)).toBeTruthy();
  }
  expect(screen.getByText("OPENAI_API_KEY")).toBeTruthy();
  expect(screen.getByText("XAI_API_KEY")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: /API 키 값|토큰|비밀번호/ })).toBeNull();
  expect(document.querySelectorAll('input[type="password"]')).toHaveLength(2);
  expect(screen.queryByRole("button", { name: /로그인|실행|승인/ })).toBeNull();
  expect(puts()).toHaveLength(0);
  expect(api.fetchJson).toHaveBeenCalledWith(endpoint, { signal: expect.any(AbortSignal) });
});

it("saves an explicit connector and model map by keyboard with the observed revision, allowing unselected models", async () => {
  await open();
  const user = userEvent.setup();
  await user.selectOptions(screen.getByRole("combobox", { name: "기본 모델 연결" }), "openai_api");
  fireEvent.change(model("OpenAI · API 키"), { target: { value: "chosen-model" } });
  save().focus(); await user.keyboard("{Enter}");
  await screen.findByText(/모델 연결 설정을 저장했습니다/);
  expect(puts()).toHaveLength(1);
  expect(puts()[0][0]).toBe(endpoint);
  expect(JSON.parse(puts()[0][1]!.body as string)).toEqual({ expected_revision: 0, default_connector_id: "openai_api", models: { ...emptyModels, openai_api: "chosen-model" } });
  expect(screen.getByText("현재 적용 연결: OpenAI · API 키")).toBeTruthy();
  expect(model("OpenAI · API 키")).toHaveProperty("value", "chosen-model");
});

it("distinguishes the stored choice from an environment override without changing the saved selection", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...initial, effective_connector_id: "disabled", environment_override: "disabled" });
  await open();
  expect(screen.getByText("현재 적용 연결: disabled")).toBeTruthy();
  expect(screen.getByText(/환경 변수 선택.*disabled.*저장한 기본 연결보다 우선합니다/)).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "기본 모델 연결" })).toHaveProperty("value", "codex_subscription");
  expect(puts()).toHaveLength(0);
});

it.each([
  ["network", new Error("lost response"), /저장하지 못했습니다/],
  ["validation", new api.ApiError("invalid", 422, "{}"), /연결 또는 모델 값을 확인하세요/],
  ["admin", new api.ApiError("forbidden", 403, "{}"), /관리자 권한이 없어 저장하지 못했습니다/],
] as const)("preserves exact user input after a %s save failure", async (_name, error, expected) => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => { if (init?.method === "PUT") throw error; return initial as never; });
  await open();
  fireEvent.change(model(), { target: { value: "local-model" } });
  fireEvent.click(save());
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.getByText(expected)).toBeTruthy();
  expect(model()).toHaveProperty("value", "local-model");
  expect(save()).toHaveProperty("disabled", false);
  expect(puts()).toHaveLength(1);
});

it("requires an explicit refresh after a conflict and preserves local edits while adopting the latest revision", async () => {
  let reads = 0, writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") { if (++writes === 1) throw new api.ApiError("changed", 409, "{}"); return updated(JSON.parse(init.body as string), 3) as never; }
    return (++reads === 1 ? initial : { ...initial, revision: 2, default_connector_id: "openai_api", effective_connector_id: "openai_api", connectors: initial.connectors.map(item => ({ ...item, model: "other-tab-model" })) }) as never;
  });
  await open(); fireEvent.change(model(), { target: { value: "keep-my-model" } }); fireEvent.click(save());
  await screen.findByText(/다른 곳에서 설정이 변경되었습니다/);
  expect(save()).toHaveProperty("disabled", true); expect(reads).toBe(1);
  fireEvent.click(refresh());
  await screen.findByText(/입력 중인 값은 유지했습니다/);
  expect(model()).toHaveProperty("value", "keep-my-model");
  expect(screen.getByRole("combobox", { name: "기본 모델 연결" })).toHaveProperty("value", "codex_subscription");
  expect(screen.getByText("현재 적용 연결: OpenAI · API 키")).toBeTruthy();
  fireEvent.click(save()); await screen.findByText(/모델 연결 설정을 저장했습니다/);
  expect(JSON.parse(puts()[1][1]!.body as string)).toEqual({ expected_revision: 2, default_connector_id: "codex_subscription", models: { ...emptyModels, codex_subscription: "keep-my-model" } });
});

it("retries an initial read failure without claiming a connection or sending a save", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("read failed")).mockResolvedValueOnce(initial);
  render(<ModelConnectionsPanel/>);
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.queryByRole("combobox", { name: "기본 모델 연결" })).toBeNull();
  fireEvent.click(refresh()); await screen.findByRole("combobox", { name: "기본 모델 연결" });
  expect(screen.queryByRole("alert")).toBeNull(); expect(puts()).toHaveLength(0);
});

it("keeps edited values on a failed refresh and only resumes saving after a successful read", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async () => { if (++reads === 2) throw new Error("read failed"); return initial as never; });
  await open(); fireEvent.change(model(), { target: { value: "draft-model" } }); fireEvent.click(refresh());
  await screen.findByRole("alert");
  expect(model()).toHaveProperty("value", "draft-model"); expect(save()).toHaveProperty("disabled", true);
  fireEvent.click(refresh()); await screen.findByText(/입력 중인 값은 유지했습니다/);
  expect(model()).toHaveProperty("value", "draft-model"); expect(save()).toHaveProperty("disabled", false);
});

it("ignores superseded reads and preserves edits made while a refresh is in flight", async () => {
  let reads = 0, finishOld!: (value: unknown) => void, finishNew!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") return updated(JSON.parse(init.body as string), 4) as never;
    if (++reads === 1) return initial as never;
    return new Promise(resolve => { if (reads === 2) finishOld = resolve; else finishNew = resolve; }) as never;
  });
  await open(); fireEvent.click(refresh());
  fireEvent.change(model(), { target: { value: "edited-during-read" } }); fireEvent.click(refresh());
  await act(async () => finishNew({ ...initial, revision: 3 }));
  await act(async () => finishOld({ ...initial, revision: 1 }));
  expect(model()).toHaveProperty("value", "edited-during-read");
  expect(vi.mocked(api.fetchJson).mock.calls[1][1]?.signal).toHaveProperty("aborted", true);
  fireEvent.click(save()); await screen.findByText(/모델 연결 설정을 저장했습니다/);
  expect(JSON.parse(puts()[0][1]!.body as string).expected_revision).toBe(3);
});

it("locks duplicate saves and disables editing and reload until the saved response arrives", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => init?.method === "PUT" ? new Promise(resolve => { finish = resolve; }) as never : initial as never);
  await open(); fireEvent.change(model(), { target: { value: "saved-model" } });
  const form = screen.getByRole("form", { name: "모델 연결 설정 저장" });
  fireEvent.submit(form); fireEvent.submit(form);
  expect(puts()).toHaveLength(1); expect(model().matches(":disabled")).toBe(true); expect(refresh()).toHaveProperty("disabled", true);
  await act(async () => finish(updated(JSON.parse(puts()[0][1]!.body as string))));
  expect(save()).toHaveProperty("disabled", false); expect(model()).toHaveProperty("value", "saved-model");
});

it("discards an old StrictMode read and aborts the active read on unmount", async () => {
  const pending: Array<(value: unknown) => void> = [];
  vi.mocked(api.fetchJson).mockImplementation(async () => new Promise(resolve => pending.push(resolve)) as never);
  const view = render(<StrictMode><ModelConnectionsPanel/></StrictMode>);
  await waitFor(() => expect(pending).toHaveLength(2));
  await act(async () => pending[1]({ ...initial, revision: 2, effective_connector_id: "openai_api" }));
  await act(async () => pending[0](initial));
  expect(screen.getByText("현재 적용 연결: OpenAI · API 키")).toBeTruthy();
  fireEvent.click(refresh()); view.unmount();
  expect(vi.mocked(api.fetchJson).mock.calls.at(-1)![1]?.signal).toHaveProperty("aborted", true);
  await act(async () => pending[2](initial)); expect(puts()).toHaveLength(0);
});

it("renders login instructions as text and omits an unsafe documentation URL", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...initial, connectors: [{ ...initial.connectors[0], docs_url: "javascript:alert(1)", login_instructions: '<img src=x onerror="alert(1)">' }] });
  await open();
  expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeTruthy();
  expect(screen.queryByRole("link")).toBeNull(); expect(document.querySelector("img")).toBeNull();
});

it("saves a local runtime model without credential reads, login actions, or inferred installation", async () => {
  const local = connector("ollama_local", "Ollama · 로컬 런타임", { auth_mode: "local_runtime", provider_name: "ollama", cli_command: null,
    availability_reason: "로컬 모델 실행 경로는 아직 연결되지 않았습니다.", login_instructions: "", docs_url: "https://docs.ollama.com/" });
  const localSettings: ModelConnections = { ...initial, schema_version: "MODEL_CONNECTIONS_V2", catalog_schema_version: "MODEL_CONNECTORS_V2",
    default_connector_id: local.connector_id, effective_connector_id: local.connector_id, connectors: [local] };
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method !== "PUT") return localSettings as never;
    const body = JSON.parse(init.body as string) as ModelConnectionsUpdate;
    return { ...localSettings, revision: 1, connectors: [{ ...local, model: body.models.ollama_local }] } as never;
  });
  await open();
  const card = screen.getByRole("group", { name: local.display_name });
  expect(within(card).getByText("로컬 실행 미검증 · 문서 계획만 가능")).toBeTruthy();
  expect(within(card).getByText(/설치 여부·실제 로컬 추론·컨텍스트 용량은 확인하지 않습니다/)).toBeTruthy();
  expect(within(card).queryByText(/공식 CLI 안내|CLI 로그인 상태|환경 변수 이름/)).toBeNull();
  expect(document.querySelector('input[type="password"]')).toBeNull();
  expect(modelApi.getModelCredential).not.toHaveBeenCalled();
  fireEvent.change(model(local.display_name), { target: { value: "fixture-local:small" } });
  save().focus(); await userEvent.keyboard("{Enter}");
  await screen.findByText(/모델 연결 설정을 저장했습니다/);
  expect(JSON.parse(puts()[0][1]!.body as string)).toEqual({ expected_revision: 0, default_connector_id: "ollama_local", models: { ollama_local: "fixture-local:small" } });
  expect(model(local.display_name)).toHaveProperty("value", "fixture-local:small");
  expect(vi.mocked(api.fetchJson).mock.calls.every(([url]) => url === endpoint)).toBe(true);
});
