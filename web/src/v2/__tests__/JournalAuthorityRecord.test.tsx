import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalAuthorityRecord } from "../views/JournalAuthorityRecord";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const source = { record: { registered_by: "record-owner", registered_at: "2026-09-27T00:00:00Z", decision_ref: "reviewed-exact-request" }, revocation: null };
beforeEach(() => vi.resetAllMocks());

it("loads only on request and distinguishes stored source from real permission or model proof", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(source);
  render(<JournalAuthorityRecord url="/source"/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  await screen.findByText(/기록 등록자: record-owner/);
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/source");
  expect(screen.getByText(/현재 호출 가능 여부를 보장하지 않습니다/)).toBeTruthy();
  expect(screen.getByText(/별도의 승인 진위·실제 모델 실행 증명은 아닙니다/)).toBeTruthy();
});

it("refreshes revocation, renders reason as inert text and exposes errors without stale status", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(source).mockResolvedValueOnce({ ...source,
    revocation: { actor_id: "owner", at: source.record.registered_at, reason: "<img src=https://external.test/x>\nWithdrawn." } }).mockRejectedValueOnce(new Error("source unavailable"));
  const { container } = render(<JournalAuthorityRecord url="/source"/>);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  await screen.findByText(/조회한 기록에 철회는 없습니다/);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 새로고침" }));
  await screen.findByText(/철회 기록: owner/);
  expect(screen.getByText(/<img src=/)).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 새로고침" }));
  await screen.findByRole("alert");
  expect(screen.queryByText(/철회 기록:/)).toBeNull();
  expect(screen.getByText(/현재 철회 여부를 확인하지 못했습니다/)).toBeTruthy();
});

it("ignores a late response after the selected authority changes", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...source, record: { ...source.record, registered_by: "new-owner" } });
  const view = render(<JournalAuthorityRecord key="old" url="/old"/>);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  view.rerender(<JournalAuthorityRecord key="new" url="/new"/>);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  await screen.findByText(/기록 등록자: new-owner/);
  await act(async () => resolve(source));
  expect(screen.queryByText(/기록 등록자: record-owner/)).toBeNull();
});

it("keeps the exact withdrawal request across lost responses and failed refreshes", async () => {
  const submit = vi.fn().mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce({ applied: false,
    revocation: { actor_id: "server-user", at: source.record.registered_at, reason: "保留\nKeep my reason" } });
  vi.mocked(api.fetchJson).mockResolvedValueOnce(source).mockRejectedValueOnce(new Error("read unavailable")).mockResolvedValueOnce(source);
  render(<JournalAuthorityRecord url="/source" withdrawal={{ authorization_id: "exact-id", record_digest: "a".repeat(64), submit }}/>);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  const input = await screen.findByRole("textbox", { name: "실행 허용 철회 사유" });
  expect((screen.getByRole("button", { name: "이 실행 허용 철회" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(input, { target: { value: "保留\nKeep my reason" } });
  fireEvent.click(screen.getByRole("button", { name: "이 실행 허용 철회" }));
  await screen.findByText(/response lost/);
  expect((input as HTMLTextAreaElement).disabled).toBe(true);
  const body = submit.mock.calls[0][0];
  expect(body).toEqual({ authorization_id: "exact-id", expected_record_digest: "a".repeat(64), request_id: expect.any(String), reason: "保留\nKeep my reason" });
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 새로고침" }));
  await screen.findByText(/read unavailable/);
  expect(screen.queryByRole("button", { name: "같은 철회 요청 다시 보내기" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 새로고침" }));
  await screen.findByRole("button", { name: "같은 철회 요청 다시 보내기" });
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(body.reason);
  fireEvent.click(screen.getByRole("button", { name: "같은 철회 요청 다시 보내기" }));
  await screen.findByText("이미 처리한 철회 요청입니다.");
  expect(submit.mock.calls[1][0]).toEqual(body);
  expect(screen.getByText(/철회 기록: server-user/)).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("shows the other writer's revocation after a conflict without replacing it", async () => {
  const submit = vi.fn().mockRejectedValue(new Error("journal_authority_conflict"));
  vi.mocked(api.fetchJson).mockResolvedValueOnce(source).mockResolvedValueOnce({ ...source,
    revocation: { actor_id: "other-owner", at: source.record.registered_at, reason: "Different withdrawal" } });
  render(<JournalAuthorityRecord url="/source" withdrawal={{ authorization_id: "exact-id", record_digest: "a".repeat(64), submit }}/>);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  fireEvent.change(await screen.findByRole("textbox"), { target: { value: "My reason" } });
  fireEvent.click(screen.getByRole("button", { name: "이 실행 허용 철회" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 새로고침" }));
  await screen.findByText(/철회 기록: other-owner/);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(submit).toHaveBeenCalledOnce();
});

it("does not show an old source as unrevoked after another attempt's panel withdrew it", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(r => { finish = r; }));
  const view = render(<JournalAuthorityRecord url="/source"/>);
  fireEvent.click(screen.getByRole("button", { name: "허용 출처 확인" }));
  view.rerender(<JournalAuthorityRecord url="/source" knownRevocation={{ actor_id: "owner", at: source.record.registered_at, reason: "Withdrawn elsewhere" }}/>);
  await act(async () => finish(source));
  expect(screen.getByText(/철회 기록: owner/)).toBeTruthy();
  expect(screen.queryByText(/조회한 기록에 철회는 없습니다/)).toBeNull();
});
