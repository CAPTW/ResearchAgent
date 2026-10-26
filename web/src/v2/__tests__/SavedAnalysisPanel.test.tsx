import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { SavedAnalysisPanel } from "../components/SavedAnalysisPanel";
import { SavedAnalysisDraftProvider } from "../SavedAnalysisDrafts";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const record = { analysis_id: "analysis_one", topic_id: "a", saved_at: "2026-10-11T00:00:00Z", bundle: { plan: { contract: { model: "claude-haiku-5-5" } }, response_sha256: "raw-hash", request_body_sha256: "body-hash" }, receipt: { receipt_id: "receipt_one", topic_id: "original-topic", run_id: "original-run", paper_id: "public-paper", input_origin: "MANUAL_IMPORT_UNVERIFIED", status: "needs_review", packet: { input_scope: {}, evidence_spans: [{ span_id: "cite-1", text: "Preserved e\u0301 evidence", locator: { page: 2 } }] }, draft: { summary: "Interpretation", cited_evidence_span_ids: ["cite-1"] }, validation: {} }, latest_decision: null, decisions: [] };
const list = { items: [{ analysis_id: record.analysis_id, paper_id: "public-paper", model: "claude-haiku-5-5", saved_at: record.saved_at, latest_decision: null }], page: 1, limit: 10, total: 1 };
function Session({ topic = "a" }: { topic?: string }) { return <SavedAnalysisDraftProvider><SavedAnalysisPanel key={topic} topicId={topic}/></SavedAnalysisDraftProvider>; }
async function open() { fireEvent.click(screen.getByText("저장된 분석 결과")); fireEvent.click(await screen.findByRole("button", { name: "분석 열기 · public-paper" })); await screen.findByLabelText("분석 검토 메모"); }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockImplementation(async url => (url.includes("analysis_one") ? record : list) as never); });

it("loads lazily and presents frozen evidence separately from interpretation and human review", async () => {
  render(<Session/>); expect(api.fetchJson).not.toHaveBeenCalled(); await open();
  expect(screen.getByText("Preserved e\u0301 evidence")).toBeTruthy();
  expect(screen.getByText(/원래 주제: original-topic/)).toBeTruthy();
  expect(screen.getByText(/과학적 검증이나 저널 채택을 뜻하지 않습니다/)).toBeTruthy();
  expect(screen.getByLabelText("분석 검토 상태")).toHaveProperty("value", "needs_review");
});

it("retains notes across topic changes and retries the identical lost-response command", async () => {
  let posts = 0; const commands: unknown[] = [];
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { const body = JSON.parse(options.body as string); commands.push(body); if (++posts === 1) throw new Error("lost"); const decision = { decision_id: "d1", state: body.state, note: body.note, command: body }; return { ...record, latest_decision: decision, decisions: [decision] } as never; }
    return (url.includes("analysis_one") ? record : list) as never;
  });
  const view = render(<Session/>); await open();
  fireEvent.change(screen.getByLabelText("분석 검토 메모"), { target: { value: "Original note" } });
  view.rerender(<Session topic="b"/>); view.rerender(<Session/>); await open();
  expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("value", "Original note");
  fireEvent.click(screen.getByRole("button", { name: "분석 검토 저장" })); await screen.findByRole("alert");
  expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "같은 검토 저장 재시도" }));
  await screen.findByText("검토를 새 이력으로 저장했습니다."); expect(commands).toHaveLength(2); expect(commands[0]).toEqual(commands[1]);
});

it("shows refresh failures without discarding evidence or unsaved notes", async () => {
  render(<Session/>); await open(); fireEvent.change(screen.getByLabelText("분석 검토 메모"), { target: { value: "Keep me" } });
  vi.mocked(api.fetchJson).mockRejectedValue(new Error("offline")); fireEvent.click(screen.getByRole("button", { name: "분석 이력 새로고침" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy()); expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("value", "Keep me");
  expect(screen.getByText("Preserved e\u0301 evidence")).toBeTruthy();
});

it("does not replace a newly opened analysis with the late result of another save", async () => {
  let finish!: (value: unknown) => void;
  let command: unknown;
  const pending = new Promise(resolve => { finish = resolve; });
  const other = { ...record, analysis_id: "analysis_two", receipt: { ...record.receipt, paper_id: "other-paper" } };
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { command = JSON.parse(options.body as string); return await pending as never; }
    if (url.endsWith("analysis_one")) return record as never;
    if (url.endsWith("analysis_two")) return other as never;
    return { ...list, items: [...list.items, { ...list.items[0], analysis_id: "analysis_two", paper_id: "other-paper" }] } as never;
  });
  render(<Session/>); await open();
  fireEvent.click(screen.getByRole("button", { name: "분석 검토 저장" }));
  fireEvent.click(screen.getByRole("button", { name: "분석 열기 · other-paper" }));
  await screen.findByText(/원래 주제: original-topic · 논문: other-paper/);
  const decision = { decision_id: "d1", state: "reviewed", note: "", command };
  await act(async () => finish({ ...record, latest_decision: decision, decisions: [decision] }));
  await waitFor(() => expect(screen.getByText(/원래 주제: original-topic · 논문: other-paper/)).toBeTruthy());
  expect(screen.queryByText(/원래 주제: original-topic · 논문: public-paper/)).toBeNull();
});

it("does not silently rebase an acknowledged replay over a newer human review", async () => {
  let result = record as unknown;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") {
      const command = JSON.parse(options.body as string);
      const old = { decision_id: "d1", state: command.state, note: command.note, command };
      const newer = { decision_id: "d2", state: "rejected", note: "Newer human review", command: { ...command, request_id: "other" } };
      result = { ...record, latest_decision: newer, decisions: [old, newer] };
      return result as never;
    }
    return (url.includes("analysis_one") ? result : list) as never;
  });
  render(<Session/>); await open(); fireEvent.change(screen.getByLabelText("분석 검토 메모"), { target: { value: "Our saved note" } });
  fireEvent.click(screen.getByRole("button", { name: "분석 검토 저장" }));
  await screen.findByText("검토를 새 이력으로 저장했습니다.");
  expect(screen.getByText(/편집 기준과 최신 이력이 다릅니다/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "분석 검토 저장" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("value", "Our saved note");
});

it("keeps a rejected file for explicit retry and never sends an automatic retry", async () => {
  render(<Session/>); await open();
  vi.mocked(api.fetchJson).mockRejectedValue(new api.ApiError("invalid", 422, "invalid"));
  const originalText = '{"invalid":true,"timeout":300.0}';
  const file = new File([originalText], "invalid.json", { type: "application/json" });
  fireEvent.change(screen.getByLabelText("분석 응답 묶음 파일"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "구조화 응답 저장" }));
  await screen.findByRole("alert");
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  expect(vi.mocked(api.fetchJson).mock.calls.find(([, options]) => options?.method === "POST")?.[1]?.body).toBe(originalText);
  expect(screen.getByRole("button", { name: "구조화 응답 저장" })).toHaveProperty("disabled", false);
  expect(screen.getByText("Preserved e\u0301 evidence")).toBeTruthy();
});

it("lets the user correct a definitively rejected review without losing its note", async () => {
  render(<Session/>); await open();
  fireEvent.change(screen.getByLabelText("분석 검토 메모"), { target: { value: "Keep and correct" } });
  vi.mocked(api.fetchJson).mockRejectedValue(new api.ApiError("invalid", 422, "invalid"));
  fireEvent.click(screen.getByRole("button", { name: "분석 검토 저장" }));
  await screen.findByText(/검토 입력이 거부되어 저장되지 않았습니다/);
  expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("disabled", false);
  expect(screen.getByLabelText("분석 검토 메모")).toHaveProperty("value", "Keep and correct");
});
