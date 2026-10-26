import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalExecutionPreparation } from "../views/JournalExecutionPreparation";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/local-synthesis/job", input = "a".repeat(64);
const result = { execution: { contract: { execution_id: "prepared" } }, created: true };
const mutations = vi.fn();
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchJson).mockImplementation((url, options) => options?.method === "POST" ? mutations(url, options) : new Promise(() => {}));
});
function open(onPrepared = vi.fn()) {
  const view = render(<JournalExecutionPreparation root={root} inputDigest={input} onPrepared={onPrepared}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByRole("radio", { name: "도구·모델 직접 입력" }));
  fireEvent.change(screen.getByLabelText("요청 모델 이름"), { target: { value: "offline-model" } });
  return { ...view, onPrepared };
}
function submit() { fireEvent.submit(screen.getByRole("form", { name: "생성 요청 준비" })); }

it("prepares an explicit manual input-bound record without dispatching", async () => {
  mutations.mockResolvedValue(result);
  const { onPrepared } = open();
  expect(mutations).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("최대 시도 횟수"), { target: { value: "2" } });
  submit();
  await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  const [url, options] = mutations.mock.calls[0];
  expect(url).toBe(`${root}/journal-executions`);
  expect(options?.method).toBe("POST");
  expect(JSON.parse(options!.body as string)).toEqual({ request_id: expect.any(String), expected_input_digest: input, provider_name: "openai", model: "offline-model", max_attempts: 2, max_output_tokens: 4000 });
  expect(onPrepared).toHaveBeenCalledExactlyOnceWith("prepared");
  expect(mutations).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "로컬 생성 요청 준비" })).toBeNull();
  expect(screen.queryByRole("button", { name: /생성 실행|재시도 실행/ })).toBeNull();
});

it("locks uncertain inputs and retries the same identity after a lost response", async () => {
  mutations.mockRejectedValueOnce(new Error("response lost")).mockResolvedValue({ ...result, created: false });
  open(); submit();
  await screen.findByText(/저장 응답을 확인하지 못했습니다/);
  expect((screen.getByRole("group", { name: "요청 내용" }) as HTMLFieldSetElement).disabled).toBe(true);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByRole("button", { name: "같은 준비 요청 다시 보내기" }));
  await screen.findByText(/이미 저장된 요청을 찾았습니다/);
  const calls = mutations.mock.calls;
  expect(calls[0]).toEqual(calls[1]);
});

it("allows editing after a definite rejection without losing text", async () => {
  mutations.mockRejectedValue(new api.ApiError("journal_execution_input_changed", 409, ""));
  open(); submit();
  await screen.findByText(/준비 요청이 거절되었습니다/);
  expect((screen.getByRole("group", { name: "요청 내용" }) as HTMLFieldSetElement).disabled).toBe(false);
  expect((screen.getByLabelText("요청 모델 이름") as HTMLInputElement).value).toBe("offline-model");
});

it("requires a deliberate new request after success and allocates a new identity", async () => {
  mutations.mockResolvedValue(result);
  open(); submit(); await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  fireEvent.click(screen.getByRole("button", { name: "다른 요청 준비" }));
  fireEvent.change(screen.getByLabelText("요청 모델 이름"), { target: { value: "second-model" } });
  submit(); await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  const bodies = mutations.mock.calls.map(([, o]) => JSON.parse(o!.body as string));
  expect(bodies[0].request_id).not.toBe(bodies[1].request_id);
  expect(bodies[1].model).toBe("second-model");
});

it("prevents concurrent submission and discards a late reply after unmount", async () => {
  let resolve!:(value: unknown) => void;
  mutations.mockImplementation(() => new Promise(r => { resolve = r; }));
  const { unmount, onPrepared } = open(); submit(); submit();
  expect(mutations).toHaveBeenCalledTimes(1);
  unmount(); await act(async () => resolve(result));
  expect(onPrepared).not.toHaveBeenCalled();
});

it("offers a fixed offline synthetic request only when the server enables it, without approval or dispatch", async () => {
  mutations.mockResolvedValue(result);
  render(<JournalExecutionPreparation root={root} inputDigest={input} onPrepared={vi.fn()}
    offline={{ enabled: true, mode: "OFFLINE_SYNTHETIC", provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1", reason: null }}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByRole("checkbox", { name: "오프라인 합성 요청 준비" }));
  expect((screen.getByLabelText("요청 도구 식별자") as HTMLInputElement).readOnly).toBe(true);
  expect((screen.getByLabelText("요청 모델 이름") as HTMLInputElement).value).toBe("synthetic-journal-v1");
  submit();
  await screen.findByText(/생성 요청을 로컬에 준비했습니다/);
  const calls = mutations.mock.calls;
  expect(calls).toHaveLength(1);
  expect(calls[0][0]).toBe(`${root}/journal-executions`);
  expect(JSON.parse(calls[0][1]!.body as string)).toMatchObject({ provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1" });
});

it("drops pending preparation and late callbacks when the job changes without a caller key", async () => {
  let finish!: (value: unknown) => void;
  mutations.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { rerender, onPrepared } = open(); submit();
  rerender(<JournalExecutionPreparation root={`${root}-other`} inputDigest={input} onPrepared={onPrepared}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  expect((screen.getByRole("radio", { name: "저장된 모델 연결 사용" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole("radio", { name: "도구·모델 직접 입력" }));
  expect((screen.getByLabelText("요청 모델 이름") as HTMLInputElement).value).toBe("");
  await act(async () => finish(result));
  expect(onPrepared).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "같은 준비 요청 다시 보내기" })).toBeNull();
});

it("does not silently turn a selected synthetic request into a live target during a capability refresh", async () => {
  const props = { root, inputDigest: input, onPrepared: vi.fn() };
  const { rerender } = render(<JournalExecutionPreparation {...props} offline={{ enabled: true, mode: "OFFLINE_SYNTHETIC", provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1", reason: null }}/>);
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  fireEvent.click(screen.getByRole("checkbox", { name: "오프라인 합성 요청 준비" }));
  rerender(<JournalExecutionPreparation {...props}/>);
  expect((screen.getByLabelText("요청 모델 이름") as HTMLInputElement).value).toBe("synthetic-journal-v1");
  expect((screen.getByRole("button", { name: "로컬 생성 요청 준비" }) as HTMLButtonElement).disabled).toBe(true);
  submit();
  expect(mutations).not.toHaveBeenCalled();
});
