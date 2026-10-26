import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalReviewPanel } from "../views/JournalReviewPanel";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const root = "/api/candidate/reviews";
const citations = [{ citation_id: "ref1", title: "Original source", text: "Exact frozen quotation", locator: { page: 2 } }];
const entries = [{ entry_id: "entry1", section: "synthesis", kind: "interpretation", text: "Imported interpretation", citation_ids: ["ref1"] }];
const initial = { items: [], latest: null, total: 0, limit: 10, candidate_digest: "digest", entries };
const review = (id: string, state = "accepted") => ({ review_id: id, previous_id: null, state, actor_id: "human", created_at: "2026-09-27T00:00:00Z", note: "Review note", entries: [{ entry_id: "entry1", assessment: "supported", note: "Source inspected" }], report: state === "accepted" ? { version_id: "report-" + id, content: "# Reviewed manuscript", content_sha256: "hash" } : null });
const history = (id: string, state = "accepted") => ({ ...initial, items: [review(id, state)], latest: review(id, state), total: 1 });
beforeEach(() => vi.resetAllMocks());
afterEach(() => { Reflect.deleteProperty(window, "__XRML_NATIVE_DOWNLOADS__"); Reflect.deleteProperty(window, "__TAURI__"); });
it("finds exact frozen evidence without changing judgments, notes or making requests", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(initial);
  await open();
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Keep this pending note" } });
  fireEvent.click(screen.getByText("검토 근거 · Original source"));
  const before = vi.mocked(api.fetchJson).mock.calls.length;
  const search = screen.getByRole("textbox", { name: "근거 안에서 문구 찾기" });
  fireEvent.change(search, { target: { value: "frozen" } });
  expect(screen.getByText("일치 1 / 1 · 근거 내부 문자 위치 6–12")).toBeTruthy();
  expect(screen.getByLabelText("문장 1 근거 판단")).toHaveProperty("value", "not_assessed");
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Keep this pending note");
  expect(api.fetchJson).toHaveBeenCalledTimes(before);
});
it("allows saved history and comparison when a changed source disables new judgments and exports", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.includes('/comparison?')) return { from: { ...review('old'), report_version_id: 'old' }, to: { ...review('new'), report_version_id: 'new' }, overall_changes: { state: false, note: false }, counts: { total: 1, changed: 0, unchanged: 1, assessment_changed: 0, note_changed: 0 }, entries: [], citations: [] } as never;
    if (url.endsWith('/new')) return review('new') as never;
    return { ...history('new'), items: [review('new'), review('old')], total: 2 } as never;
  });
  render(<JournalReviewPanel root={root} citations={citations} enabled={false} initiallyOpen/>);
  const openSaved = await screen.findByRole('button', { name: /저널로 채택 ·.*new/ });
  expect(openSaved).toHaveProperty('disabled', false); fireEvent.click(openSaved);
  await screen.findByText('보존 판단: 저널로 채택');
  expect(screen.getByText(/제목·본문은 채택 당시 보존된 원문/)).toBeTruthy();
  expect(screen.getByRole('button', { name: '채택 저널 Markdown 내려받기' })).toHaveProperty('disabled', true);
  expect(screen.getByRole('button', { name: '저널로 채택' }).closest('fieldset')).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByRole('button', { name: '검토 2번을 비교 기준으로 선택' }));
  await screen.findByText('판단·메모 변경 0 / 1문장 · 동일 1문장');
  expect(vi.mocked(api.fetchJson).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
});
it("keeps the selected review and pending notes while choosing a comparison baseline on an older page", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.includes("/comparison?")) return { from: { ...review("r1"), report_version_id: "v1" }, to: { ...review("r2"), report_version_id: "v2" }, overall_changes: { state: false, note: false }, counts: { total: 1, changed: 0, unchanged: 1, assessment_changed: 0, note_changed: 0 }, entries: [], citations: [] } as never;
    if (url.endsWith("/r2")) return review("r2") as never;
    return { ...history("r2"), total: 11, items: [review(url.includes("page=2") ? "r1" : "r2")] } as never;
  });
  await open();
  fireEvent.click(screen.getByRole("button", { name: /저널로 채택 ·.*r2/ }));
  await screen.findByText("보존 판단: 저널로 채택");
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Pending notes remain mine" } });
  fireEvent.click(screen.getByRole("button", { name: "다음 검토 페이지" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "검토 11번을 비교 기준으로 선택" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "검토 11번을 비교 기준으로 선택" }));
  await screen.findByText("판단·메모 변경 0 / 1문장 · 동일 1문장");
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Pending notes remain mine");
  expect(api.fetchJson).toHaveBeenCalledWith(root + "/r2/comparison?from_review_id=r1");
  expect(vi.mocked(api.fetchJson).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "검토 비교 기준 지우기" }));
  expect(screen.getByText(/기준이 없으면 변화량을 계산하지/)).toBeTruthy();
});
it("opens an exact historical acceptance outside the current history page without enabling stale exports", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith("/old") ? review("old") as never : { ...history("new", "held"), total: 25 } as never);
  render(<JournalReviewPanel root={root} citations={citations} enabled initialReviewId="old" initiallyOpen/>);
  await screen.findByText("Reviewed manuscript");
  expect(screen.getByText("현재 사람의 검토 상태: 보류")).toBeTruthy();
  expect(screen.getByRole("button", { name: "채택 저널 Markdown 내려받기" })).toHaveProperty("disabled", true);
  expect(screen.getByText("Reviewed manuscript").closest("details")).toHaveProperty("open", true);
});
async function open() {
  const view = render(<JournalReviewPanel root={root} citations={citations} enabled/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("사람의 근거 검토·저널 채택"));
  await screen.findByLabelText("문장 1 근거 판단");
  return view;
}
function choose(value: string) { fireEvent.change(screen.getByLabelText("문장 1 근거 판단"), { target: { value } }); }
function editor() { return screen.getByRole("region", { name: "저널 근거 판단 편집" }); }

it("warns before closing with unsaved review input and removes the warning when reverted", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(initial);
  await open();
  const closing = () => { const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; };
  expect(closing()).toBe(false);
  fireEvent.change(screen.getByLabelText("저널 전체 검토 메모"), { target: { value: "Do not lose this" } });
  expect(closing()).toBe(true);
  fireEvent.change(screen.getByLabelText("저널 전체 검토 메모"), { target: { value: "" } });
  expect(closing()).toBe(false);
});

it("requires an explicit choice before replacing pending judgments with the latest record", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(history("r1"));
  await open();
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Keep my reasoning" } });
  fireEvent.click(screen.getByRole("button", { name: "검토 입력을 최신 기록으로 교체" }));
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Keep my reasoning");
  const keep = screen.getByRole("button", { name: "입력 계속 편집" }); keep.focus(); fireEvent.click(keep);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "검토 입력을 최신 기록으로 교체" }));
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Keep my reasoning");
  fireEvent.click(screen.getByRole("button", { name: "검토 입력을 최신 기록으로 교체" }));
  const discard = screen.getByRole("button", { name: "미저장 입력 버리고 교체" }); discard.focus(); fireEvent.click(discard);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "검토 입력을 최신 기록으로 교체" }));
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Source inspected");
  expect(vi.mocked(api.fetchJson).mock.calls.some(([, o]) => o?.method === "POST")).toBe(false);
});

it("requires reviewed support, reasons for limitations and preserves an explicit acceptance against the exact candidate", async () => {
  let saved = false;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { saved = true; return { review: review("r1"), created: true } as never; }
    if (url.endsWith("/r1")) return review("r1") as never;
    return (saved ? history("r1") : initial) as never;
  });
  await open();
  expect(within(editor()).getByRole("button", { name: "저널로 채택" })).toHaveProperty("disabled", true);
  choose("unsupported");
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Not supported yet" } });
  expect(within(editor()).getByRole("button", { name: "저널로 채택" })).toHaveProperty("disabled", true);
  choose("limited");
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "" } });
  expect(within(editor()).getByRole("button", { name: "저널로 채택" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Abstract only" } });
  fireEvent.click(within(editor()).getByText("검토 근거 · Original source"));
  expect(within(editor()).getByText("Exact frozen quotation")).toBeTruthy();
  fireEvent.click(within(editor()).getByRole("button", { name: "저널로 채택" }));
  await screen.findByText("현재 사람의 검토 상태: 저널로 채택");
  await screen.findByText("보존 판단: 저널로 채택");
  const post = vi.mocked(api.fetchJson).mock.calls.find(([, options]) => options?.method === "POST")![1]!.body as string;
  expect(JSON.parse(post)).toMatchObject({ expected_previous_id: null, expected_candidate_digest: "digest", state: "accepted", entries: [{ entry_id: "entry1", assessment: "limited", note: "Abstract only" }] });
  expect(screen.getByRole("button", { name: "채택 저널 Markdown 내려받기" })).toHaveProperty("disabled", false);
});

it("retries an unchanged lost-response request and does not reuse its identity for a different decision", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => { if (options?.method === "POST") throw new Error("response lost"); return initial as never; });
  await open(); choose("supported");
  for (let i = 0; i < 2; i++) { fireEvent.click(within(editor()).getByRole("button", { name: "저널로 채택" })); await screen.findByText(/response lost/); }
  fireEvent.click(within(editor()).getByRole("button", { name: "보류" }));
  await waitFor(() => expect(vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST")).toHaveLength(3));
  const posts = vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST").map(([, o]) => JSON.parse(o!.body as string));
  expect(posts[0]).toEqual(posts[1]);
  expect(posts[2].request_id).not.toBe(posts[1].request_id);
  expect(posts[2].state).toBe("held");
});

it("keeps pending judgments on conflict and requires explicit adoption of a refreshed predecessor", async () => {
  let latest = "r1", writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => {
    if (options?.method === "POST") { writes++; if (writes === 1) { latest = "r2"; throw new api.ApiError("conflict", 409, ""); } return { review: review("r3", "reviewing"), created: true } as never; }
    return history(latest) as never;
  });
  await open();
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "My pending reason" } });
  fireEvent.click(within(editor()).getByRole("button", { name: "검토 내용 저장" }));
  await screen.findByText(/다른 검토 기록이 먼저 저장/);
  expect(within(editor()).getByRole("button", { name: "검토 내용 저장" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "저널 검토 이력 새로고침" }));
  await screen.findByText(/최신 검토와 편집 기준이 다릅니다/);
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "My pending reason");
  fireEvent.click(screen.getByRole("button", { name: "최신 검토를 기준으로 입력 유지" }));
  fireEvent.click(within(editor()).getByRole("button", { name: "검토 내용 저장" }));
  await waitFor(() => expect(writes).toBe(2));
  const post = vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST")[1][1]!.body as string;
  expect(JSON.parse(post)).toMatchObject({ expected_previous_id: "r2", entries: [{ note: "My pending reason" }] });
});

it("keeps old accepted reports readable after hold but disables their export", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.endsWith("/r1")) return review("r1") as never;
    return { ...history("r2", "held"), items: [review("r2", "held"), review("r1")], total: 2 } as never;
  });
  await open();
  fireEvent.click(screen.getByRole("button", { name: /저널로 채택 ·.*r1/ }));
  await screen.findByText("보존 판단: 저널로 채택");
  expect(screen.getByText("현재 사람의 검토 상태: 보류")).toBeTruthy();
  expect(screen.getByRole("button", { name: "채택 저널 Markdown 내려받기" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByText("보존된 채택 저널 읽기"));
  expect(screen.getByText("Reviewed manuscript")).toBeTruthy();
});

it("preserves entered text on history failure and disables saves until refreshed", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(initial).mockRejectedValueOnce(new Error("offline"));
  await open(); choose("supported");
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Keep my notes" } });
  fireEvent.click(screen.getByRole("button", { name: "저널 검토 이력 새로고침" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Keep my notes");
  expect(screen.getByLabelText("문장 1 검토 메모").closest("fieldset")!.parentElement!.closest("fieldset")!.disabled).toBe(true);
});

it("does not inherit a previous candidate's late review response", async () => {
  let resolve!:(value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValue(initial);
  const view = render(<JournalReviewPanel root={root} citations={citations} enabled/>);
  fireEvent.click(screen.getByText("사람의 근거 검토·저널 채택"));
  await waitFor(() => expect(api.fetchJson).toHaveBeenCalledTimes(1));
  view.rerender(<JournalReviewPanel root="/another/reviews" citations={citations} enabled/>);
  await screen.findByLabelText("문장 1 근거 판단");
  await act(async () => resolve(history("old-accepted")));
  expect(screen.getByText("현재 사람의 검토 상태: 미검토")).toBeTruthy();
  expect(screen.queryByText(/old-accepted/)).toBeNull();
  expect(screen.getByLabelText("문장 1 근거 판단")).toHaveProperty("value", "not_assessed");
});

it("refreshes after the server refuses stale acceptance export without losing unsaved review notes", async () => {
  const invoke = vi.fn();
  Object.assign(window, { __XRML_NATIVE_DOWNLOADS__: 1, __TAURI__: { core: { invoke } } });
  let held = false;
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.includes("/export?")) { held = true; throw new api.ApiError("journal_review_export_not_accepted", 409, ""); }
    if (url.endsWith("/r1")) return review("r1") as never;
    return (held ? { ...history("r2", "held"), items: [review("r2", "held"), review("r1")], total: 2 } : history("r1")) as never;
  });
  await open();
  fireEvent.change(screen.getByLabelText("문장 1 검토 메모"), { target: { value: "Unsaved follow-up" } });
  fireEvent.click(screen.getByRole("button", { name: /저널로 채택 ·.*r1/ }));
  await screen.findByText("보존 판단: 저널로 채택");
  fireEvent.click(screen.getByRole("button", { name: "채택 저널 Markdown 내려받기" }));
  await screen.findByText(/내보내기 실패/);
  await screen.findByText("현재 사람의 검토 상태: 보류");
  expect(screen.getByLabelText("문장 1 검토 메모")).toHaveProperty("value", "Unsaved follow-up");
  expect(screen.getByRole("button", { name: "채택 저널 Markdown 내려받기" })).toHaveProperty("disabled", true);
  expect(invoke).not.toHaveBeenCalled();
});

it("holds the native export button until selection completes and permits retry after cancel", async () => {
  let complete!: (result: unknown) => void;
  const invoke = vi.fn(() => new Promise(resolve => { complete = resolve; }));
  Object.assign(window, { __XRML_NATIVE_DOWNLOADS__: 1, __TAURI__: { core: { invoke } } });
  const content = "# 검토\r\n원문 보존";
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.includes("/export?")) return { content, filename: "review.md", media_type: "text/markdown" } as never;
    if (url.endsWith("/r1")) return review("r1") as never;
    return history("r1") as never;
  });
  await open();
  fireEvent.click(screen.getByRole("button", { name: /저널로 채택 ·.*r1/ }));
  const button = await screen.findByRole("button", { name: "채택 저널 Markdown 내려받기" });
  fireEvent.click(button);
  await waitFor(() => expect(invoke).toHaveBeenCalledExactlyOnceWith("save_text_export", { content, filename: "review.md" }));
  expect(button).toHaveProperty("disabled", true);
  fireEvent.click(button);
  expect(invoke).toHaveBeenCalledTimes(1);
  await act(async () => complete({ state: "cancelled", path: null, bytes: 0 }));
  expect(button).toHaveProperty("disabled", false);
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(button);
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  await act(async () => complete({ state: "saved", path: "D:\\review.md", bytes: new TextEncoder().encode(content).length }));
  expect(button).toHaveProperty("disabled", false);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});
