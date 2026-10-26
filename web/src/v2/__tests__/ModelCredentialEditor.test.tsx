import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ModelConnectionsPanel } from "../components/ModelConnectionsPanel";
import { ModelCredentialEditor } from "../components/ModelCredentialEditor";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const settings = { revision: 0, default_connector_id: "codex_subscription", effective_connector_id: "codex_subscription",
  environment_override: null, execution_allowed: false, connectors: [
    { connector_id: "codex_subscription", display_name: "Codex · ChatGPT 구독", auth_mode: "subscription_cli", provider_name: "codex_cli",
      model: "", adapter_available: true, credential_env: null, credential_present: null, cli_command: "codex login",
      login_instructions: "공식 CLI 로그인", docs_url: "https://example.invalid/codex", availability_reason: "실행 미검증" },
    { connector_id: "anthropic_api", display_name: "Claude · Anthropic API 키", auth_mode: "api_key", provider_name: "anthropic",
      model: "", adapter_available: true, credential_env: "ANTHROPIC_API_KEY", credential_present: false, cli_command: null,
      login_instructions: "API 키 경로", docs_url: "https://example.invalid/anthropic", availability_reason: "실행 미검증" },
  ] };
const credential = { connector_id: "anthropic_api", revision: 0, storage_available: true, stored: false,
  environment_present: false, source: "none", execution_allowed: false };

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/credential") ? credential : settings) as never);
});

it("offers API key management outside the model settings form", async () => {
  render(<ModelConnectionsPanel/>);
  const editor = await screen.findByRole("region", { name: "Claude · Anthropic API 키 자격 증명" });
  const input = within(editor).getByLabelText("Claude · Anthropic API 키 값");
  expect(input).toHaveProperty("type", "password");
  expect(input.getAttribute("autocomplete")).toBe("off");
  expect(input.closest("form")).toBeNull();
  expect(within(editor).getByRole("button", { name: "API 키 저장" })).toBeTruthy();
});

it("fills the Opus 5.5 model without saving or changing the default connector", async () => {
  render(<ModelConnectionsPanel/>);
  const shortcut = await screen.findByRole("button", { name: "Opus 5.5 모델 입력" });
  fireEvent.click(shortcut);
  expect(screen.getByRole("textbox", { name: "Claude · Anthropic API 키 모델" })).toHaveProperty("value", "claude-opus-5-5");
  expect(screen.getByRole("combobox", { name: "기본 모델 연결" })).toHaveProperty("value", "codex_subscription");
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
});

const endpoint = "/settings/model-connections/anthropic_api/credential";
const keyInput = () => screen.getByLabelText("Claude · Anthropic API 키 값") as HTMLInputElement;
const save = () => screen.getByRole("button", { name: "API 키 저장" });
const remove = () => screen.getByRole("button", { name: "저장된 API 키 삭제" });
const refresh = () => screen.getByRole("button", { name: "API 키 상태 새로고침" });
const writes = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => ["PUT", "DELETE"].includes(init?.method ?? ""));
const stored = { ...credential, revision: 1, stored: true, source: "stored" };
async function openEditor() {
  const view = render(<ModelCredentialEditor connectorId="anthropic_api" displayName="Claude · Anthropic API 키"/>);
  await waitFor(() => expect(keyInput()).toHaveProperty("disabled", false));
  return view;
}

it("saves a key with a guarded revision by Enter, clears it, and does not execute or write browser storage", async () => {
  const local = vi.spyOn(Storage.prototype, "setItem");
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "PUT" ? stored : credential) as never);
  const view = await openEditor();
  const user = userEvent.setup();
  fireEvent.change(keyInput(), { target: { value: "  synthetic-key  " } });
  keyInput().focus(); await user.keyboard("{Enter}");
  await screen.findByText(/API 키를 로컬 암호화 저장소에 저장했습니다/);
  expect(writes()).toHaveLength(1);
  expect(writes()[0]).toEqual([endpoint, { method: "PUT", headers: { "Content-Type": "application/json", "X-XRML-Credential-Action": "1" },
    body: JSON.stringify({ expected_revision: 0, api_key: "  synthetic-key  " }) }]);
  expect(keyInput()).toHaveProperty("value", "");
  expect(screen.getByText("적용 경로: 로컬 저장 키")).toBeTruthy();
  expect(local).not.toHaveBeenCalled(); local.mockRestore(); view.unmount();
});

it("deletes only the stored key, clears the input and explains environment fallback", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "DELETE"
    ? { ...credential, revision: 2, environment_present: true, source: "environment" }
    : { ...stored, environment_present: true }) as never);
  await openEditor(); fireEvent.change(keyInput(), { target: { value: "unsubmitted-key" } }); fireEvent.click(remove());
  await screen.findByText("저장된 API 키를 삭제했습니다. 서버 환경 변수의 키가 적용됩니다.");
  expect(writes()).toHaveLength(1);
  expect(JSON.parse(writes()[0][1]!.body as string)).toEqual({ expected_revision: 1 });
  expect(writes()[0][1]?.method).toBe("DELETE");
  expect(writes()[0][1]?.headers).toEqual({ "Content-Type": "application/json", "X-XRML-Credential-Action": "1" });
  expect(keyInput()).toHaveProperty("value", ""); expect(remove()).toHaveProperty("disabled", true);
});

it.each([422, 403, 503])("preserves the input and never renders a server error echo on HTTP %s", async status => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") throw new api.ApiError("leaked-synthetic-key", status, "leaked-synthetic-key");
    return credential as never;
  });
  await openEditor(); fireEvent.change(keyInput(), { target: { value: "leaked-synthetic-key" } }); fireEvent.click(save());
  await screen.findByRole("alert");
  expect(keyInput()).toHaveProperty("value", "leaked-synthetic-key");
  expect(screen.queryByText(/leaked-synthetic-key/)).toBeNull();
  expect(save()).toHaveProperty("disabled", status !== 422);
  if (status === 403) expect(screen.getByText(/관리자 권한과 같은 서버의 로컬 \/app 화면/)).toBeTruthy();
});

it.each(["lost response", "revision conflict", "invalid receipt"])("requires refresh before a manual retry after %s and keeps the typed key", async failure => {
  let reads = 0, attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") {
      if (++attempts === 1) {
        if (failure === "revision conflict") throw new api.ApiError("conflict", 409, "{}");
        if (failure === "invalid receipt") return { ...stored, connector_id: "openai_api" } as never;
        throw new Error("network");
      }
      return { ...stored, revision: 3 } as never;
    }
    return (++reads === 1 ? credential : { ...stored, revision: 2 }) as never;
  });
  await openEditor(); fireEvent.change(keyInput(), { target: { value: "retry-key" } }); fireEvent.click(save());
  await screen.findByRole("alert"); expect(save()).toHaveProperty("disabled", true); expect(reads).toBe(1);
  fireEvent.click(save()); expect(attempts).toBe(1);
  fireEvent.click(refresh()); await screen.findByText(/API 키 저장 상태를 확인했습니다/);
  expect(keyInput()).toHaveProperty("value", "retry-key"); expect(writes()).toHaveLength(1);
  fireEvent.click(save()); await screen.findByText(/API 키를 로컬 암호화 저장소에 저장했습니다/);
  expect(JSON.parse(writes()[1][1]!.body as string)).toEqual({ expected_revision: 2, api_key: "retry-key" });
});

it("accepts a no-op successful save with the same revision", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(stored);
  await openEditor(); fireEvent.change(keyInput(), { target: { value: "same-key" } }); fireEvent.click(save());
  await screen.findByText(/API 키를 로컬 암호화 저장소에 저장했습니다/);
  expect(keyInput()).toHaveProperty("value", "");
});

it("locks repeated Enter/clicks and status refresh during a pending mutation", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => init?.method === "PUT"
    ? new Promise(resolve => { finish = resolve; }) as never : credential as never);
  await openEditor(); fireEvent.change(keyInput(), { target: { value: "pending-key" } });
  fireEvent.keyDown(keyInput(), { key: "Enter" }); fireEvent.click(save()); fireEvent.keyDown(keyInput(), { key: "Enter" });
  expect(writes()).toHaveLength(1); expect(refresh()).toHaveProperty("disabled", true); expect(keyInput()).toHaveProperty("disabled", true);
  await act(async () => finish(stored)); expect(keyInput()).toHaveProperty("value", "");
});

it("drops secret state on connector change and ignores the old mutation response", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => init?.method === "PUT"
    ? new Promise(resolve => { finish = resolve; }) as never
    : { ...credential, connector_id: path.includes("openai_api") ? "openai_api" : "anthropic_api" } as never);
  const view = await openEditor(); const oldInput = keyInput();
  fireEvent.change(oldInput, { target: { value: "old-key" } }); fireEvent.click(save());
  view.rerender(<ModelCredentialEditor connectorId="openai_api" displayName="OpenAI · API 키"/>);
  const nextInput = screen.getByLabelText("OpenAI · API 키 값");
  await waitFor(() => expect(nextInput).toHaveProperty("disabled", false));
  expect(oldInput).toHaveProperty("value", ""); expect(nextInput).toHaveProperty("value", "");
  fireEvent.change(nextInput, { target: { value: "new-key" } }); await act(async () => finish(stored));
  expect(nextInput).toHaveProperty("value", "new-key"); expect(screen.queryByText(/API 키를 로컬 암호화 저장소에 저장했습니다/)).toBeNull();
});

it("aborts superseded StrictMode reads, ignores stale status and clears detached input on unmount", async () => {
  const pending: Array<(value: unknown) => void> = [];
  vi.mocked(api.fetchJson).mockImplementation(async () => new Promise(resolve => pending.push(resolve)) as never);
  const view = render(<StrictMode><ModelCredentialEditor connectorId="anthropic_api" displayName="Claude · Anthropic API 키"/></StrictMode>);
  await waitFor(() => expect(pending).toHaveLength(2));
  await act(async () => pending[1](credential));
  fireEvent.change(keyInput(), { target: { value: "typed-key" } });
  await act(async () => pending[0](stored));
  expect(screen.getByText("적용 경로: 키 없음")).toBeTruthy();
  fireEvent.click(refresh()); const element = keyInput(); view.unmount();
  expect(vi.mocked(api.fetchJson).mock.calls.at(-1)![1]?.signal).toHaveProperty("aborted", true);
  expect(element).toHaveProperty("value", ""); await act(async () => pending[2](stored)); expect(writes()).toHaveLength(0);
});

it("shows unsupported storage without treating an available environment key as a saved key", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...credential, storage_available: false, environment_present: true, source: "environment" });
  render(<ModelCredentialEditor connectorId="anthropic_api" displayName="Claude · Anthropic API 키"/>);
  await screen.findByText(/이 환경에서는 로컬 암호화 저장소를 사용할 수 없습니다/);
  expect(screen.getByText("적용 경로: 서버 환경 변수")).toBeTruthy();
  expect(keyInput()).toHaveProperty("disabled", true); expect(save()).toHaveProperty("disabled", true); expect(remove()).toHaveProperty("disabled", true);
});

it("explains administrator and same-origin app requirements when the initial status read is forbidden", async () => {
  vi.mocked(api.fetchJson).mockRejectedValue(new api.ApiError("sensitive-server-detail", 403, "sensitive-server-detail"));
  render(<ModelCredentialEditor connectorId="anthropic_api" displayName="Claude · Anthropic API 키"/>);
  expect((await screen.findByRole("alert")).textContent).toContain("관리자 권한과 같은 서버의 로컬 /app 화면");
  expect(screen.queryByText(/sensitive-server-detail/)).toBeNull();
  expect(save()).toHaveProperty("disabled", true); expect(writes()).toHaveLength(0);
});

it.each([
  { ...credential, connector_id: "openai_api" }, { ...credential, revision: -1 }, { ...credential, revision: 0.5 },
  { ...credential, stored: true }, { ...credential, environment_present: true }, { ...credential, execution_allowed: true },
  { ...credential, storage_available: "true" },
])("fails closed on a mismatched or malformed status %#", async response => {
  vi.mocked(api.fetchJson).mockResolvedValue(response);
  render(<ModelCredentialEditor connectorId="anthropic_api" displayName="Claude · Anthropic API 키"/>);
  await screen.findByRole("alert"); expect(save()).toHaveProperty("disabled", true); expect(writes()).toHaveLength(0);
});

it.each(["한글-key", "key\u0001value", "x".repeat(4097), "key value"])("does not submit an invalid key %#", async value => {
  await openEditor(); fireEvent.change(keyInput(), { target: { value } }); fireEvent.click(save());
  await screen.findByRole("alert"); expect(writes()).toHaveLength(0);
});
