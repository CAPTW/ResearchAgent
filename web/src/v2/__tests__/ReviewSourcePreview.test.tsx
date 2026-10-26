import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewPreparation } from "../components/ReviewPreparation";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const body = "a".repeat(4000) + "b".repeat(4000) + "e\u0301🙂";
const source = { run_id: "run", paper_id: "paper", source_digest: "a".repeat(64), source_chars: 8003, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 8007, extraction_truncation: "NOT_OBSERVED" };
const sources = { sources: [source], limits: { max_selected_chars: 12000 }, state: "NOT_EXECUTED" };
const history = { items: [], hasMore: false, page: 1, pageSize: 10 };
function preview(start: number, chosen = source) {
  const text = Array.from(body).slice(start, start + 4000).join("");
  return { schema_version: "PAPER_REVIEW_PREVIEW_V1", source: chosen, window: { start, end: start + Array.from(text).length, text, chars: Array.from(text).length }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT", state: "NOT_EXECUTED" };
}
const requests = () => vi.mocked(api.fetchJson).mock.calls.filter(([path]) => path.endsWith("/preview"));
const saves = () => vi.mocked(api.fetchJson).mock.calls.filter(([path, init]) => init?.method === "POST" && !path.endsWith("/preview"));
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => (
    path.endsWith("/preview") ? preview(JSON.parse(String(init?.body)).start) : path.endsWith("/sources") ? sources : history
  ) as never);
});
async function open() {
  const view = render(<ReviewPreparation topicId="topic" paperVersionId="version"/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  await screen.findByRole("combobox", { name: "저장 본문 출처" });
  return view;
}
async function load() {
  fireEvent.click(screen.getByRole("button", { name: "본문 미리보기" }));
  return await screen.findByRole("textbox", { name: "저장 본문 미리보기" }) as HTMLTextAreaElement;
}
async function jump(start: number) {
  fireEvent.change(screen.getByLabelText("미리보기 시작 위치"), { target: { value: String(start) } });
  fireEvent.click(screen.getByRole("button", { name: "이 위치 미리보기" }));
  return await screen.findByRole("textbox", { name: "저장 본문 미리보기" }) as HTMLTextAreaElement;
}
function select(textarea: HTMLTextAreaElement, start: number, end: number) {
  textarea.focus(); textarea.setSelectionRange(start, end); fireEvent.select(textarea);
}

it("only reads explicitly and applies a keyboard-selected Unicode tail without saving or executing", async () => {
  await open();
  expect(requests()).toHaveLength(0);
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  await load();
  expect(JSON.parse(String(requests()[0][1]?.body))).toEqual({ run_id: "run", paper_id: "paper", expected_source_digest: source.source_digest, start: 0, length: 4000 });
  const jumpInput = screen.getByLabelText("미리보기 시작 위치");
  fireEvent.change(jumpInput, { target: { value: "8000" } });
  jumpInput.focus(); await userEvent.setup().keyboard("{Enter}");
  const tail = await screen.findByRole("textbox", { name: "저장 본문 미리보기" }) as HTMLTextAreaElement;
  expect(tail.value).toBe("e\u0301🙂"); expect(tail.readOnly).toBe(true);
  tail.focus(); await userEvent.setup().keyboard("{Control>}a{/Control}");
  fireEvent.select(tail);
  const apply = screen.getByRole("button", { name: "선택한 본문을 고정 범위로 사용" });
  apply.focus(); await userEvent.setup().keyboard("{Enter}");
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "8000");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "8003");
  expect(screen.getByText(/미리보기 구간: 8000–8003/)).toBeTruthy();
  expect(screen.getByText(/미리보기는 모델 실행·구간 저장을 하지 않습니다/)).toBeTruthy();
  expect(saves()).toHaveLength(0);
});

it("rejects half-surrogate selection while allowing a combining mark boundary", async () => {
  await open(); await load(); const tail = await jump(8000);
  select(tail, 3, 4);
  expect(screen.getByRole("button", { name: "선택한 본문을 고정 범위로 사용" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/문자 경계/)).toBeTruthy();
  select(tail, 1, 4);
  fireEvent.click(screen.getByRole("button", { name: "선택한 본문을 고정 범위로 사용" }));
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "8001");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "8003");
  expect(saves()).toHaveLength(0);
});

it("clears old text and selection while paging, retries the failed window and keeps manual range", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/preview")) { if (++reads === 2) throw new Error("preview unavailable"); return preview(JSON.parse(String(init?.body)).start) as never; }
    return (path.endsWith("/sources") ? sources : history) as never;
  });
  await open(); const first = await load(); select(first, 0, 5);
  fireEvent.click(screen.getByRole("button", { name: "다음 본문 구간" }));
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  await screen.findByText(/preview unavailable/);
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "본문 미리보기 다시 시도" }));
  const next = await screen.findByRole("textbox", { name: "저장 본문 미리보기" });
  expect(next).toHaveProperty("value", "b".repeat(4000));
  expect(screen.getByRole("button", { name: "선택한 본문을 고정 범위로 사용" })).toHaveProperty("disabled", true);
  expect(JSON.parse(String(requests()[2][1]?.body))).toEqual(JSON.parse(String(requests()[1][1]?.body)));
  fireEvent.click(screen.getByRole("button", { name: "이전 본문 구간" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "저장 본문 미리보기" })).toHaveProperty("value", "a".repeat(4000)));
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "0");
  expect(saves()).toHaveLength(0);
});

it("cancels a pending read, keeps capture and source locked during it, and ignores its late response", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async path => path.endsWith("/preview") ? new Promise(resolve => { finish = resolve; }) as never : (path.endsWith("/sources") ? sources : history) as never);
  await open(); fireEvent.click(screen.getByRole("button", { name: "본문 미리보기" }));
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("combobox", { name: "저장 본문 출처" }).matches(":disabled")).toBe(true);
  expect(screen.getByRole("button", { name: "저장 본문 출처 새로고침" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "미리보기 요청 취소" }));
  expect(requests()[0][1]?.signal?.aborted).toBe(true);
  await act(async () => finish(preview(0)));
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", false);
  expect(saves()).toHaveLength(0);
});

it.each([404, 409])("blocks capture after preview %s until a successful explicit source refresh, retaining coordinates", async status => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/preview")) throw new api.ApiError("review_preparation_source_changed", status, "{}");
    if (path.endsWith("/sources")) { if (++reads === 2) throw new Error("source unavailable"); return sources as never; }
    return history as never;
  });
  await open(); fireEvent.change(screen.getByLabelText("시작 위치"), { target: { value: "42" } });
  fireEvent.click(screen.getByRole("button", { name: "본문 미리보기" }));
  await screen.findByText(/review_preparation_source_changed/);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  expect(reads).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText(/source unavailable/);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", false));
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "42");
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  expect(saves()).toHaveLength(0);
});

it("drops source preview on source refresh or source selection and rejects late responses across topics", async () => {
  const other = { ...source, run_id: "second-run", source_digest: "c".repeat(64) };
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => path.endsWith("/preview") ? preview(JSON.parse(String(init?.body)).start) as never : (path.endsWith("/sources") ? { ...sources, sources: [source, other] } : history) as never);
  const view = await open(); await load();
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "본문 미리보기" })).toHaveProperty("disabled", false));
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  await load();
  fireEvent.change(screen.getByRole("combobox", { name: "저장 본문 출처" }), { target: { value: JSON.stringify([other.run_id, other.paper_id]) } });
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  let finish!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async path => path.endsWith("/preview") ? new Promise(resolve => { finish = resolve; }) as never : (path.endsWith("/sources") ? sources : history) as never);
  fireEvent.click(screen.getByRole("button", { name: "본문 미리보기" }));
  view.rerender(<ReviewPreparation topicId="other" paperVersionId="version"/>);
  await act(async () => finish(preview(0, other)));
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  await screen.findByRole("combobox", { name: "저장 본문 출처" });
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "0");
  expect(saves()).toHaveLength(0);
});

it("does not show an inconsistent source or window response as usable text", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/preview")) return (++reads === 1 ? { ...preview(0), source: { ...source, source_digest: "wrong-source" } } : preview(0)) as never;
    return (path.endsWith("/sources") ? sources : history) as never;
  });
  await open(); fireEvent.click(screen.getByRole("button", { name: "본문 미리보기" }));
  await screen.findByText(/요청한 출처·구간과 미리보기 응답이 일치하지 않습니다/);
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "본문 미리보기 다시 시도" }));
  await screen.findByRole("textbox", { name: "저장 본문 미리보기" });
  expect(saves()).toHaveLength(0);
});

it("removes preview controls while a capture response is unknown and preserves the exact pending input", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/preview")) return preview(0) as never;
    if (init?.method === "POST") throw new api.ApiError("capture timeout", 408, "{}");
    return (path.endsWith("/sources") ? sources : history) as never;
  });
  await open(); await load(); fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" }));
  await screen.findByText(/capture timeout/);
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  expect(screen.queryByRole("button", { name: "본문 미리보기" })).toBeNull();
  expect(screen.getByRole("button", { name: "저장 본문 출처 새로고침" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 고정 요청 다시 보내기" }));
  await screen.findByText(/capture timeout/);
  expect(saves()).toHaveLength(2);
  expect(saves()[1]).toEqual(saves()[0]);
});

it("keeps a changed-source warning after refresh while retaining only numeric coordinates", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/sources")) return { ...sources, sources: [{ ...source, source_digest: ++reads === 1 ? source.source_digest : "d".repeat(64) }] } as never;
    if (path.endsWith("/preview")) return preview(JSON.parse(String(init?.body)).start) as never;
    return history as never;
  });
  await open(); const text = await load(); select(text, 15, 25);
  fireEvent.click(screen.getByRole("button", { name: "선택한 본문을 고정 범위로 사용" }));
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText("출처가 변경되어 숫자 위치만 유지했습니다. 본문을 미리보고 범위를 다시 확인하세요.");
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "15");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "25");
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await waitFor(() => expect(reads).toBe(3));
  expect(screen.getByText("출처가 변경되어 숫자 위치만 유지했습니다. 본문을 미리보고 범위를 다시 확인하세요.")).toBeTruthy();
  expect(requests()).toHaveLength(1); expect(saves()).toHaveLength(0);
});
