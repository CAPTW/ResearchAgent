import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import type { ExecutionDetail } from "../../lib/journalExecutions";
import { JournalOfflineExecution } from "../views/JournalOfflineExecution";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const url = "/api/topics/topic/local-synthesis/job/journal-executions/offline";
const capability = { enabled: true, mode: "OFFLINE_SYNTHETIC", provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1", reason: null };
const event = { sequence: 0, state: "READY" as const, attempt: 0, at: "2026-09-28T00:00:00Z", deadline: null, outcome_unknown: false, reason: null };
const execution: ExecutionDetail = { contract: { execution_id: "offline", actor_id: "owner", created_at: event.at, packet: { packet_id: "packet" }, specification: { provider_name: capability.provider_name, model: capability.model, max_attempts: 3, max_output_tokens: 4000, expected_input_digest: "frozen" } }, current: event, events: [] };
const authority = { record_digest: "a".repeat(64), status: "AVAILABLE", revocation: null,
  record: { schema_version: "JOURNAL_AUTHORITY_RECORD_V1", registered_by: "owner", registered_at: event.at, decision_ref: "offline-approval",
    grant: { authorization_id: "approval-1", expires_at: "2026-09-28T00:05:00Z", max_attempts: 3, max_reserved_output_tokens: 12000 } } };
const updated = vi.fn(), busy = vi.fn();
beforeEach(() => vi.resetAllMocks());
function view(saved = execution) { return render(<JournalOfflineExecution url={url} saved={saved} onUpdated={updated} onBusy={busy}/>); }
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");

it("separates read, explicit approval and dispatch, and never preserves or accepts a candidate automatically", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, options) => {
    if (!options?.method) return { ...capability, authorities: [] } as never;
    if (path.endsWith("/offline-approvals")) return { authority, created: true } as never;
    return { execution: { ...execution, current: { ...event, state: "LEASED", sequence: 1, attempt: 1 } } } as never;
  });
  view();
  fireEvent.click(await screen.findByRole("button", { name: "오프라인 합성 실행 승인 기록" }));
  await screen.findByText(/승인만 기록했습니다/);
  expect(posts()).toHaveLength(1);
  expect(posts()[0][0]).toBe(`${url}/offline-approvals`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 0 });
  expect(updated).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "승인한 오프라인 합성 실행" }));
  await waitFor(() => expect(updated).toHaveBeenCalledOnce());
  expect(posts()).toHaveLength(2);
  expect(JSON.parse(posts()[1][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_sequence: 0, authorization_id: "approval-1", acknowledge_unknown: false });
  expect(posts().every(([path]) => !path.endsWith("/candidate") && !path.includes("/review"))).toBe(true);
  expect(screen.getByText(/실제 AI 모델·논문 전체 검토·과학적 검증을 수행하지 않습니다/)).toBeTruthy();
});

it.each(["approval", "dispatch"])("retries a lost %s response with the exact request and no duplicate new action", async action => {
  let writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_path, options) => {
    if (!options?.method) return { ...capability, authorities: action === "approval" ? [] : [authority] } as never;
    if (++writes === 1) throw new Error("response lost");
    return (action === "approval" ? { authority, created: false } : { execution }) as never;
  });
  view();
  fireEvent.click(await screen.findByRole("button", { name: action === "approval" ? "오프라인 합성 실행 승인 기록" : "승인한 오프라인 합성 실행" }));
  await screen.findByText(/response lost/);
  expect((screen.getByRole("button", { name: "오프라인 합성 실행 승인 기록" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: action === "approval" ? "같은 오프라인 승인 요청 다시 보내기" : "같은 오프라인 실행 요청 다시 보내기" }));
  await waitFor(() => expect(posts()).toHaveLength(2));
  expect(posts()[0]).toEqual(posts()[1]);
  await waitFor(() => expect(busy).toHaveBeenLastCalledWith(false));
});

it("restores an approval after reopen and requires explicit acknowledgment of an unknown attempt before another dispatch", async () => {
  const unknown = { ...execution, current: { ...event, sequence: 3, attempt: 1, state: "INTERRUPTED" as const, outcome_unknown: true } };
  vi.mocked(api.fetchJson).mockResolvedValue({ ...capability, authorities: [authority] });
  view(unknown);
  const dispatch = await screen.findByRole("button", { name: "승인한 오프라인 합성 실행" });
  expect((dispatch as HTMLButtonElement).disabled).toBe(true);
  expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("checkbox", { name: /이전 시도의 결과가 불명확함을 확인/ }));
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ execution: unknown });
  fireEvent.click(dispatch);
  await waitFor(() => expect(updated).toHaveBeenCalledOnce());
  expect(JSON.parse(posts()[0][1]!.body as string)).toMatchObject({ acknowledge_unknown: true, expected_sequence: 3 });
});

it("shows expired and revoked records without dispatch authority and lets the user request a fresh approval", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...capability, authorities: [
    { ...authority, status: "EXPIRED" },
    { ...authority, record_digest: "b".repeat(64), status: "REVOKED", record: { ...authority.record, grant: { ...authority.record.grant, authorization_id: "revoked" } }, revocation: { actor_id: "owner", at: event.at, reason: "withdrawn" } },
  ] });
  view();
  expect((await screen.findByRole("button", { name: "승인한 오프라인 합성 실행" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/기한 만료/)).toBeTruthy();
  expect(screen.getByText(/철회됨/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "오프라인 합성 실행 승인 기록" }) as HTMLButtonElement).disabled).toBe(false);
  expect(posts()).toHaveLength(0);
});

it("refuses actions when capability is disabled or cannot be read", async () => {
  vi.mocked(api.fetchJson).mockRejectedValueOnce(new Error("offline status unavailable")).mockResolvedValueOnce({ ...capability, enabled: false, reason: "journal_offline_disabled", authorities: [] });
  view();
  await screen.findByText(/offline status unavailable/);
  expect(screen.queryByRole("button", { name: "오프라인 합성 실행 승인 기록" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "오프라인 승인 목록 새로고침" }));
  await screen.findByText(/이 환경에서는 오프라인 합성 실행을 사용할 수 없습니다/);
  expect(posts()).toHaveLength(0);
});

it("discards scoped approval state and an in-flight reply when execution changes", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...capability, authorities: [] })
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ ...capability, authorities: [] });
  const { rerender } = view();
  fireEvent.click(await screen.findByRole("button", { name: "오프라인 합성 실행 승인 기록" }));
  rerender(<JournalOfflineExecution url={`${url}-other`} saved={{ ...execution, contract: { ...execution.contract, execution_id: "other" } }} onUpdated={updated} onBusy={busy}/>);
  await screen.findByRole("button", { name: "오프라인 합성 실행 승인 기록" });
  await act(async () => finish({ authority, created: true }));
  expect(screen.queryByText(/승인만 기록했습니다/)).toBeNull();
  expect(screen.queryByText(/approval-1/)).toBeNull();
  expect(updated).not.toHaveBeenCalled();
});
