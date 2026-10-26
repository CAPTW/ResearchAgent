import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewPreparation } from "../components/ReviewPreparation";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const body = "a".repeat(8000) + "e\u0301🙂";
const source = { run_id: "run", paper_id: "paper", source_digest: "a".repeat(64), source_chars: 8003, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 8007, extraction_truncation: "NOT_OBSERVED", artifact_ref: "run:raw/paper.txt" };
const sources = { sources: [source], limits: { max_selected_chars: 12000 }, state: "NOT_EXECUTED" };
const record = { schema_version: "PAPER_REVIEW_PREPARATION_V1", preparation_id: "prep-one", topic_id: "topic", paper_version_id: "version", actor_id: "owner", created_at: "2026-10-02T00:00:00Z", state: "PREPARED_NOT_EXECUTED", source, selection: { start: 0, end: 8000, chars: 8000, text: body.slice(0, 8000), sha256: "c".repeat(64) }, execution: { started: false, full_document_review: "NOT_ESTABLISHED" } };
const history = { items: [record], hasMore: false, page: 1, pageSize: 10 };
const coverage = { schema_version: "PAPER_REVIEW_SOURCE_COVERAGE_V1", anchor_preparation_id: "prep-one", source, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT", prepared: { record_count: 1, covered_chars: 8000, ranges: [{ start: 0, end: 8000 }], gaps: [{ start: 8000, end: 8003 }], records: [{ preparation_id: "prep-one", start: 0, end: 8000 }] }, inputs: [], unmapped_receipt_count: 0, other_source_receipt_count: 0, full_document_review: "NOT_ESTABLISHED", scientific_support: "NOT_ASSESSED" };
function preview(start: number, length: number) {
  const text = Array.from(body).slice(start, start + length).join("");
  return { schema_version: "PAPER_REVIEW_PREVIEW_V1", source, window: { start, end: start + Array.from(text).length, text, chars: Array.from(text).length }, normalization: "XRML_WHITESPACE_V1", decoding: "UTF8_STRICT", state: "NOT_EXECUTED" };
}
function response(path: string, init?: RequestInit): unknown {
  if (path.endsWith("/preview")) { const request = JSON.parse(String(init?.body)); return preview(request.start, request.length); }
  if (path.endsWith("/sources")) return sources;
  if (path.endsWith("/coverage")) return coverage;
  if (path.includes("?")) return history;
  return record;
}
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockImplementation(async (path, init) => response(path, init) as never); });
const writes = () => vi.mocked(api.fetchJson).mock.calls.filter(([path, init]) => init?.method === "POST" && !path.endsWith("/preview"));
const previews = () => vi.mocked(api.fetchJson).mock.calls.filter(([path]) => path.endsWith("/preview"));
function setRange(start: string, end: string) {
  fireEvent.change(screen.getByLabelText("시작 위치", { exact: true }), { target: { value: start } });
  fireEvent.change(screen.getByLabelText("끝 위치 (포함하지 않음)"), { target: { value: end } });
}
function expectRange(start: string, end: string) {
  expect(screen.getByLabelText("시작 위치", { exact: true })).toHaveProperty("value", start);
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", end);
}
async function open() {
  const view = render(<ReviewPreparation topicId="topic" paperVersionId="version"/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  await screen.findByRole("combobox", { name: "저장 본문 출처" }); setRange("10", "20");
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · prep-one" }));
  fireEvent.click(await screen.findByText("보존된 출처의 범위 요약"));
  await screen.findByText("준비 기록 1건 · 중복 제외 8000자");
  return view;
}
async function gap() {
  fireEvent.click(await screen.findByRole("button", { name: "선택한 미준비 구간 열기" }));
  return screen.findByRole("region", { name: "미준비 구간 이어서 준비" });
}
async function readGap() {
  const region = await gap();
  const read = within(region).getByRole("button", { name: "본문 미리보기" });
  read.focus(); await userEvent.setup().keyboard("{Enter}");
  const textarea = await within(region).findByRole("textbox", { name: "저장 본문 미리보기" }) as HTMLTextAreaElement;
  textarea.focus(); textarea.setSelectionRange(0, textarea.value.length); fireEvent.select(textarea);
  return region;
}

it("offers a remaining range without replacing manual input and applies only an explicit Unicode selection", async () => {
  await open(); expect(previews()).toHaveLength(0);
  const region = await readGap();
  expectRange("10", "20");
  expect(within(region).getByRole("textbox", { name: "저장 본문 미리보기" })).toHaveProperty("value", "e\u0301🙂");
  expect(JSON.parse(String(previews()[0][1]?.body))).toEqual({ run_id: "run", paper_id: "paper", expected_source_digest: source.source_digest, start: 8000, length: 3 });
  const apply = within(region).getByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" });
  apply.focus(); await userEvent.setup().keyboard("{Enter}");
  expectRange("8000", "8003"); expect(writes()).toHaveLength(0);
});

it.each([404, 409])("preserves usable current input after a historical source %s and never presents a successful replacement", async status => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/preview")) throw new api.ApiError("review_preparation_source_changed", status, "{}");
    return response(path, init) as never;
  });
  await open(); const region = await gap();
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기" }));
  expect((await within(region).findByRole("alert")).textContent).toContain("source_changed");
  expectRange("10", "20");
  expect(screen.getByLabelText("시작 위치", { exact: true }).matches(":disabled")).toBe(false);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", false);
  expect(within(region).queryByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" })).toBeNull();
  expect(writes()).toHaveLength(0);
});

it("rejects explicit application against a different current source observation without a false success notice", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => path.endsWith("/sources") ? { ...sources, sources: [{ ...source, source_digest: "d".repeat(64) }] } as never : response(path, init) as never);
  await open(); const region = await readGap();
  fireEvent.click(within(region).getByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" }));
  expect(within(region).getByRole("alert").textContent).toContain("현재 출처 목록과 미리보기 출처가 일치하지 않습니다");
  expect(within(region).queryByText(/고정할 입력 위치에 반영했습니다/)).toBeNull();
  expectRange("10", "20"); expect(writes()).toHaveLength(0);
});

it("preserves an unresolved save request even after a gap was selected and retries the identical body", async () => {
  let attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST" && !path.endsWith("/preview") && ++attempts === 1) throw new Error("response lost");
    return response(path, init) as never;
  });
  await open(); const region = await readGap();
  fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" }));
  await screen.findByText(/구간 고정 오류: response lost/);
  expect(within(region).getByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" })).toHaveProperty("disabled", true);
  fireEvent.click(within(region).getByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" }));
  expectRange("10", "20"); expect(writes()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "같은 고정 요청 다시 보내기" }));
  await screen.findByText("구간을 고정했습니다. 모델을 실행하지 않았으며 저장된 기록을 다시 읽습니다.");
  expect(writes()).toHaveLength(2); expect(writes()[1]).toEqual(writes()[0]);
});

it.each(["cancel", "close", "refresh", "topic"])("invalidates a delayed gap read on %s while preserving input", async action => {
  let resolve!: (value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation((path, init) => path.endsWith("/preview") ? new Promise(done => { resolve = done; }) as never : Promise.resolve(response(path, init)) as never);
  const view = await open(); const region = await gap();
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기" }));
  await within(region).findByText("본문 미리보기 읽는 중");
  expect(screen.getByLabelText("시작 위치", { exact: true }).matches(":disabled")).toBe(true);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  const signal = previews()[0][1]?.signal as AbortSignal;
  if (action === "cancel") fireEvent.click(within(region).getByRole("button", { name: "미리보기 요청 취소" }));
  if (action === "close") fireEvent.click(within(region).getByRole("button", { name: "미준비 구간 닫기" }));
  if (action === "refresh") fireEvent.click(screen.getByRole("button", { name: "범위 요약 새로고침" }));
  if (action === "topic") view.rerender(<ReviewPreparation topicId="other-topic" paperVersionId="version"/>);
  expect(signal.aborted).toBe(true);
  await act(async () => resolve(preview(8000, 3)));
  expect(screen.queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  if (action !== "topic") { expectRange("10", "20"); expect(screen.getByLabelText("시작 위치", { exact: true }).matches(":disabled")).toBe(false); }
  else expect(screen.queryByLabelText("시작 위치", { exact: true })).toBeNull();
  expect(writes()).toHaveLength(0);
});

it("bounds all windows and jump controls to the selected gap, not the full source", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => path.endsWith("/coverage") ? { ...coverage, prepared: { ...coverage.prepared, gaps: [{ start: 100, end: 7501 }] } } as never : response(path, init) as never);
  await open(); const region = await readGap();
  expect(within(region).getByRole("button", { name: "이전 본문 구간" })).toHaveProperty("disabled", true);
  fireEvent.change(within(region).getByLabelText("미리보기 시작 위치"), { target: { value: "99" } });
  expect(within(region).getByRole("button", { name: "이 위치 미리보기" })).toHaveProperty("disabled", true);
  fireEvent.click(within(region).getByRole("button", { name: "다음 본문 구간" }));
  await within(region).findByText(/미리보기 구간: 4100–7501/);
  expect(within(region).getByRole("button", { name: "다음 본문 구간" })).toHaveProperty("disabled", true);
  fireEvent.change(within(region).getByLabelText("미리보기 시작 위치"), { target: { value: "7501" } });
  expect(within(region).getByRole("button", { name: "이 위치 미리보기" })).toHaveProperty("disabled", true);
  expect(previews().map(([, init]) => { const request = JSON.parse(String(init?.body)); return [request.start, request.length]; })).toEqual([[100, 4000], [4100, 3401]]);
  expectRange("10", "20"); expect(writes()).toHaveLength(0);
});

it("switches an explicitly chosen source only on apply and clears the former gap selection on a different gap", async () => {
  const other = { ...source, run_id: "run-two", source_digest: "f".repeat(64) };
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/sources")) return { ...sources, sources: [source, other] } as never;
    if (path.endsWith("/coverage")) return { ...coverage, source: other, prepared: { ...coverage.prepared, gaps: [{ start: 0, end: 3 }, { start: 8000, end: 8003 }] } } as never;
    if (path.endsWith("/preview")) { const request = JSON.parse(String(init?.body)); return { ...preview(request.start, request.length), source: other } as never; }
    return response(path, init) as never;
  });
  await open(); const first = await readGap();
  expect(screen.getByLabelText("저장 본문 출처")).toHaveProperty("value", JSON.stringify([source.run_id, source.paper_id]));
  fireEvent.change(screen.getByLabelText("살펴볼 미준비 구간"), { target: { value: "8000:8003" } });
  expect(within(first).getByText(/선택한 미준비 범위 0–3/)).toBeTruthy();
  const region = await gap();
  expect(within(region).queryByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" })).toBeNull();
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기" }));
  const textarea = await within(region).findByRole("textbox", { name: "저장 본문 미리보기" }) as HTMLTextAreaElement;
  textarea.focus(); textarea.setSelectionRange(0, textarea.value.length); fireEvent.select(textarea);
  fireEvent.click(within(region).getByRole("button", { name: "선택한 본문으로 입력 출처·범위 바꾸기" }));
  expect(screen.getByLabelText("저장 본문 출처")).toHaveProperty("value", JSON.stringify([other.run_id, other.paper_id]));
  expectRange("8000", "8003"); expect(writes()).toHaveLength(0);
});

it("locks main and gap reads in both directions and releases each lock on cancellation", async () => {
  const pending: Array<() => void> = [];
  vi.mocked(api.fetchJson).mockImplementation((path, init) => {
    if (path.endsWith("/preview")) { const request = JSON.parse(String(init?.body)); return new Promise(done => pending.push(() => done(preview(request.start, request.length)))) as never; }
    return Promise.resolve(response(path, init)) as never;
  });
  await open(); const main = screen.getByRole("region", { name: "저장 본문 구간 미리보기" }); const region = await gap();
  fireEvent.click(within(main).getByRole("button", { name: "본문 미리보기" }));
  expect(within(region).getByRole("button", { name: "본문 미리보기" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  fireEvent.click(within(main).getByRole("button", { name: "미리보기 요청 취소" }));
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기" }));
  expect(within(main).getByRole("button", { name: "본문 미리보기" })).toHaveProperty("disabled", true);
  fireEvent.click(within(region).getByRole("button", { name: "미리보기 요청 취소" }));
  await act(async () => pending.forEach(resolve => resolve()));
  expect(within(main).getByRole("button", { name: "본문 미리보기" })).toHaveProperty("disabled", false);
  expectRange("10", "20"); expect(previews()).toHaveLength(2); expect(writes()).toHaveLength(0);
});

it("rejects a response extending outside the gap even when still within the source", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/coverage")) return { ...coverage, prepared: { ...coverage.prepared, gaps: [{ start: 100, end: 103 }] } } as never;
    if (path.endsWith("/preview")) return preview(100, 4) as never;
    return response(path, init) as never;
  });
  await open(); const region = await gap();
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기" }));
  expect((await within(region).findByRole("alert")).textContent).toContain("요청한 출처·구간과 미리보기 응답이 일치하지 않습니다");
  expect(within(region).queryByRole("textbox", { name: "저장 본문 미리보기" })).toBeNull();
  expectRange("10", "20"); expect(writes()).toHaveLength(0);
});

it("retries a failed gap window without applying text automatically", async () => {
  let calls = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/preview") && ++calls === 1) throw new Error("preview unavailable");
    return response(path, init) as never;
  });
  await open(); const region = await gap();
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기" }));
  await within(region).findByRole("alert");
  fireEvent.click(within(region).getByRole("button", { name: "본문 미리보기 다시 시도" }));
  expect(await within(region).findByRole("textbox", { name: "저장 본문 미리보기" })).toHaveProperty("value", "e\u0301🙂");
  expect(previews()[1][1]?.body).toEqual(previews()[0][1]?.body);
  expectRange("10", "20"); expect(writes()).toHaveLength(0);
});

it("keeps whole-paper review unestablished when the stored source has no preparation gaps", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => path.endsWith("/coverage") ? { ...coverage, prepared: { ...coverage.prepared, gaps: [] } } as never : response(path, init) as never);
  await open();
  expect(screen.getByText(/이 저장 출처에는 미준비 구간이 없습니다. 전체 논문 검토 완료를 뜻하지 않습니다/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "선택한 미준비 구간 열기" })).toBeNull();
  expectRange("10", "20"); expect(writes()).toHaveLength(0);
});
