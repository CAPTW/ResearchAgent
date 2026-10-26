import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalExecutionResults } from "../views/JournalExecutionResults";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
vi.mock("../views/JournalExchangePanel", () => ({ SavedCandidate: ({ root, candidateId }: { root: string; candidateId: string }) => <p>Opened {root}/{candidateId}</p> }));
const item = { execution_id: "exec-1", created_at: "2026-09-27T00:00:00Z", requested_provider: "test", requested_model: "requested-model", state: "SUCCEEDED", attempt: 1, reason: null, outcome_unknown: false, deadline: null, result_digest: "abc", title: "Saved response", provider_reported: { provider_name: "test", model: "reported-model" } };
const list = { items: [item], page: 1, limit: 10, total: 1 };
beforeEach(() => vi.resetAllMocks());
async function open() {
  const view = render(<JournalExecutionResults topicId="topic" jobId="job"/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("저장된 생성 결과"));
  await screen.findByRole("button", { name: "검토 후보로 보존·열기" });
  return view;
}

it("only preserves on explicit request with the observed result binding and opens that candidate", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => (options?.method === "POST" ? { candidate: { candidate_id: "generated" } } : list) as never);
  await open();
  expect(screen.getByText(/어댑터 보고 모델: test · reported-model/)).toBeTruthy();
  expect(screen.getByText(/실제 외부 실행의 독립적인 증명이 아닙니다/)).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, o]) => !o?.method)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "검토 후보로 보존·열기" }));
  await screen.findByText("Opened /api/topics/topic/local-synthesis/job/journal-candidates/generated");
  expect(api.fetchJson).toHaveBeenLastCalledWith("/api/topics/topic/local-synthesis/job/journal-executions/exec-1/candidate", { method: "POST", body: JSON.stringify({ expected_result_digest: "abc" }) });
});

it("identifies an unreported model in the result list without displaying the requested model as the response model", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...list, items: [{ ...item, requested_provider: "codex_cli", requested_model: "requested-codex-model", provider_reported: { provider_name: "codex_cli", model: "not_reported" } }] });
  await open();
  expect(screen.getByText("어댑터 보고 모델: codex_cli · 실제 보고 모델 미확인")).toBeTruthy();
  expect(screen.queryByText(/not_reported/)).toBeNull();
  expect(screen.queryByText(/어댑터 보고 모델:.*requested-codex-model/)).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it("retries a lost preservation response with identical binding and keeps reader on history failure", async () => {
  let writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => {
    if (options?.method === "POST") { if (++writes === 1) throw new Error("response lost"); return { candidate: { candidate_id: "generated" } } as never; }
    if (writes > 1) throw new Error("history offline");
    return list as never;
  });
  await open();
  fireEvent.click(screen.getByRole("button", { name: "검토 후보로 보존·열기" }));
  await screen.findByText(/response lost/);
  fireEvent.click(screen.getByRole("button", { name: "검토 후보로 보존·열기" }));
  await screen.findByText(/Opened/);
  const posts = vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST");
  expect(posts[0][1]).toEqual(posts[1][1]);
  fireEvent.click(screen.getByRole("button", { name: "저장된 생성 결과 새로고침" }));
  await screen.findByText(/history offline/);
  expect(screen.queryByText("Saved response")).toBeNull();
  expect(screen.getByText(/Opened/)).toBeTruthy();
});

it("shows pending, failed, cancelled and uncertain records without preservation or generation actions", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...list, total: 4, items: ["RUNNING", "FAILED", "CANCELLED", "INTERRUPTED"].map((state, i) => ({ ...item, execution_id: String(i), state, title: null, result_digest: null, provider_reported: null, reason: state === "FAILED" ? "output_invalid" : null, outcome_unknown: state === "INTERRUPTED" })) });
  render(<JournalExecutionResults topicId="topic" jobId="job"/>);
  fireEvent.click(screen.getByText("저장된 생성 결과"));
  await screen.findByText(/외부 호출의 완료 여부는 확인되지 않았습니다/);
  expect(screen.getByText(/현재도 실행 중이라는 보장이 아닙니다/)).toBeTruthy();
  expect(screen.getByText("출력 형식·입력·인용 검증 실패")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "검토 후보로 보존·열기" })).toBeNull();
  expect(screen.queryByRole("button", { name: /생성 실행|재시도 실행/ })).toBeNull();
});

it("paginates stored records and drops stale responses/reader when changing job", async () => {
  let resolve!:(value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...list, total: 11 })
    .mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...list, items: [], total: 0 });
  const view = await open();
  fireEvent.click(screen.getByRole("button", { name: "다음 실행 기록 페이지" }));
  expect(api.fetchJson).toHaveBeenLastCalledWith("/api/topics/topic/local-synthesis/job/journal-executions?page=2&limit=10");
  view.rerender(<JournalExecutionResults topicId="topic" jobId="other"/>);
  await screen.findByText(/저장된 실행 기록이 없습니다/);
  await act(async () => resolve(list));
  await waitFor(() => expect(screen.queryByText("Saved response")).toBeNull());
});

it("loads the isolated offline capability and labels persisted synthetic results without scientific claims", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/journal-offline")) return { enabled: true, mode: "OFFLINE_SYNTHETIC", provider_name: "xrml_offline_synthetic", model: "synthetic-journal-v1", reason: null } as never;
    return { ...list, items: [{ ...item, requested_provider: "xrml_offline_synthetic", requested_model: "synthetic-journal-v1" }] } as never;
  });
  render(<JournalExecutionResults topicId="topic" jobId="job" inputDigest={"a".repeat(64)}/>);
  fireEvent.click(screen.getByText("저장된 생성 결과"));
  await screen.findByText("오프라인 합성 실행 기록 · 실제 AI 본문 검토가 아닙니다.");
  fireEvent.click(screen.getByText("새 생성 요청 준비"));
  await screen.findByRole("checkbox", { name: "오프라인 합성 요청 준비" });
  expect(api.fetchJson).toHaveBeenCalledWith("/api/topics/topic/local-synthesis/job/journal-offline");
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method)).toBe(true);
});
