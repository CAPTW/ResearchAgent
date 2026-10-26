import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalReviewComparison } from "../views/JournalReviewComparison";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/topic/candidate/reviews";
const summary = (id: string, state: string) => ({ review_id: id, state, note: "Overall " + id, actor_id: "reviewer", created_at: "2026-09-27T00:00:00Z", report_version_id: state === "accepted" ? "report-v1" : null });
const entry = { entry_id: "entry1", kind: "interpretation", text: "Preserved interpretation", citation_ids: ["ref1"], changed: true, changes: { assessment: true, note: true }, before: { assessment: "supported", note: "Initial reason" }, after: { assessment: "limited", note: "Abstract only" }, citations: [{ citation_id: "ref1", title: "Frozen source", text: "Original source text", locator: { page: 2 }, source_identity: { provider: "local" }, paper_version_id: "paper-v1" }] };
const value = { candidate_id: "candidate", candidate_digest: "digest", input_digest: "input", packet_id: "packet", citations: entry.citations, from: summary("old", "accepted"), to: summary("new", "held"), overall_changes: { state: true, note: true }, counts: { total: 2, changed: 1, unchanged: 1, assessment_changed: 1, note_changed: 1 }, entries: [entry, { ...entry, entry_id: "entry2", text: "Unchanged question", kind: "question", changed: false, changes: { assessment: false, note: false }, after: entry.before }] };
beforeEach(() => vi.resetAllMocks());

it("does not calculate a zero without a baseline, target or distinct record", () => {
  const view = render(<JournalReviewComparison root={root} reviewId="new" baselineId=""/>);
  expect(screen.getByText(/기준이 없으면 변화량을 계산하지/)).toBeTruthy();
  view.rerender(<JournalReviewComparison root={root} reviewId="" baselineId="old"/>);
  expect(screen.getByText(/비교할 검토 기록을 이력에서/)).toBeTruthy();
  view.rerender(<JournalReviewComparison root={root} reviewId="old" baselineId="old"/>);
  expect(screen.getByText(/같은 기록을 선택/)).toBeTruthy();
  expect(api.fetchJson).not.toHaveBeenCalled();
});

it("shows exact changed judgments and preserved quotations without claiming scientific improvement", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(value);
  render(<JournalReviewComparison root={root} reviewId="new" baselineId="old"/>);
  await screen.findByText("판단·메모 변경 1 / 2문장 · 동일 1문장");
  expect(screen.getByText("전체 상태: 변경 · 전체 메모: 변경")).toBeTruthy();
  expect(screen.getByText(/판단의 개선 여부를 평가하지/)).toBeTruthy();
  expect(screen.getByText("Initial reason")).toBeTruthy();
  expect(screen.getByText("Abstract only")).toBeTruthy();
  expect(screen.queryByText("Unchanged question")).toBeNull();
  fireEvent.click(screen.getByText("비교 문장의 보존 근거 · Frozen source"));
  expect(screen.getByText("Original source text")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "동일 문장도 보기" }));
  expect(screen.getByText("Unchanged question")).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledWith(root + "/new/comparison?from_review_id=old");
});

it("shows zero only for verified identical judgments and distinguishes whole-review notes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ ...value, counts: { ...value.counts, changed: 0, unchanged: 2, assessment_changed: 0, note_changed: 0 }, entries: value.entries.map(e => ({ ...e, changed: false })) });
  render(<JournalReviewComparison root={root} reviewId="new" baselineId="old"/>);
  await screen.findByText("판단·메모 변경 0 / 2문장 · 동일 2문장");
  expect(screen.getByText(/전체 상태와 전체 메모는 위에서 별도로/)).toBeTruthy();
});

it("drops stale comparisons on refresh failure and allows an explicit retry", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(value).mockRejectedValueOnce(new Error("damaged record")).mockResolvedValueOnce(value);
  render(<JournalReviewComparison root={root} reviewId="new" baselineId="old"/>);
  await screen.findByText("판단·메모 변경 1 / 2문장 · 동일 1문장");
  fireEvent.click(screen.getByRole("button", { name: "검토 비교 다시 확인" }));
  await screen.findByRole("alert");
  expect(screen.queryByText("판단·메모 변경 1 / 2문장 · 동일 1문장")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "검토 비교 다시 확인" }));
  await screen.findByText("판단·메모 변경 1 / 2문장 · 동일 1문장");
});

it("ignores a delayed baseline result after changing candidates or clearing the baseline", async () => {
  let resolve!:(value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...value, from: summary("another", "accepted"), entries: [] });
  const view = render(<JournalReviewComparison root={root} reviewId="new" baselineId="old"/>);
  view.rerender(<JournalReviewComparison root="/api/another/reviews" reviewId="new" baselineId="another"/>);
  await screen.findByText("Overall another");
  await act(async () => resolve(value));
  expect(screen.queryByText("Overall old")).toBeNull();
  view.rerender(<JournalReviewComparison root={root} reviewId="new" baselineId=""/>);
  await waitFor(() => expect(screen.queryByText("판단·메모 변경 1 / 2문장 · 동일 1문장")).toBeNull());
});
