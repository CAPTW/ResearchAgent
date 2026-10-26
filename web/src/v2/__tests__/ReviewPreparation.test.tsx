import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { ReviewPreparation } from "../components/ReviewPreparation";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const base = "/api/topics/topic%20one/paper-bank/version%2Fone/review-preparations";
const source = { run_id: "run-1", paper_id: "paper-1", source_digest: "a".repeat(64), source_chars: 20000, source_kind: "text", artifact_sha256: "b".repeat(64), artifact_bytes: 40000, extraction_truncation: "TRUNCATED" };
const sources = { sources: [source], limits: { max_selected_chars: 12000 }, state: "NOT_EXECUTED" };
const record = { schema_version: "PAPER_REVIEW_PREPARATION_V1", preparation_id: "preparation-one", topic_id: "topic one", paper_version_id: "version/one", actor_id: "owner", created_at: "2026-09-28T09:00:00Z", state: "PREPARED_NOT_EXECUTED", source: { ...source, artifact_ref: "run:raw/paper.txt" }, selection: { start: 12000, end: 12003, text: "e\u0301🙂", chars: 3, sha256: "c".repeat(64) }, execution: { started: false, full_document_review: "NOT_ESTABLISHED" } };
const summary = { ...record, selection: { start: 12000, end: 12003, chars: 3, sha256: "c".repeat(64) } };
const listing = { items: [summary], hasMore: false, page: 1, pageSize: 10 };
const offlineResult = { execution_id: "offline-one", preparation_id: record.preparation_id, receipt_id: "receipt-one", actor_id: "owner", created_at: record.created_at, state: "RESULT_SAVED", mode: "OFFLINE_SYNTHETIC", adapter_id: "xrml_offline_review", adapter_version: "1" };
const capability = { enabled: true, mode: "OFFLINE_SYNTHETIC", adapter_id: "xrml_offline_review", adapter_version: "1", reason: null, preparation_digest: "e".repeat(64), latest: null };
const savedContext = { schema_version: "REVIEW_CONTEXT_V1", topic_title: "Saved topic title", topic_name: "Saved XR research question", paper_title: "Saved paper title", authors: ["Saved Author", "Second Author"], published_at: "2026-08-01", metadata_digest: "f".repeat(64), topic_updated_at: "2026-09-27T09:00:00Z" };
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => (init?.method === "POST" ? record : path.endsWith("/sources") ? sources : path.includes("?") ? listing : record) as never);
});
async function open() {
  const view = render(<ReviewPreparation topicId="topic one" paperVersionId="version/one"/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  await screen.findByRole("combobox", { name: "저장 본문 출처" });
  return view;
}
function range(start: string, end: string) {
  fireEvent.change(screen.getByLabelText("시작 위치"), { target: { value: start } });
  fireEvent.change(screen.getByLabelText("끝 위치 (포함하지 않음)"), { target: { value: end } });
}
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");

it("reads frozen V2 research context alongside the exact selected body when current sources are unavailable", async () => {
  const saved = { ...record, schema_version: "PAPER_REVIEW_PREPARATION_V2", context: savedContext };
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/sources")) throw new Error("current source unavailable");
    return (path.includes("?") ? { ...listing, items: [{ ...summary, schema_version: saved.schema_version, context: savedContext }] } : saved) as never;
  });
  render(<ReviewPreparation topicId="topic one" paperVersionId="version/one"/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  const context = await screen.findByRole("region", { name: "보존된 연구 맥락" });
  expect(within(context).getByText("저장된 주제 제목: Saved topic title")).toBeTruthy();
  expect(within(context).getByText("연구 주제: Saved XR research question")).toBeTruthy();
  expect(within(context).getByText("논문 제목: Saved paper title")).toBeTruthy();
  expect(within(context).getByText("저자: Saved Author, Second Author")).toBeTruthy();
  expect(within(context).getByText("발행일: 2026-08-01")).toBeTruthy();
  expect(within(context).getByText(/추가로 검토한 본문이나 근거가 아닙니다/)).toBeTruthy();
  expect(within(context).getByText(`논문 메타데이터 식별값: ${savedContext.metadata_digest}`)).toBeTruthy();
  expect(screen.getByLabelText("보존된 준비 본문").textContent).toBe(record.selection.text);
  expect(vi.mocked(api.fetchJson).mock.calls.every(([path]) => path.startsWith(base))).toBe(true);
  expect(posts()).toHaveLength(0);
});

it("keeps a V1 preparation readable and explicitly identifies absent saved research context", async () => {
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  await screen.findByLabelText("보존된 준비 본문");
  expect(screen.getByText("별도의 준비 맥락 기록 없음 · 이 기록에는 새 준비 맥락 항목이 없습니다. 현재 메타데이터로 채우지 않습니다.")).toBeTruthy();
  expect(screen.getByText(/현재 메타데이터로 채우지 않습니다/)).toBeTruthy();
  expect(screen.queryByText(/저장된 주제 제목:/)).toBeNull();
  expect(screen.getByLabelText("보존된 준비 본문").textContent).toBe(record.selection.text);
  expect(posts()).toHaveLength(0);
});

it("shows unavailable occurrences while keeping a healthy source selectable", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/sources") ? {
    ...sources, unavailable_sources: [{ diagnostic_id: "unavailable-one", run_id: "old-run", reason: "review_preparation_source_changed" }],
  } : listing) as never);
  await open();
  const warning = await screen.findByRole("region", { name: "사용할 수 없는 저장 본문 출처" });
  expect(within(warning).getByText(/old-run/)).toBeTruthy();
  expect(within(warning).getByText(/보존된 추출 기록과 현재 파일이 일치하지 않습니다/)).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "저장 본문 출처" })).toHaveProperty("value", JSON.stringify([source.run_id, source.paper_id]));
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", false);
  expect(posts()).toHaveLength(0);
});

it("identifies each unavailable PDF extraction separately from the legacy source in the same run", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/sources") ? {
    ...sources, unavailable_sources: [
      { diagnostic_id: "legacy-source", run_id: source.run_id, reason: "review_preparation_not_found" },
      { diagnostic_id: "extraction-one-source", run_id: source.run_id, extraction_id: "pdfx-one", reason: "review_preparation_source_changed" },
      { diagnostic_id: "extraction-two-source", run_id: source.run_id, extraction_id: "pdfx-two", reason: "review_preparation_source_invalid" },
    ],
  } : listing) as never);
  await open();
  const warning = await screen.findByRole("region", { name: "사용할 수 없는 저장 본문 출처" });
  const entries = within(warning).getAllByRole("listitem"); expect(entries).toHaveLength(3);
  expect(within(entries[0]).queryByText(/PDF 추출 기록:/)).toBeNull();
  expect(within(entries[1]).getByText("PDF 추출 기록: pdfx-one")).toBeTruthy();
  expect(within(entries[2]).getByText("PDF 추출 기록: pdfx-two")).toBeTruthy();
  expect(within(entries[1]).getByText(/보존된 추출 기록과 현재 파일이 일치하지 않습니다/)).toBeTruthy();
  expect(within(entries[2]).getByText(/형식·인코딩 또는 안전한 파일 경로를 확인할 수 없습니다/)).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "저장 본문 출처" })).toHaveProperty("value", JSON.stringify([source.run_id, source.paper_id]));
  expect(posts()).toHaveLength(0);
});

it("preserves coordinates when the selected source becomes unavailable and requires an explicit replacement", async () => {
  const other = { ...source, run_id: "run-2" };
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/sources") ? ++reads === 1 ? sources : {
    ...sources, sources: [other], unavailable_sources: [{ diagnostic_id: "unavailable-one", run_id: source.run_id, reason: "review_preparation_not_found" }],
  } : listing) as never);
  await open(); range("100", "120");
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText(/선택했던 출처를 현재 사용할 수 없습니다/);
  expect(screen.getByRole("combobox", { name: "저장 본문 출처" })).toHaveProperty("value", "");
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "120");
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  await userEvent.setup().selectOptions(screen.getByRole("combobox", { name: "저장 본문 출처" }), JSON.stringify([other.run_id, other.paper_id]));
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", false);
  expect(screen.queryByText(/선택했던 출처를 현재 사용할 수 없습니다/)).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("keeps history readable when all sources are unavailable and compares the restored selection to its last digest", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => (path.endsWith("/sources") ? ++reads === 1 ? sources : [2, 4].includes(reads) ? {
    ...sources, sources: [], unavailable_sources: [{ diagnostic_id: "unavailable-one", run_id: source.run_id, reason: "review_preparation_source_invalid" }],
  } : { ...sources, sources: [{ ...source, source_digest: "d".repeat(64) }], unavailable_sources: [] }
    : path.includes("?") ? listing : record) as never);
  await open(); range("100", "120");
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText(/준비할 수 있는 저장 본문이 없습니다/);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100");
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  await screen.findByLabelText("보존된 준비 본문");
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText(/출처가 변경되어 숫자 위치만 유지했습니다/);
  expect(screen.queryByRole("region", { name: "사용할 수 없는 저장 본문 출처" })).toBeNull();
  expect(screen.queryByText(/선택했던 출처를 현재 사용할 수 없습니다/)).toBeNull();
  // Becoming unavailable again must not acknowledge an earlier changed digest.
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText(/선택했던 출처를 현재 사용할 수 없습니다/);
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByText(/출처가 변경되어 숫자 위치만 유지했습니다/);
  expect(screen.queryByText(/선택했던 출처를 현재 사용할 수 없습니다/)).toBeNull();
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "100");
  expect(screen.getByLabelText("보존된 준비 본문").textContent).toBe(record.selection.text);
  expect(posts()).toHaveLength(0);
});

it("offers a saved-source range overview from a preserved preparation without loading it eagerly", async () => {
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  await screen.findByLabelText("보존된 준비 본문");
  expect(screen.getByText("보존된 출처의 범위 요약")).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.some(([path]) => path.endsWith("/coverage"))).toBe(false);
});

it("loads lazily, freezes an explicit tail range by keyboard and reads stored codepoint text without executing", async () => {
  await open();
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "0");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "6000");
  expect(screen.getByText(/Unicode 코드 포인트/)).toBeTruthy();
  expect(screen.getByText(/NUL·연속 공백을 공백 하나로 정리/)).toBeTruthy();
  expect(screen.queryByLabelText("보존된 준비 본문")).toBeNull();
  expect(posts()).toHaveLength(0);
  range("12000", "12003");
  const button = screen.getByRole("button", { name: "선택 구간 고정" });
  button.focus(); await userEvent.setup().keyboard("{Enter}");
  const saved = await screen.findByLabelText("보존된 준비 본문");
  expect(saved.textContent).toBe("e\u0301🙂");
  expect(screen.getByText("저장된 구간: 12000–12003 · 3자 (Unicode 코드 포인트)")).toBeTruthy();
  expect(screen.getByText("준비만 저장됨 · 모델 실행 안 함 · 전체 문서 검토 미확인")).toBeTruthy();
  expect(screen.getByText(/저장 시점의 정규화된 본문에서 고정한 선택 구간/)).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledWith(base + "/preparation-one");
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), run_id: "run-1", paper_id: "paper-1", expected_source_digest: source.source_digest, start: 12000, end: 12003 });
  expect(posts()).toHaveLength(1);
});

it.each([["network", undefined], ["408", 408]])("keeps the same exact request and coordinates after a %s lost save response", async (_label, status) => {
  let attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") { if (++attempts === 1) throw status === undefined ? new Error("response lost") : new api.ApiError("response lost", Number(status), "{}"); return record as never; }
    return (path.endsWith("/sources") ? sources : path.includes("?") ? listing : record) as never;
  });
  await open(); range("12000", "12003");
  fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" }));
  expect(await screen.findByText(/response lost/)).toBeTruthy();
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "12000");
  expect(screen.getByLabelText("시작 위치").matches(":disabled")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "같은 고정 요청 다시 보내기" }));
  await screen.findByLabelText("보존된 준비 본문");
  expect(posts()).toHaveLength(2);
  expect(posts()[1]).toEqual(posts()[0]);
});

it("requires explicit source refresh after conflict and preserves range coordinates", async () => {
  let reads = 0, attempts = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") { if (++attempts === 1) throw new api.ApiError("review_preparation_source_changed", 409, "{}"); return record as never; }
    if (path.endsWith("/sources")) return { ...sources, sources: [{ ...source, source_digest: ++reads === 1 ? source.source_digest : "d".repeat(64) }] } as never;
    return (path.includes("?") ? listing : record) as never;
  });
  await open(); range("12000", "12003");
  fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" }));
  await screen.findByText(/review_preparation_source_changed/);
  expect(screen.getByText(/출처 변경 오류가 계속되면/)).toBeTruthy();
  expect(reads).toBe(1);
  expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", false));
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "12000");
  expect(screen.getByLabelText("끝 위치 (포함하지 않음)")).toHaveProperty("value", "12003");
  fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" }));
  await screen.findByLabelText("보존된 준비 본문");
  const first = JSON.parse(posts()[0][1]!.body as string), second = JSON.parse(posts()[1][1]!.body as string);
  expect(second).toMatchObject({ expected_source_digest: "d".repeat(64), start: 12000, end: 12003 });
  expect(second.request_id).not.toBe(first.request_id);
});

it("keeps saved history readable when current source lookup fails and supports source retry", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/sources")) { if (++reads === 1) throw new Error("source missing"); return sources as never; }
    return (path.includes("?") ? listing : record) as never;
  });
  render(<ReviewPreparation topicId="topic one" paperVersionId="version/one"/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  expect(await screen.findByText(/source missing/)).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  await screen.findByLabelText("보존된 준비 본문");
  expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "저장 본문 출처 새로고침" }));
  await screen.findByRole("combobox", { name: "저장 본문 출처" });
  expect(screen.getByLabelText("보존된 준비 본문").textContent).toBe(record.selection.text);
});

it("rejects invalid or oversized ranges locally while preserving the entered values", async () => {
  await open();
  for (const [start, end] of [["0", "12001"], ["5", "5"], ["20000", "20001"], ["", "3"]]) {
    range(start, end);
    expect(screen.getByRole("button", { name: "선택 구간 고정" })).toHaveProperty("disabled", true);
    expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", start);
  }
  expect(posts()).toHaveLength(0);
});

it("retries history errors and paginates without hiding an open preserved record", async () => {
  let pageTwo = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/sources")) return sources as never;
    if (path.includes("page=2")) { if (++pageTwo === 1) throw new Error("history unavailable"); return { ...listing, page: 2, items: [] } as never; }
    return (path.includes("?") ? { ...listing, hasMore: true } : record) as never;
  });
  await open();
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  await screen.findByLabelText("보존된 준비 본문");
  fireEvent.click(screen.getByRole("button", { name: "다음 준비 기록 페이지" }));
  expect(await screen.findByText(/history unavailable/)).toBeTruthy();
  expect(screen.getByLabelText("보존된 준비 본문")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "준비 이력 새로고침" }));
  await screen.findByText("이 페이지에 저장된 준비 기록이 없습니다.");
  const pages = screen.getByRole("navigation", { name: "준비 이력 페이지" });
  expect(within(pages).getByText("2 페이지")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "이전 준비 기록 페이지" }));
  await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" });
});

it("discards an old save response when the topic or paper changes", async () => {
  let resolve!: (value: never) => void;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (init?.method === "POST") return new Promise<never>(value => { resolve = value; });
    return (path.endsWith("/sources") ? sources : { ...listing, items: [] }) as never;
  });
  const view = await open(); range("12000", "12003");
  fireEvent.click(screen.getByRole("button", { name: "선택 구간 고정" }));
  view.rerender(<ReviewPreparation topicId="different" paperVersionId="different"/>);
  expect(screen.queryByRole("combobox", { name: "저장 본문 출처" })).toBeNull();
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  await screen.findByRole("combobox", { name: "저장 본문 출처" });
  await act(async () => { resolve(record as never); });
  expect(screen.queryByLabelText("보존된 준비 본문")).toBeNull();
  expect(screen.queryByText(/구간을 고정했습니다/)).toBeNull();
  expect(screen.getByLabelText("시작 위치")).toHaveProperty("value", "0");
});

it("retries a preserved record read and downloads the exact stored JSON without a new preparation", async () => {
  let reads = 0;
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/sources")) return sources as never;
    if (path.includes("?")) return listing as never;
    if (++reads === 1) throw new Error("saved record unavailable");
    return record as never;
  });
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  const create = vi.fn((_blob: Blob) => "blob:local-preparation"), revoke = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revoke });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    expect(this.download).toBe("preparation-one.json");
    expect(this.href).toBe("blob:local-preparation");
  });
  try {
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
    await screen.findByText(/saved record unavailable/);
    expect(screen.queryByRole("button", { name: "준비 기록 JSON 내려받기" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "준비 기록 다시 읽기" }));
    await screen.findByLabelText("보존된 준비 본문");
    fireEvent.click(screen.getByRole("button", { name: "준비 기록 JSON 내려받기" }));
    expect(create).toHaveBeenCalledOnce();
    const blob = create.mock.calls[0][0] as Blob;
    const text = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsText(blob);
    });
    expect(JSON.parse(text)).toEqual(record);
    expect(click).toHaveBeenCalledOnce();
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:local-preparation"));
    expect(posts()).toHaveLength(0);
  } finally {
    click.mockRestore();
    if (originalCreate) Object.defineProperty(URL, "createObjectURL", originalCreate); else Reflect.deleteProperty(URL, "createObjectURL");
    if (originalRevoke) Object.defineProperty(URL, "revokeObjectURL", originalRevoke); else Reflect.deleteProperty(URL, "revokeObjectURL");
  }
});

it.each([["network", undefined], ["408", 408]])("requires an explicit synthetic action and retries the exact %s lost-response request", async (_label, status) => {
  let attempts = 0;
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/offline-review")) return capability as never;
    if (path.endsWith("/offline-reviews") && init?.method === "POST") {
      if (++attempts === 1) throw status === undefined ? new Error("synthetic response lost") : new api.ApiError("synthetic response lost", Number(status), "{}");
      return offlineResult as never;
    }
    return (path.endsWith("/sources") ? sources : path.includes("?") ? listing : record) as never;
  });
  render(<ReviewPreparation topicId="topic one" paperVersionId="version/one" onOpenReceipt={onOpenReceipt}/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  const run = await screen.findByRole("button", { name: "이 준비 구간으로 합성 검토 결과 만들기" });
  expect(posts()).toHaveLength(0);
  expect(onOpenReceipt).not.toHaveBeenCalled();
  run.focus(); await userEvent.setup().keyboard("{Enter}");
  await screen.findByText(/synthetic response lost/);
  expect(run).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 합성 검토 요청 다시 보내기" }));
  await waitFor(() => expect(onOpenReceipt).toHaveBeenCalledExactlyOnceWith("receipt-one"));
  expect(posts()).toHaveLength(2);
  expect(posts()[1]).toEqual(posts()[0]);
  expect(posts()[0][0]).toBe(`${base}/preparation-one/offline-reviews`);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ request_id: expect.any(String), expected_preparation_digest: capability.preparation_digest });
  expect(screen.getByText(/연구 품질·신규성·신뢰도는 미평가/)).toBeTruthy();
});

it("shows stored synthetic receipt in disabled mode and retries capability errors without execution", async () => {
  let reads = 0;
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async path => {
    if (path.endsWith("/offline-review")) {
      if (++reads === 1) throw new Error("capability unavailable");
      return { ...capability, enabled: false, reason: "review_offline_disabled", latest: offlineResult } as never;
    }
    return (path.endsWith("/sources") ? sources : path.includes("?") ? listing : record) as never;
  });
  render(<ReviewPreparation topicId="topic one" paperVersionId="version/one" onOpenReceipt={onOpenReceipt}/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  await screen.findByText(/capability unavailable/);
  expect(screen.queryByRole("button", { name: "이 준비 구간으로 합성 검토 결과 만들기" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "합성 검토 상태 새로고침" }));
  fireEvent.click(await screen.findByRole("button", { name: "저장된 합성 입력·결과 열기" }));
  expect(onOpenReceipt).toHaveBeenCalledExactlyOnceWith("receipt-one");
  expect(screen.queryByRole("button", { name: "이 준비 구간으로 합성 검토 결과 만들기" })).toBeNull();
  expect(posts()).toHaveLength(0);
});

it("ignores a synthetic completion after the paper or topic scope changes", async () => {
  let finish!: (value: unknown) => void;
  const onOpenReceipt = vi.fn();
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/offline-review")) return capability as never;
    if (init?.method === "POST") return new Promise(resolve => { finish = resolve; }) as never;
    return (path.endsWith("/sources") ? sources : path.includes("?") ? listing : record) as never;
  });
  const view = render(<ReviewPreparation topicId="topic one" paperVersionId="version/one" onOpenReceipt={onOpenReceipt}/>);
  fireEvent.click(screen.getByText("본문 구간 검토 준비"));
  fireEvent.click(await screen.findByRole("button", { name: "준비 기록 열기 · preparation-one" }));
  fireEvent.click(await screen.findByRole("button", { name: "이 준비 구간으로 합성 검토 결과 만들기" }));
  view.rerender(<ReviewPreparation topicId="other" paperVersionId="other" onOpenReceipt={onOpenReceipt}/>);
  await act(async () => finish(offlineResult));
  expect(onOpenReceipt).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "저장된 합성 입력·결과 열기" })).toBeNull();
});
