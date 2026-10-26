import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { candidateOriginLabel, candidateTimestamp } from "../../lib/journalCandidates";
import { JournalExchangePanel, SavedCandidate } from "../views/JournalExchangePanel";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const at = "2026-10-15T00:00:00Z";
const empty = { items: [], total: 0, page: 1, limit: 10 };
const candidate = {
  schema_version: "REVIEW_INPUT_JOURNAL_CANDIDATE_V1", origin: "PRESERVED_REVIEW_DERIVATION" as const,
  candidate_id: "derived-one", preserved_at: at, preserved_by: "owner", raw_json: "{}", raw_sha256: "a".repeat(64),
  source: { receipt_id: "receipt-one", paper_version_id: "paper-one", input_origin: "PROVIDER_RESPONSE", model: "fixture-model", validation_status: "accepted",
    receipt_sha256: "b".repeat(64), request_sha256: null, raw_text_sha256: null, selected_input_component_ids: ["/evidence_spans/0/text"] },
  selected_component_ids: ["/contribution_summary"], component_bindings: [{ section: "overview", position: 0, component_id: "/contribution_summary", label: "기여 요약", support: "NOT_ASSESSED" }],
  proposal: { title: "저장된 분석 후보", sections: [{ kind: "overview", entries: [{ kind: "interpretation", text: "보존된 해석", citation_ids: ["ref"] }] }] },
  packet: { packet_id: "packet-one", references: [{ paper_version_id: "paper-one", title: "논문" }] },
  citations: [{ citation_id: "ref", title: "논문", kind: "review_input_excerpt", text: "고정된 원문", paper_version_id: "paper-one", locator: null,
    source_identity: null, review_state: "accepted", rights: null, review_receipt_id: "receipt-one", input_origin: "PROVIDER_RESPONSE" }],
  coverage: { selected_papers: 1, cited_papers: 1, abstract_citations: 0, excerpt_citations: 0, review_input_excerpt_citations: 1, uncited_paper_version_ids: [] },
  markdown: "# 저장된 분석 후보",
};
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.fetchJson).mockResolvedValue(empty); });

it("offers a separate saved-analysis candidate disclosure without automatically loading or choosing inputs", async () => {
  render(<JournalExchangePanel topicId="topic" jobId="job"/>);
  fireEvent.click(screen.getByText("저널 입력·결과 파일 교환"));
  await screen.findByText("보존된 저널 후보 · 0개");
  expect(screen.getByText("저장된 분석으로 Journal 후보 만들기")).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.some(([url]) => url.includes("journal-review-input"))).toBe(false);
});

it("keeps derived candidate origin and preservation time separate from imported and generated history", () => {
  expect(candidateOriginLabel(candidate)).toBe("저장된 분석에서 구성");
  expect(candidateTimestamp(candidate)).toBe(at);
  expect(candidateOriginLabel({ imported_at: at })).toBe("파일로 가져온 후보");
  expect(candidateOriginLabel({ origin: "RECORDED_ADAPTER_RESULT", preserved_at: at })).toBe("저장된 생성 결과");
});

it("opens a derived candidate for existing citation review without claiming a new model execution or automatic adoption", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(candidate);
  render(<SavedCandidate root="/candidates" candidateId="derived-one"/>);
  await screen.findByRole("article", { name: "저장된 분석으로 구성한 저널 후보" });
  expect(screen.getByText(/새 모델 호출 없이 저장된 분석 항목으로 구성/)).toBeTruthy();
  expect(screen.getByText(/후보로 보존한 사용자: owner/)).toBeTruthy();
  expect(screen.queryByText(/가져온 사용자:/)).toBeNull();
  expect(screen.getByText(/독립적 과학 검증 미수행/)).toBeTruthy();
  expect(screen.getByText(/인용 \[1\].*선택한 검토 입력 발췌/)).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
});

it("locks other workspace inputs after lost response while preserving its own exact retry and opening the saved candidate", async () => {
  let writes = 0;
  const input = { ...candidate.source, components: [{ component_id: "/contribution_summary", label: "기여 요약", text: "보존된 해석",
    section: "overview", kind: "interpretation", support: "NOT_ASSESSED", eligible: true, ineligible_reason: null }] };
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { if (++writes === 1) throw new api.ApiError("lost", 503, ""); return { candidate, created: false } as never; }
    if (url.endsWith("journal-review-inputs")) return { packet_id: "packet-one", inputs: [input] } as never;
    if (url.endsWith("/derived-one")) return candidate as never;
    return { ...empty, total: 1, items: [{ candidate_id: "derived-one", title: "기존 보존 후보", origin: candidate.origin, preserved_at: at }] } as never;
  });
  render(<JournalExchangePanel topicId="topic" jobId="job"/>); fireEvent.click(screen.getByText("저널 입력·결과 파일 교환"));
  await screen.findByText("보존된 저널 후보 · 1개"); fireEvent.click(screen.getByText("저장된 분석으로 Journal 후보 만들기"));
  fireEvent.change(await screen.findByLabelText("후보에 사용할 저장 분석"), { target: { value: "receipt-one" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /기여 요약/ })); fireEvent.click(screen.getByRole("button", { name: "선택한 항목을 Journal 후보로 저장" }));
  const retry = await screen.findByRole("button", { name: "같은 Journal 후보 저장 다시 확인" });
  expect(retry).toHaveProperty("disabled", false); expect(screen.getByLabelText("저널 결과 JSON 파일")).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: /^기존 보존 후보/ })).toHaveProperty("disabled", true);
  fireEvent.click(retry); await screen.findByRole("article", { name: "저장된 분석으로 구성한 저널 후보" });
  await waitFor(() => expect(screen.getByLabelText("저널 결과 JSON 파일")).toHaveProperty("disabled", false));
  const posts = vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === "POST"); expect(posts).toHaveLength(2); expect(posts[0][1]?.body).toBe(posts[1][1]?.body);
});
