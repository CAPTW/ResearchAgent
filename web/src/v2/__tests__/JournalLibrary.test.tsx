import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalLibraryPanel } from "../views/JournalLibraryPanel";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
vi.mock("../views/JournalExchangePanel", () => ({ SavedCandidate: ({ root, candidateId, initialReviewId, onReviewSaved }: { root: string; candidateId: string; initialReviewId?: string; onReviewSaved: () => void }) => <div aria-label="opened candidate">{root}/{candidateId}/{initialReviewId}<button onClick={onReviewSaved}>save review fixture</button></div> }));
const row = { title: "Preserved journal", candidate_id: "candidate-a", job_id: "job-a", imported_at: "2026-09-27T00:00:00Z", latest_review: { review_id: "held", state: "held", created_at: "2026-09-27T01:00:00Z" }, last_acceptance: { review_id: "accepted-old", version_id: "v1", created_at: "2026-09-27T00:30:00Z" } };
const listing = { items: [row], total: 1, page: 1, limit: 10 };
beforeEach(() => vi.resetAllMocks());
it.each([
  ["accepted", "저널로 채택", "원문 대조 초안 (미채택)"],
  ["held", "보류", "채택 완료 · 원래 제목"],
  ["rejected", "검토 대상에서 제외", "최종 승인 원고"],
  ["reviewing", "검토 중", "완료된 원고"],
  [null, "미검토", "Accepted journal · e\u0301 한글"],
])("uses review records, not preserved title wording, for current state %s", async (state, label, title) => {
  const original = { ...row, title, latest_review: state ? { ...row.latest_review, state } : null, last_acceptance: state === "accepted" ? { ...row.last_acceptance, review_id: row.latest_review.review_id } : null };
  const before = JSON.stringify(original);
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing, items: [original] });
  render(<JournalLibraryPanel topicId="topic"/>);
  const heading = await screen.findByRole("heading", { name: `보존된 원고 제목 ${title}` });
  const card = heading.closest("li")!;
  expect(within(card).getByText(title!, { exact: true }).textContent).toBe(title);
  expect(within(card).getByText(new RegExp(`현재 검토:.*${label}`))).toBeTruthy();
  expect(screen.getByText(/제목은 후보를 보존한 당시의 원문/)).toBeTruthy();
  expect(JSON.stringify(original)).toBe(before);
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/api/topics/topic/local-synthesis/journals?page=1&limit=10");
});
it("distinguishes generated provenance from imported rows and uses preserved date", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...listing, total: 2, items: [row, { ...row, candidate_id: "generated", title: "Generated journal", origin: "RECORDED_ADAPTER_RESULT", preserved_at: "2026-09-28T00:00:00Z" }] });
  render(<JournalLibraryPanel topicId="topic"/>);
  await screen.findByText("Generated journal");
  expect(screen.getByText(/저장된 생성 결과 · 보존 날짜:/)).toBeTruthy();
  expect(screen.getByText(/파일로 가져온 후보 · 보존 날짜:/)).toBeTruthy();
  expect(screen.getByText("보존된 저널 2개 · 현재 페이지 2개")).toBeTruthy();
});

it("distinguishes held state from historical acceptance and opens that exact preserved review", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(listing);
  render(<JournalLibraryPanel topicId="topic-a"/>);
  const open = await screen.findByRole("button", { name: "보존 채택본·검토 열기" });
  expect(screen.getByText(/현재 검토: 보류/)).toBeTruthy();
  expect(screen.getByText(/현재 채택 상태가 아닌 과거 기록/)).toBeTruthy();
  fireEvent.click(open);
  expect(screen.getByLabelText("opened candidate").textContent).toContain("/topic-a/local-synthesis/job-a/journal-candidates/candidate-a/accepted-old");
  expect(document.activeElement?.textContent).toBe("선택한 저널 · 보존된 제목: Preserved journal");
  fireEvent.click(screen.getByRole("button", { name: "보관함 저널 닫기" }));
  expect(document.activeElement).toBe(open);
});

it("paginates unreviewed candidates and refreshes current state after a saved review", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce({ ...listing, total: 11 }).mockResolvedValueOnce({ ...listing, page: 2, total: 11, items: [{ ...row, title: "Unreviewed", last_acceptance: null, latest_review: null }] }).mockResolvedValueOnce({ ...listing, page: 2, total: 11 });
  render(<JournalLibraryPanel topicId="topic"/>);
  fireEvent.click(await screen.findByRole("button", { name: "다음 주제 저널 페이지" }));
  await screen.findByText("현재 검토: 미검토");
  fireEvent.click(screen.getByRole("button", { name: "후보·검토 열기" }));
  fireEvent.click(screen.getByText("save review fixture"));
  await screen.findByText(/현재 검토: 보류/);
  expect(api.fetchJson).toHaveBeenLastCalledWith("/api/topics/topic/local-synthesis/journals?page=2&limit=10");
  expect(screen.getByLabelText("opened candidate")).toBeTruthy();
});

it("does not present a failed refresh as an empty or current successful list and retains the open reader", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(listing).mockRejectedValueOnce(new Error("store offline")).mockResolvedValueOnce({ ...listing, items: [], total: 0 });
  render(<JournalLibraryPanel topicId="topic"/>);
  fireEvent.click(await screen.findByRole("button", { name: "보존 채택본·검토 열기" }));
  fireEvent.click(screen.getByRole("button", { name: "주제 저널 새로고침" }));
  await screen.findByRole("alert");
  expect(screen.queryByText(/현재 검토: 보류/)).toBeNull();
  expect(screen.queryByText(/보존된 저널이 없습니다/)).toBeNull();
  expect(screen.getByLabelText("opened candidate")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "주제 저널 새로고침" }));
  await screen.findByText(/보존된 저널이 없습니다/);
});

it("drops a previous topic's selected candidate and late response", async () => {
  let resolve!:(value: unknown) => void;
  vi.mocked(api.fetchJson).mockResolvedValueOnce(listing).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...listing, items: [], total: 0 });
  const view = render(<JournalLibraryPanel topicId="topic-a"/>);
  fireEvent.click(await screen.findByRole("button", { name: "보존 채택본·검토 열기" }));
  fireEvent.click(screen.getByRole("button", { name: "주제 저널 새로고침" }));
  view.rerender(<JournalLibraryPanel topicId="topic-b"/>);
  await screen.findByText(/보존된 저널이 없습니다/);
  await act(async () => resolve(listing));
  await waitFor(() => expect(within(screen.getByRole("region", { name: "주제 저널 보관함" })).queryByText("Preserved journal")).toBeNull());
  expect(screen.queryByLabelText("opened candidate")).toBeNull();
});
