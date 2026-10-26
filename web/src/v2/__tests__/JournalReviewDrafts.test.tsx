import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalReviewDraftNotice, JournalReviewDraftProvider } from "../JournalReviewDrafts";
import { JournalReviewPanel } from "../views/JournalReviewPanel";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/topics/topic-a/candidates/one/reviews";
const entry = { entry_id: "e1", section: "overview", kind: "observation", text: "Synthetic observation", citation_ids: [] };
const initial = { items: [], latest: null, total: 0, limit: 10, candidate_digest: "digest-one", entries: [entry] };
const judgments = [{ entry_id: "e1", assessment: "not_assessed", note: "" }];
const review = { review_id: "saved", previous_id: null, state: "reviewing", entries: judgments, note: "", actor_id: "human", created_at: "2026-10-05T00:00:00Z", report: null };
const closing = () => { const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; };
const postCalls = () => vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST").map(([, o]) => JSON.parse(o!.body as string));
function deferred<T>() { let resolve!: (v: T) => void; let reject!: (e: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function Session({ path = root, session = "actor-a", copies = 1, initialReviewId }: { path?: string | null; session?: string; copies?: number; initialReviewId?: string }) {
  return <JournalReviewDraftProvider key={session}><JournalReviewDraftNotice/>{path ? Array.from({ length: copies }, (_, n) => <JournalReviewPanel key={`${path}:${n}`} root={path} citations={[]} enabled initiallyOpen initialReviewId={initialReviewId}/>) : <p>Another screen</p>}</JournalReviewDraftProvider>;
}
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockResolvedValue(initial); });

it("preserves exact input on candidate and topic round trips without saving or rebasing it", async () => {
  const view = render(<Session/>);
  await screen.findByLabelText("문장 1 근거 판단");
  fireEvent.change(screen.getByLabelText("문장 1 근거 판단"), { target: { value: "limited" } });
  const note = "e\u0301 한글\u2028line";
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: note } });
  view.rerender(<Session path={null}/>);
  expect(closing()).toBe(true);
  expect(screen.getByText(/저널 검토 1개/)).toBeTruthy();
  view.rerender(<Session path="/topics/topic-b/candidates/one/reviews"/>);
  expect(await screen.findByLabelText("문장 1 검토 메모")).toHaveProperty("value", "");
  vi.mocked(api.fetchJson).mockResolvedValue({ ...initial, latest: { ...review, review_id: "other-human" } });
  view.rerender(<Session/>);
  expect(await screen.findByLabelText("문장 1 검토 메모")).toHaveProperty("value", note);
  expect(screen.getByLabelText("문장 1 근거 판단")).toHaveProperty("value", "limited");
  expect(screen.getByText(/최신 검토와 편집 기준이 다릅니다/)).toBeTruthy();
  expect(postCalls()).toHaveLength(0);
});

it("does not apply a draft to a different candidate digest", async () => {
  const view = render(<Session/>);
  await screen.findByLabelText("저널 전체 검토 메모");
  fireEvent.change(screen.getByLabelText("저널 전체 검토 메모"), { target: { value: "Bound to original digest" } });
  view.rerender(<Session path={null}/>);
  vi.mocked(api.fetchJson).mockResolvedValue({ ...initial, candidate_digest: "different" });
  view.rerender(<Session/>);
  expect(await screen.findByLabelText("저널 전체 검토 메모")).toHaveProperty("value", "");
  expect(closing()).toBe(true);
});

it("retains an unchanged lost-response request across navigation and clears protection only after acknowledgement", async () => {
  let calls = 0, saved = false;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { if (++calls === 1) throw new Error("response lost"); saved = true; return { review, created: false } as never; }
    if (url.endsWith("/saved")) return review as never;
    return (saved ? { ...initial, latest: review, items: [review], total: 1 } : initial) as never;
  });
  const view = render(<Session/>);
  fireEvent.click(await screen.findByRole("button", { name: "검토 내용 저장" }));
  await screen.findByText(/response lost/);
  expect(closing()).toBe(true); // A state-only action still has an unknown save outcome.
  view.rerender(<Session path={null}/>); view.rerender(<Session/>);
  await screen.findByText(/response lost/);
  expect(postCalls()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "검토 내용 저장" }));
  await screen.findByText("같은 요청으로 보존된 검토 기록을 확인했습니다.");
  expect(postCalls()).toHaveLength(2);
  expect(postCalls()[0]).toEqual(postCalls()[1]);
  expect(closing()).toBe(false);
});

it("shares the in-flight lock across two views and receives success after unmount and remount", async () => {
  const response = deferred<unknown>(); let saved = false;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") return await response.promise as never;
    if (url.endsWith("/saved")) return review as never;
    return (saved ? { ...initial, latest: review, items: [review], total: 1 } : initial) as never;
  });
  const view = render(<Session copies={2}/>);
  await waitFor(() => expect(screen.getAllByRole("button", { name: "검토 내용 저장" })).toHaveLength(2));
  const buttons = screen.getAllByRole("button", { name: "검토 내용 저장" });
  act(() => { fireEvent.click(buttons[0]); fireEvent.click(buttons[1]); });
  expect(postCalls()).toHaveLength(1);
  view.rerender(<Session path={null}/>); view.rerender(<Session/>);
  const pending = await screen.findByRole("button", { name: "검토 내용 저장" });
  expect(pending.closest("fieldset")).toHaveProperty("disabled", true);
  expect(closing()).toBe(true);
  saved = true;
  await act(async () => response.resolve({ review, created: true }));
  await screen.findByText("현재 사람의 검토 상태: 검토 중");
  expect(closing()).toBe(false);
  expect(postCalls()).toHaveLength(1);
});

it("keeps failure and request identity when a pending save finishes off-screen", async () => {
  const response = deferred<unknown>();
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => options?.method === "POST" ? await response.promise as never : initial as never);
  const view = render(<Session/>);
  fireEvent.click(await screen.findByRole("button", { name: "검토 내용 저장" }));
  view.rerender(<Session path={null}/>);
  await act(async () => response.reject(new Error("offline failure")));
  view.rerender(<Session/>);
  await screen.findByText(/offline failure/);
  fireEvent.click(screen.getByRole("button", { name: "검토 내용 저장" }));
  await waitFor(() => expect(postCalls()).toHaveLength(2));
  expect(postCalls()[0]).toEqual(postCalls()[1]);
});

it("refreshes a stale history response that arrives after an off-screen save completed", async () => {
  const response = deferred<unknown>(), staleHistory = deferred<unknown>(); let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") return await response.promise as never;
    if (url.endsWith("/saved")) return review as never;
    if (++reads === 2) return await staleHistory.promise as never;
    return (reads > 2 ? { ...initial, latest: review, items: [review], total: 1 } : initial) as never;
  });
  const view = render(<Session/>);
  fireEvent.click(await screen.findByRole("button", { name: "검토 내용 저장" }));
  view.rerender(<Session path={null}/>); view.rerender(<Session/>);
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => response.resolve({ review, created: true }));
  await act(async () => staleHistory.resolve(initial));
  await screen.findByText("현재 사람의 검토 상태: 검토 중");
  expect(screen.queryByText(/최신 검토와 편집 기준이 다릅니다/)).toBeNull();
  expect(reads).toBe(3);
  expect(postCalls()).toHaveLength(1);
});

it("isolates authenticated sessions from old input and late responses", async () => {
  const response = deferred<unknown>();
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => options?.method === "POST" ? await response.promise as never : initial as never);
  const view = render(<Session/>);
  await screen.findByLabelText("저널 전체 검토 메모");
  fireEvent.change(screen.getByLabelText("저널 전체 검토 메모"), { target: { value: "Private actor A" } });
  fireEvent.click(screen.getByRole("button", { name: "검토 내용 저장" }));
  view.rerender(<Session session="actor-b"/>);
  expect(await screen.findByLabelText("저널 전체 검토 메모")).toHaveProperty("value", "");
  await act(async () => response.resolve({ review: { ...review, note: "Private actor A" }, created: true }));
  expect(screen.getByLabelText("저널 전체 검토 메모")).toHaveProperty("value", "");
  expect(screen.queryByText(/Private actor A/)).toBeNull();
  expect(closing()).toBe(false);
  const form = within(screen.getByRole("region", { name: "저널 근거 판단 편집" }));
  expect(form.getByRole("button", { name: "검토 내용 저장" }).closest("fieldset")).toHaveProperty("disabled", false);
});

it("does not replace an explicitly opened historical review with a cached save selection", async () => {
  let saved = false;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { saved = true; return { review, created: true } as never; }
    if (url.endsWith("/historical")) return { ...review, review_id: "historical", note: "Exact older review" } as never;
    if (url.endsWith("/saved")) return review as never;
    return (saved ? { ...initial, latest: review, items: [review], total: 1 } : initial) as never;
  });
  const view = render(<Session/>);
  fireEvent.click(await screen.findByRole("button", { name: "검토 내용 저장" }));
  await screen.findByText("검토 판단을 새 기록으로 보존했습니다.");
  view.rerender(<Session path={null}/>); view.rerender(<Session initialReviewId="historical"/>);
  await screen.findByText("Exact older review");
  await waitFor(() => expect(screen.queryByText("저널 검토 이력 확인 중")).toBeNull());
  expect(screen.getByText("Exact older review")).toBeTruthy();
});
