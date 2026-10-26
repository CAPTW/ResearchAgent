import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewExecution } from "../components/ReviewExecution";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topics/topic/paper-bank/version/review-preparations", id = "prepared/one";
const base = `${root}/prepared%2Fone/executions`;
const digest = "a".repeat(64);
function detail(state = "READY", sequence = 1, unknown = false, reason: string | null = null) {
  const event = { sequence, state, at: "2026-10-01T08:00:00Z", deadline: ["LEASED", "RUNNING"].includes(state) ? "2026-10-01T08:02:00Z" : null, outcome_unknown: unknown, reason, receipt_id: state === "RESULT_SAVED" ? "receipt-one" : null };
  return { contract: { execution_id: "execution-one", preparation_id: id, actor_id: "owner", created_at: event.at, mode: "OFFLINE_SYNTHETIC", specification: { request_id: "original", expected_preparation_digest: digest, acknowledge_unknown: false }, prepared: { provider_name: "xrml_offline_review", model: "synthetic-prepared-review-v2", provider_request: { prompt: "frozen" } } }, events: [event], current: event };
}
const listing = (items: ReturnType<typeof detail>[] = [], enabled = true) => ({ items, enabled, reason: enabled ? null : "review_execution_disabled", mode: "OFFLINE_SYNTHETIC", preparation_digest: digest, outcome_unknown_pending: items.some(item => item.current.outcome_unknown), page: 1, pageSize: 10, hasMore: false });
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockResolvedValue(listing() as never); });
async function open(props: { root?: string; onOpenReceipt?: (id: string) => void } = {}) {
  const view = render(<ReviewExecution root={props.root ?? root} id={id} onOpenReceipt={props.onOpenReceipt}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("검토 실행 이력"));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledWith(`${base}?page=1&pageSize=10`));
  return view;
}

it("loads only when opened and separates prepare, approval, and dispatch", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method !== "POST") return listing() as never;
    const body = JSON.parse(init.body as string);
    return detail(path.endsWith("/dispatch") ? "LEASED" : body.action === "approve" ? "APPROVED" : "READY", path.endsWith("/dispatch") ? 3 : body.action === "approve" ? 2 : 1) as never;
  });
  await open();
  expect(await screen.findByText("저장된 검토 실행 요청이 없습니다.")).toBeTruthy();
  expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "새 합성 검토 실행 요청 준비" }));
  await screen.findByRole("button", { name: "이 요청의 오프라인 실행 승인" });
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_preparation_digest: digest, acknowledge_unknown: false });
  expect(posts()).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "승인된 합성 검토 실행 시작" })).toBeNull();
  const approve = screen.getByRole("button", { name: "이 요청의 오프라인 실행 승인" });
  approve.focus(); await userEvent.setup().keyboard("{Enter}");
  await screen.findByRole("button", { name: "승인된 합성 검토 실행 시작" });
  expect(posts()[1][0]).toBe(`${base}/execution-one/controls`);
  expect(JSON.parse(posts()[1][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 1, action: "approve" });
  expect(posts()).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "승인된 합성 검토 실행 시작" }));
  await screen.findByText(/마지막 저장 상태: 실행 담당 할당/);
  expect(posts()[2][0]).toBe(`${base}/execution-one/dispatch`);
  expect(JSON.parse(posts()[2][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 2 });
  expect(screen.getByText(/현재 작업자가 살아 있다는 증거는 아닙니다/)).toBeTruthy();
});

it.each([undefined, 408, 503])("retries the exact pending approval after ambiguous status %s and blocks competing actions", async status => {
  let attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method !== "POST") return listing([detail()]) as never;
    if (++attempts === 1) throw status === undefined ? new Error("response lost") : new api.ApiError("response lost", status, "{}");
    return detail("APPROVED", 2) as never;
  });
  await open(); fireEvent.click(await screen.findByRole("button", { name: "이 요청의 오프라인 실행 승인" }));
  await screen.findByText(/response lost/);
  expect(screen.getByRole("button", { name: "검토 실행 이력 새로고침" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "이 실행 요청 취소" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 실행 요청 다시 보내기" }));
  await screen.findByRole("button", { name: "승인된 합성 검토 실행 시작" });
  expect(posts()).toHaveLength(2); expect(posts()[1]).toEqual(posts()[0]);
});

it("reloads current history after a definitive conflict without retaining the rejected command", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => {
    if (init?.method === "POST") throw new api.ApiError("sequence_conflict", 409, "{}");
    return listing([detail(++reads === 1 ? "READY" : "REVOKED", reads)]) as never;
  });
  await open(); fireEvent.click(await screen.findByRole("button", { name: "이 요청의 오프라인 실행 승인" }));
  await screen.findByText(/마지막 저장 상태: 철회됨/);
  expect(screen.queryByRole("button", { name: "같은 실행 요청 다시 보내기" })).toBeNull();
  expect(posts()).toHaveLength(1);
});

it("saves a received result through its own control without executing the adapter again", async () => {
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? detail("RESULT_SAVED", 6) : listing([detail("RESULT_RECEIVED", 5)])) as never);
  await open({ onOpenReceipt });
  expect(screen.queryByRole("button", { name: "승인된 합성 검토 실행 시작" })).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "수신 결과 저장 다시 시도" }));
  fireEvent.click(await screen.findByRole("button", { name: "저장된 검토 입력·결과 열기" }));
  expect(onOpenReceipt).toHaveBeenCalledWith("receipt-one");
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${base}/execution-one/controls`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ action: "save_result", expected_sequence: 5 });
});

it("requires an explicit acknowledgment for a new request after an unknown outcome", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? detail() : listing([detail("INTERRUPTED", 4, true)])) as never);
  await open();
  const prepare = await screen.findByRole("button", { name: "새 합성 검토 실행 요청 준비" });
  expect(prepare).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("checkbox", { name: /이전 결과가 불명확함/ }));
  fireEvent.click(prepare);
  await screen.findByRole("button", { name: "이 요청의 오프라인 실행 승인" });
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ acknowledge_unknown: true });
  expect(posts()).toHaveLength(1);
});

it("requires unknown acknowledgment even when the older unknown attempt is on another page", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing([detail("RESULT_SAVED", 6)]), outcome_unknown_pending: true } as never);
  await open();
  expect(await screen.findByRole("checkbox", { name: /이전 결과가 불명확함/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: "새 합성 검토 실행 요청 준비" })).toHaveProperty("disabled", true);
  expect(posts()).toHaveLength(0);
});

it("keeps a newly unknown cancellation visible when another completed record is selected", async () => {
  const older = detail("RESULT_SAVED", 6); older.contract.execution_id = "older-execution";
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") return detail("CANCELLED", 4, true) as never;
    if (path.endsWith("/older-execution")) return older as never;
    return listing([detail("RUNNING", 3), older]) as never;
  });
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "이 실행 요청 취소" }));
  await screen.findByText(/마지막 저장 상태: 취소됨/);
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 기록 열기 · older-execution" }));
  await screen.findByText(/마지막 저장 상태: 결과 저장됨/);
  expect(screen.getByRole("checkbox", { name: /이전 결과가 불명확함/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: "새 합성 검토 실행 요청 준비" })).toHaveProperty("disabled", true);
});

it("does not recover or retry an expired running attempt merely by reading it", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(listing([detail("RUNNING", 3)]) as never);
  await open();
  await screen.findByRole("button", { name: "만료 상태 확인·복구" });
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(2));
  expect(posts()).toHaveLength(0);
});

it("keeps history readable with default-deny execution and does not offer mutation", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(listing([detail("RESULT_SAVED", 6)], false) as never);
  await open();
  await screen.findByText(/이 환경에서는 새 실행·승인·시작이 비활성화/);
  expect(screen.getByText(/마지막 저장 상태: 결과 저장됨/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "새 합성 검토 실행 요청 준비" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it.each(["save_result", "cancel"])("retains local %s control while new execution is disabled", async action => {
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? detail(action === "save_result" ? "RESULT_SAVED" : "CANCELLED", 6) : listing([detail("RESULT_RECEIVED", 5)], false)) as never);
  await open();
  fireEvent.click(await screen.findByRole("button", { name: action === "save_result" ? "수신 결과 저장 다시 시도" : "이 실행 요청 취소" }));
  await screen.findByText(action === "save_result" ? /마지막 저장 상태: 결과 저장됨/ : /마지막 저장 상태: 취소됨/);
  expect(posts()).toHaveLength(1);
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ expected_sequence: 5, action });
  expect(screen.queryByRole("button", { name: "새 합성 검토 실행 요청 준비" })).toBeNull();
});

it("recovers a deadline only through an explicit local control, without dispatching", async () => {
  const expired = detail("APPROVED", 2); expired.current.deadline = "2000-01-01T00:00:00Z";
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? detail("INTERRUPTED", 3, false, "deadline_expired") : listing([expired], false)) as never);
  await open();
  expect(posts()).toHaveLength(0);
  fireEvent.click(await screen.findByRole("button", { name: "만료 상태 확인·복구" }));
  await screen.findByText(/마지막 저장 상태: 중단됨/);
  expect(screen.getByText("기록 사유: 유효 시간이 지나 종료됨 (deadline_expired)")).toBeTruthy();
  expect(posts()[0][0]).toBe(`${base}/execution-one/controls`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ expected_sequence: 2, action: "recover" });
  expect(posts()).toHaveLength(1);
});

it("distinguishes read failure from empty history and allows manual read retry", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("read unavailable")).mockResolvedValue(listing() as never);
  await open();
  await screen.findByRole("alert");
  expect(screen.queryByText("저장된 검토 실행 요청이 없습니다.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "검토 실행 이력 새로고침" }));
  await screen.findByText("저장된 검토 실행 요청이 없습니다.");
  expect(posts()).toHaveLength(0);
});

it("drops pending and late responses when topic changes", async () => {
  let resolve!: (value: never) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => init?.method === "POST" ? new Promise<never>(value => { resolve = value; }) : listing() as never);
  const view = await open(); fireEvent.click(await screen.findByRole("button", { name: "새 합성 검토 실행 요청 준비" }));
  await waitFor(() => expect(posts()).toHaveLength(1));
  view.rerender(<ReviewExecution root="/other-topic/preparations" id={id}/>);
  await act(async () => resolve(detail("RESULT_SAVED", 6) as never));
  expect(screen.queryByText(/receipt-one/)).toBeNull();
  expect(screen.queryByRole("button", { name: "같은 실행 요청 다시 보내기" })).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByText("검토 실행 이력"));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledWith("/other-topic/preparations/prepared%2Fone/executions?page=1&pageSize=10"));
  expect(posts()).toHaveLength(1);
});
