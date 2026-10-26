import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { SavedAnalysisPanel } from "../components/SavedAnalysisPanel";
import { SavedAnalysisDraftProvider } from "../SavedAnalysisDrafts";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const binding = "a".repeat(64), response = "b".repeat(64);
const source = { trial_id: "trial-one", binding_sha256: binding, response_sha256: response, model: "claude-fixture", origin: "PUBLIC_EXCERPT", title: "Public paper", page: 2, start: 10, end: 27, excerpt: "Preserved excerpt", captured_at: "2026-10-11T00:00:00Z" };
const captured = { analysis_id: "analysis_capture", topic_id: "a", saved_at: "2026-10-11T00:01:00Z", bundle: { schema_version: "SAVED_ANTHROPIC_TRIAL_ANALYSIS_V1", binding_sha256: binding, plan: { contract: { model: source.model }, grant: { origin: source.origin, source: { title: source.title, page: source.page, start: source.start, end: source.end, excerpt: source.excerpt }, trial_id: source.trial_id, actor_id: "local-user" } }, response_sha256: response, request_body_sha256: "c".repeat(64), captured_at: source.captured_at }, receipt: { receipt_id: "receipt-capture", topic_id: "original-topic", paper_id: "captured-paper", run_id: "original-run", input_origin: "LOCAL_APP_CAPTURE", status: "needs_review", packet: { input_scope: {}, evidence_spans: [{ span_id: "e1", text: source.excerpt, locator: { page: 2 } }] }, draft: { summary: "An interpretation", cited_evidence_span_ids: ["e1"] }, validation: {} }, latest_decision: null, decisions: [] };
const manual = { ...captured, analysis_id: "analysis_manual", bundle: { ...captured.bundle, schema_version: "MANUAL_EXTERNAL_RESPONSE_V1" }, receipt: { ...captured.receipt, paper_id: "manual-paper", input_origin: "MANUAL_IMPORT_UNVERIFIED" } };
const list = { items: [{ analysis_id: manual.analysis_id, paper_id: manual.receipt.paper_id, model: source.model, saved_at: manual.saved_at, latest_decision: null }], page: 1, limit: 10, total: 1 };
const sourceUrl = "/api/topics/a/saved-analyses/sources/anthropic-trial";
function Session({ topic = "a" }: { topic?: string }) { return <SavedAnalysisDraftProvider><SavedAnalysisPanel topicId={topic}/></SavedAnalysisDraftProvider>; }
const posts = () => vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST");
async function history() { fireEvent.click(screen.getByText("저장된 분석 결과")); await screen.findByRole("button", { name: "분석 열기 · manual-paper" }); }
async function inspect() { fireEvent.click(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" })); await screen.findByRole("button", { name: "이 주제에 분석 저장" }); }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
  if (url === sourceUrl) return (options?.method === "POST" ? captured : { available: true, reason: null, source }) as never;
  if (url.endsWith(manual.analysis_id)) return manual as never;
  if (url.endsWith(captured.analysis_id)) return captured as never;
  return list as never;
}); });

it("checks the preserved response only on explicit request and saves its frozen binding without a file or model call", async () => {
  render(<Session/>); expect(api.fetchJson).not.toHaveBeenCalled(); await history();
  expect(vi.mocked(api.fetchJson).mock.calls.some(([url]) => url === sourceUrl)).toBe(false);
  await inspect(); expect(screen.getByText("요청 모델: claude-fixture")).toBeTruthy();
  expect(screen.getByText(/공개 발췌/)).toBeTruthy(); expect(screen.getByText(/10–27/)).toBeTruthy();
  expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "이 주제에 분석 저장" }));
  await screen.findByRole("region", { name: "저장된 분석 상세" });
  expect(posts()).toHaveLength(1);
  expect(posts()[0]).toEqual([sourceUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-XRML-Analysis-Save": "1" }, body: JSON.stringify({ expected_binding_sha256: binding, expected_response_sha256: response }) }]);
  expect(screen.getByText(/캡처 시각 \(앱 보존 기록\)/)).toBeTruthy();
  expect(screen.queryByText(/캡처 시각 \(반입 파일이 제공한 값\)/)).toBeNull();
});

it.each(["anthropic_trial_disabled", "anthropic_trial_analysis_unavailable"])("shows unavailable reason %s without a save action", async reason => {
  vi.mocked(api.fetchJson).mockImplementation(async url => (url === sourceUrl ? { available: false, reason } : list) as never);
  render(<Session/>); await history(); fireEvent.click(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" }));
  await screen.findByText(reason === "anthropic_trial_disabled" ? /이 서버에는 확인할 앱 실행 기록이 연결되지 않았습니다/ : /앱에 보존된 성공 응답을 아직 저장할 수 없습니다/);
  expect(screen.queryByRole("button", { name: "이 주제에 분석 저장" })).toBeNull(); expect(posts()).toHaveLength(0);
});

it.each([403, 500])("reports read failure %s as an error, preserving existing analysis and notes", async status => {
  render(<Session/>); await history(); fireEvent.click(screen.getByRole("button", { name: "분석 열기 · manual-paper" }));
  fireEvent.change(await screen.findByLabelText("분석 검토 메모"), { target: { value: "Keep unsaved note" } });
  vi.mocked(api.fetchJson).mockRejectedValue(new api.ApiError("private error detail", status, "private detail"));
  fireEvent.click(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" })); await screen.findByRole("alert");
  expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("value", "Keep unsaved note");
  if (status === 403) expect(screen.getByRole("alert").textContent).toMatch(/로컬.*앱/);
  expect(screen.queryByText(/private detail/)).toBeNull(); expect(screen.queryByRole("button", { name: "이 주제에 분석 저장" })).toBeNull();
});

it("rejects malformed source responses and cannot save their values", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => (url === sourceUrl ? { available: true, reason: null, source: { ...source, binding_sha256: "invalid" } } : list) as never);
  render(<Session/>); await history(); fireEvent.click(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" }));
  await screen.findByRole("alert"); expect(screen.queryByRole("button", { name: "이 주제에 분석 저장" })).toBeNull();
});

it("keeps the exact two hashes after a lost response and never retries automatically", async () => {
  let count = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url === sourceUrl) { if (options?.method === "POST") { if (++count === 1) throw new Error("lost"); return captured as never; } return { available: true, reason: null, source } as never; }
    return (url.endsWith(captured.analysis_id) ? captured : list) as never;
  });
  render(<Session/>); await history(); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 주제에 분석 저장" }));
  await screen.findByRole("alert"); expect(posts()).toHaveLength(1);
  expect(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 응답 저장 다시 확인" }));
  await screen.findByRole("region", { name: "저장된 분석 상세" }); expect(posts()).toHaveLength(2); expect(posts()[0][1]).toEqual(posts()[1][1]);
});

it("discards a conflicted save and requires an explicit source recheck", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url === sourceUrl) { if (options?.method === "POST") throw new api.ApiError("changed", 409, "changed"); return { available: true, reason: null, source } as never; }
    return list as never;
  });
  render(<Session/>); await history(); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 주제에 분석 저장" }));
  await screen.findByText(/확인한 실행 또는 응답이 달라졌습니다/);
  expect(screen.queryByRole("button", { name: "같은 응답 저장 다시 확인" })).toBeNull(); expect(screen.queryByRole("button", { name: "이 주제에 분석 저장" })).toBeNull();
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([url]) => url === sourceUrl)).toHaveLength(2);
  await inspect(); expect(posts()).toHaveLength(1);
});

it.each(["binding", "response", "topic"])("keeps an unacknowledged save pending when the returned %s differs", async field => {
  const wrong = { ...captured, topic_id: field === "topic" ? "other" : "a", bundle: { ...captured.bundle, binding_sha256: field === "binding" ? "d".repeat(64) : binding, response_sha256: field === "response" ? "d".repeat(64) : response } };
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => (url === sourceUrl ? options?.method === "POST" ? wrong : { available: true, reason: null, source } : list) as never);
  render(<Session/>); await history(); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 주제에 분석 저장" }));
  await screen.findByRole("alert"); expect(screen.getByRole("button", { name: "같은 응답 저장 다시 확인" })).toBeTruthy(); expect(screen.queryByRole("region", { name: "저장된 분석 상세" })).toBeNull();
});

it("locks selection and import while saving, then preserves notes when the previous analysis is reopened", async () => {
  let finish!: (value: unknown) => void;
  const pending = new Promise(resolve => { finish = resolve; });
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (url === sourceUrl) return (options?.method === "POST" ? await pending : { available: true, reason: null, source }) as never;
    if (url.endsWith(manual.analysis_id)) return manual as never; if (url.endsWith(captured.analysis_id)) return captured as never; return list as never;
  });
  render(<Session/>); await history(); fireEvent.click(screen.getByRole("button", { name: "분석 열기 · manual-paper" }));
  fireEvent.change(await screen.findByLabelText("분석 검토 메모"), { target: { value: "Do not lose me" } });
  await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 주제에 분석 저장" }));
  expect(screen.getByRole("button", { name: "분석 열기 · manual-paper" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("분석 응답 묶음 파일")).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "분석 이력 새로고침" })).toHaveProperty("disabled", true);
  await act(async () => finish(captured)); await screen.findByText(/논문: captured-paper/);
  fireEvent.click(screen.getByRole("button", { name: "분석 열기 · manual-paper" }));
  await waitFor(() => expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("value", "Do not lose me"));
});

it("ignores late save results after the topic changes", async () => {
  let finish!: (value: unknown) => void;
  const pending = new Promise(resolve => { finish = resolve; });
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => (url === sourceUrl ? options?.method === "POST" ? await pending : { available: true, reason: null, source } : list) as never);
  const view = render(<Session/>); await history(); await inspect(); fireEvent.click(screen.getByRole("button", { name: "이 주제에 분석 저장" }));
  view.rerender(<Session topic="b"/>); await history(); await act(async () => finish(captured));
  expect(screen.queryByRole("region", { name: "저장된 분석 상세" })).toBeNull();
  expect(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" })).toHaveProperty("disabled", false);
});

it("can cancel an in-flight inspection and ignores its later response", async () => {
  let finish!: (value: unknown) => void;
  const pending = new Promise(resolve => { finish = resolve; });
  vi.mocked(api.fetchJson).mockImplementation(async url => (url === sourceUrl ? await pending : list) as never);
  render(<Session/>); await history(); fireEvent.click(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" }));
  fireEvent.click(screen.getByRole("button", { name: "응답 확인 취소" })); await act(async () => finish({ available: true, reason: null, source }));
  expect(screen.queryByRole("button", { name: "이 주제에 분석 저장" })).toBeNull();
  expect(screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" })).toHaveProperty("disabled", false);
});

it("supports keyboard inspect and save, and marks synthetic captures in the reader", async () => {
  const syntheticSource = { ...source, origin: "FIXTURE_SYNTHETIC" };
  const syntheticRecord = { ...captured, bundle: { ...captured.bundle, plan: { ...captured.bundle.plan, grant: { ...captured.bundle.plan.grant, origin: "FIXTURE_SYNTHETIC" } } }, receipt: { ...captured.receipt, input_origin: "FIXTURE_SYNTHETIC_CAPTURE" } };
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => (url === sourceUrl ? options?.method === "POST" ? syntheticRecord : { available: true, reason: null, source: syntheticSource } : url.endsWith(captured.analysis_id) ? syntheticRecord : list) as never);
  const user = userEvent.setup(); render(<Session/>); await history();
  screen.getByRole("button", { name: "앱에 보존된 성공 응답 확인" }).focus(); await user.keyboard("{Enter}");
  const save = await screen.findByRole("button", { name: "이 주제에 분석 저장" }); save.focus(); await user.keyboard(" ");
  const reader = await screen.findByRole("region", { name: "저장된 분석 상세" }); expect(within(reader).getByText(/합성 실행의 앱 보존 사본/)).toBeTruthy();
  expect(within(reader).queryByText(/반입 파일이 제공한 값/)).toBeNull();
});
