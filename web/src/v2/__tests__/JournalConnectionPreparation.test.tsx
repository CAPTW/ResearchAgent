import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalExecutionPreparation } from "../views/JournalExecutionPreparation";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/local-synthesis/job", input = "a".repeat(64);
const connection = { connector_id: "codex_subscription", provider_name: "codex_cli", model: "fixture-codex", display_name: "Codex", auth_mode: "subscription_cli", selection_digest: "b".repeat(64), environment_override: false, ready: true, reason: null, execution_allowed: false };
const result = { execution: { contract: { execution_id: "prepared" } }, created: true };
beforeEach(() => vi.resetAllMocks());
function open(onPrepared = vi.fn()) {
  const view = render(<JournalExecutionPreparation root={root} inputDigest={input} onPrepared={onPrepared}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  return { ...view, onPrepared };
}
function submit() { fireEvent.submit(screen.getByRole("form", { name: "생성 요청 준비" })); }
function posts() { return vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST"); }
function mockConnection(value: unknown = connection) {
  vi.mocked(api.fetchJson).mockImplementation(async (_url, init) => init?.method === "POST" ? result : value as never);
}

it("loads the saved Codex choice only on opening and prepares its exact selection without executing", async () => {
  mockConnection();
  const onPrepared = vi.fn();
  render(<JournalExecutionPreparation root={root} inputDigest={input} onPrepared={onPrepared}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  await screen.findByText("fixture-codex");
  expect((screen.getByRole("radio", { name: "저장된 모델 연결 사용" }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByText(/실행 환경과 실행 승인은 아직 연결되지 않았습니다/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("최대 시도 횟수"), { target: { value: "2" } });
  submit();
  await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${root}/journal-executions/selected-connection`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_input_digest: input, connector_id: "codex_subscription", expected_selection_digest: connection.selection_digest, provider_name: "codex_cli", model: "fixture-codex", max_attempts: 2, max_output_tokens: 4000 });
  expect(onPrepared).toHaveBeenCalledExactlyOnceWith("prepared");
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
});

it.each([
  ["model_connection_model_required", "모델 이름을 설정에서 저장"],
  ["model_connection_unavailable", "저장된 연결을 사용할 수 없습니다"],
  ["model_connection_adapter_unavailable", "Journal 준비 어댑터가 연결되지 않았습니다"],
])("keeps limits and blocks an unavailable selected target: %s", async (reason, label) => {
  mockConnection({ ...connection, model: "", ready: false, reason });
  open();
  await screen.findByText(new RegExp(label));
  fireEvent.change(screen.getByLabelText("시도당 출력 토큰 상한"), { target: { value: "2048" } });
  expect((screen.getByRole("button", { name: "로컬 생성 요청 준비" }) as HTMLButtonElement).disabled).toBe(true);
  submit(); expect(posts()).toHaveLength(0);
  expect((screen.getByLabelText("시도당 출력 토큰 상한") as HTMLInputElement).value).toBe("2048");
});

it("displays an environment override as unverified requested identity", async () => {
  mockConnection({ ...connection, environment_override: true, connector_id: null, provider_name: null, ready: false, reason: "model_connection_unavailable", display_name: "알 수 없는 연결" });
  open();
  await screen.findByText(/서버 환경 설정이 저장된 선택보다 우선/);
  expect(screen.getByText(/로그인·모델 사용 가능 여부는 확인하지 않았습니다/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "로컬 생성 요청 준비" }) as HTMLButtonElement).disabled).toBe(true);
});

it("refreshes a failed summary explicitly without losing limits", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(connection);
  open();
  await screen.findByText(/저장된 모델 연결을 확인하지 못했습니다/);
  fireEvent.change(screen.getByLabelText("최대 시도 횟수"), { target: { value: "3" } });
  fireEvent.change(screen.getByLabelText("시도당 출력 토큰 상한"), { target: { value: "4096" } });
  fireEvent.click(screen.getByRole("button", { name: "연결 다시 확인" }));
  await screen.findByText("fixture-codex");
  expect((screen.getByLabelText("최대 시도 횟수") as HTMLInputElement).value).toBe("3");
  expect((screen.getByLabelText("시도당 출력 토큰 상한") as HTMLInputElement).value).toBe("4096");
});

it("requires explicit refresh after a selection conflict and keeps limits without resubmitting", async () => {
  const updated = { ...connection, model: "new-model", selection_digest: "c".repeat(64) };
  vi.mocked(api.fetchJson).mockResolvedValueOnce(connection).mockRejectedValueOnce(new api.ApiError("model_connection_selection_changed", 409, "")).mockResolvedValueOnce(updated).mockResolvedValue(result);
  open(); await screen.findByText("fixture-codex");
  fireEvent.change(screen.getByLabelText("최대 시도 횟수"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("시도당 출력 토큰 상한"), { target: { value: "3000" } });
  submit(); await screen.findByText(/저장된 모델 연결이 변경되었습니다/);
  expect((screen.getByRole("button", { name: "로컬 생성 요청 준비" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("fixture-codex")).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  submit(); expect(posts()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "연결 다시 확인" }));
  await screen.findByText("new-model");
  expect(posts()).toHaveLength(1);
  expect((screen.getByLabelText("최대 시도 횟수") as HTMLInputElement).value).toBe("2");
  expect((screen.getByLabelText("시도당 출력 토큰 상한") as HTMLInputElement).value).toBe("3000");
  submit(); await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  const bodies = posts().map(([, init]) => JSON.parse(init!.body as string));
  expect(bodies[1]).toMatchObject({ model: "new-model", expected_selection_digest: updated.selection_digest, max_attempts: 2, max_output_tokens: 3000 });
  expect(bodies[1].request_id).not.toBe(bodies[0].request_id);
});

it("retries an uncertain selected submission with exact endpoint and body, without reloading or retargeting", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(connection).mockRejectedValueOnce(new Error("response lost")).mockResolvedValue({ ...result, created: false });
  open(); await screen.findByText("fixture-codex");
  submit(); await screen.findByText(/저장 응답을 확인하지 못했습니다/);
  expect((screen.getByRole("group", { name: "요청 내용" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "연결 다시 확인" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByRole("button", { name: "같은 준비 요청 다시 보내기" }));
  await screen.findByText(/이미 저장된 요청을 찾았습니다/);
  expect(posts()[0]).toEqual(posts()[1]);
  expect(api.fetchJson).toHaveBeenCalledTimes(3);
});

it("preserves edited limits across manual and saved modes without changing the selected snapshot", async () => {
  mockConnection(); open(); await screen.findByText("fixture-codex");
  fireEvent.change(screen.getByLabelText("최대 시도 횟수"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("radio", { name: "도구·모델 직접 입력" }));
  fireEvent.change(screen.getByLabelText("요청 모델 이름"), { target: { value: "manual-model" } });
  fireEvent.click(screen.getByRole("radio", { name: "저장된 모델 연결 사용" }));
  expect(screen.getByText("fixture-codex")).toBeTruthy();
  expect((screen.getByLabelText("최대 시도 횟수") as HTMLInputElement).value).toBe("2");
  fireEvent.click(screen.getByRole("radio", { name: "도구·모델 직접 입력" }));
  expect((screen.getByLabelText("요청 모델 이름") as HTMLInputElement).value).toBe("manual-model");
  submit(); await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  expect(posts()[0][0]).toBe(`${root}/journal-executions`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ model: "manual-model", provider_name: "openai", max_attempts: 2 });
});

it("discards a late summary when the topic changes and resets target and limits", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue({ ...connection, model: "other-model" });
  const { rerender, onPrepared } = open();
  fireEvent.change(screen.getByLabelText("최대 시도 횟수"), { target: { value: "3" } });
  rerender(<JournalExecutionPreparation root={`${root}-other`} inputDigest={input} onPrepared={onPrepared}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  await screen.findByText("other-model");
  await act(async () => finish(connection));
  expect(screen.queryByText("fixture-codex")).toBeNull();
  expect((screen.getByLabelText("최대 시도 횟수") as HTMLInputElement).value).toBe("1");
  expect(onPrepared).not.toHaveBeenCalled();
});

it("prevents duplicate selected submission and ignores a late saved response after unmount", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(connection).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { unmount, onPrepared } = open(); await screen.findByText("fixture-codex");
  submit(); submit(); expect(posts()).toHaveLength(1);
  unmount(); await act(async () => finish(result));
  expect(onPrepared).not.toHaveBeenCalled();
});

it("allows a fixed offline target while the saved connection is unavailable", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("unavailable")).mockResolvedValue(result);
  render(<JournalExecutionPreparation root={root} inputDigest={input} onPrepared={vi.fn()} offline={{ enabled: true, mode: "OFFLINE_SYNTHETIC", provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1", reason: null }}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  await screen.findByText(/저장된 모델 연결을 확인하지 못했습니다/);
  fireEvent.click(screen.getByRole("checkbox", { name: "오프라인 합성 요청 준비" }));
  expect((screen.getByLabelText("요청 모델 이름") as HTMLInputElement).value).toBe("synthetic-journal-v1");
  submit(); await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  expect(posts()[0][0]).toBe(`${root}/journal-executions`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1" });
});
