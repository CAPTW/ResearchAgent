import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalWorkingCopy, type WorkingContent } from "../views/JournalWorkingCopy";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const content: WorkingContent = { base_version_id: null, title: "", entries: [{ kind: "question", text: "Unfinished", citation_ids: [] }] };
const saved = { head_id: "copy1", copy: { copy_id: "copy1", created_at: "2026-09-28T00:00:00Z", content } };
const props = { url: "/api/topics/topic/local-synthesis/job/journal-working-copy", inputDigest: "frozen", content, disabled: false, onRestore: vi.fn(), onBusy: vi.fn() };
beforeEach(() => vi.resetAllMocks());
async function open() {
  const view = render(<JournalWorkingCopy {...props}/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("작성 중 입력 보관·복구"));
  await screen.findByText(/마지막 확인한 보관 시각|불러올 보관본이 없습니다/);
  return view;
}

it("loads only this user's checkpoint and requires an explicit restore without automatic writes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(saved);
  await open();
  expect(props.onRestore).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "현재 입력을 보관본으로 교체" }));
  expect(props.onRestore).toHaveBeenCalledExactlyOnceWith(content);
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith(props.url);
  expect(screen.getByText(/자동 저장이 아니며/)).toBeTruthy();
});

it("saves partial input once, then retries the exact body despite subsequent editing", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ head_id: null, copy: null }).mockRejectedValueOnce(new Error("lost response"))
    .mockResolvedValueOnce({ working_copy: saved, created: false });
  const view = await open();
  fireEvent.click(screen.getByRole("button", { name: "현재 작성 중 입력 보관" }));
  await screen.findByText(/lost response/);
  view.rerender(<JournalWorkingCopy {...props} content={{ ...content, title: "Edited after request" }}/>);
  fireEvent.click(screen.getByRole("button", { name: "같은 보관 요청 다시 보내기" }));
  await screen.findByText(/요청 당시 입력을 보관했습니다/);
  const writes = vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(2); expect(writes[0][1]?.body).toBe(writes[1][1]?.body);
  expect(JSON.parse(writes[0][1]!.body as string)).toMatchObject({ expected_copy_id: null, expected_input_digest: "frozen", content });
  expect(props.onRestore).not.toHaveBeenCalled();
});

it("requires a fresh head after a conflict and does not replace current editor input", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockRejectedValueOnce(new api.ApiError("conflict", 409, ""))
    .mockResolvedValueOnce({ ...saved, head_id: "copy2" }).mockResolvedValueOnce({ working_copy: { ...saved, head_id: "copy3" }, created: true });
  await open();
  fireEvent.click(screen.getByRole("button", { name: "현재 작성 중 입력 보관" }));
  await screen.findByText(/보관 요청이 거절되었습니다/);
  expect(screen.getByRole("button", { name: "현재 작성 중 입력 보관" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "보관 상태 새로고침" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "현재 작성 중 입력 보관" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "현재 작성 중 입력 보관" }));
  await screen.findByText(/요청 당시 입력을 보관했습니다/);
  const writes = vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
  expect(JSON.parse(writes[1][1]!.body as string).expected_copy_id).toBe("copy2");
  expect(props.onRestore).not.toHaveBeenCalled();
});

it("clears with the observed head, blocks double clicks and leaves the editor unchanged", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(saved).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  await open();
  const clear = screen.getByRole("button", { name: "불러올 보관본 해제" }); fireEvent.click(clear); fireEvent.click(clear);
  expect(api.fetchJson).toHaveBeenCalledTimes(2); expect(props.onBusy).toHaveBeenLastCalledWith(true);
  expect(JSON.parse(vi.mocked(api.fetchJson).mock.calls[1][1]!.body as string)).toMatchObject({ expected_copy_id: "copy1", content: null });
  await act(async () => finish({ working_copy: { head_id: "clear1", copy: null }, created: true }));
  await screen.findByText(/불러올 보관본을 해제했습니다/);
  expect(props.onRestore).not.toHaveBeenCalled(); expect(props.onBusy).toHaveBeenLastCalledWith(false);
});

it("does not turn a read failure into empty success or expose an old job's late response", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockRejectedValueOnce(new Error("offline"));
  const view = render(<JournalWorkingCopy {...props}/>);
  fireEvent.click(screen.getByText("작성 중 입력 보관·복구"));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledOnce());
  view.rerender(<JournalWorkingCopy {...props} url="/other-job/journal-working-copy"/>);
  await screen.findByText(/보관 조회 오류: Error: offline/);
  await act(async () => finish(saved));
  expect(screen.queryByText(/마지막 확인한 보관 시각/)).toBeNull();
  expect(screen.queryByText(/불러올 보관본이 없습니다/)).toBeNull();
  expect(screen.getByRole("button", { name: "현재 입력을 보관본으로 교체" })).toHaveProperty("disabled", true);
});
