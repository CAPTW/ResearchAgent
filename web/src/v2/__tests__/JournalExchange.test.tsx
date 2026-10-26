import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { JournalExchangePanel, SavedCandidate } from "../views/JournalExchangePanel";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
const empty = { items: [], total: 0, page: 1, limit: 10 };
const candidate = (id = "candidate-one") => ({
  candidate_id: id, imported_by: "owner", imported_at: "2026-09-27T00:00:00Z", raw_json: '{"title":"Proposal"}', raw_sha256: "raw-sha",
  proposal: { title: "Proposal", sections: [{ kind: "synthesis", entries: [{ kind: "interpretation", text: '<img src="https://external.invalid/source">', citation_ids: ["ref"] }] }] }, packet: { packet_id: "frozen-packet", references: [{ paper_version_id: "pv2", title: "Not cited paper" }] },
  citations: [{ citation_id: "ref", title: "Source paper", kind: "abstract", text: "Frozen text", paper_version_id: "pv1", locator: null, source_identity: { doi: "original" }, review_state: "abstract_only", rights: { state: "unknown" } }],
  markdown: '# 미검토 후보\n\n> <img src="https://external.invalid/source">\n\n## 인용\n\n> Frozen text',
  coverage: { selected_papers: 3, cited_papers: 1, abstract_citations: 1, excerpt_citations: 0, uncited_paper_version_ids: ["pv2", "pv3"] },
});
const summary = (id = "candidate-one") => ({ candidate_id: id, title: id, imported_at: "2026-09-27T00:00:00Z" });
const file = (raw: string) => ({ name: "result.json", size: new TextEncoder().encode(raw).length, arrayBuffer: async () => new TextEncoder().encode(raw).buffer });
beforeEach(() => vi.resetAllMocks());
it("labels the exact original title as historical without changing its candidate or requesting a review write", async () => {
  const value = candidate();
  value.proposal.title = "원문 대조 초안 (미채택) · e\u0301 한글🙂";
  const before = JSON.stringify(value);
  vi.mocked(api.fetchJson).mockResolvedValue(value);
  render(<SavedCandidate root="/candidates" candidateId="candidate-one"/>);
  const heading = await screen.findByRole("heading", { name: `보존된 원고 제목 ${value.proposal.title}` });
  expect(within(heading).getByText(value.proposal.title).textContent).toBe(value.proposal.title);
  expect(screen.getByText(/제목에 적힌 상태와 현재 검토 상태는 다를 수 있습니다/)).toBeTruthy();
  expect(JSON.stringify(value)).toBe(before);
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/candidate-one");
});
it("labels a selected review input citation separately and displays the preserved past result as interpretation", async () => {
  const value = candidate();
  vi.mocked(api.fetchJson).mockResolvedValue({ ...value, coverage: { ...value.coverage, review_input_excerpt_citations: 1 }, citations: value.citations.map(citation => ({ ...citation, kind: "review_input_excerpt", review_receipt_id: "receipt-one", input_origin: "FIXTURE_SYNTHETIC" })), packet: { ...value.packet, review_inputs: [{ schema_version: "JOURNAL_REVIEW_INPUT_V1", paper_version_id: "pv1", receipt_id: "receipt-one", input_origin: "FIXTURE_SYNTHETIC", run_id: "run", paper_id: "paper", review_level: "full_text_grounded", status: "accepted", validation: { status: "validated" }, selected_component_ids: ["/selected_text_excerpts/0/text"], input_scope: {}, source_extraction: null, review_result: { contribution_summary: "Earlier model interpretation" } }] } });
  render(<SavedCandidate root="/candidates" candidateId="candidate-one"/>);
  await screen.findByText(/인용 \[1\].*선택한 검토 입력 발췌/);
  expect(screen.getByText("Earlier model interpretation")).toBeTruthy();
  expect(screen.getByText("선택한 검토 입력 발췌 인용: 1개")).toBeTruthy();
  expect(screen.getByText(/당시 검토 결과 상태: accepted/)).toBeTruthy();
  expect(screen.getByText(/당시 검토 요청 전체의 보존 범위/)).toBeTruthy();
  expect(screen.getByText(/과거 검토 결과 · 원문 사실과 구분되는 해석/)).toBeTruthy();
});
it("previews the preserved journal packet by GET without replacing a pending imported file", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => (url.endsWith("/journal-exchange") ? { packet_id: "bound-packet", review_inputs: [] } : empty) as never);
  await open(); await choose();
  fireEvent.click(screen.getByRole("button", { name: "저널 고정 입력 확인" }));
  await screen.findByRole("region", { name: "보존된 저널 입력 미리보기" });
  expect(screen.getByText("선택한 파일: result.json")).toBeTruthy();
  expect(screen.getByText("입력 묶음: bound-packet")).toBeTruthy();
  expect(vi.mocked(api.fetchJson).mock.calls.some(([,options])=>options?.method==="POST")).toBe(false);
});
it("labels generated provenance separately without claiming verified live execution or import authorship", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { attempt: 2, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "test", model: "requested" } }, provider_reported: { provider_name: "test", model: "reported", response_id: null } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText(/어댑터가 보고한 Provider·모델: test · reported/)).toBeTruthy();
  expect(screen.getByText(/테스트 어댑터의 응답일 수도 있습니다/)).toBeTruthy();
  expect(screen.queryByText(/가져온 사용자:/)).toBeNull();
  expect(screen.getByText(/후보로 보존한 사용자: reviewer/)).toBeTruthy();
  fireEvent.click(screen.getByText("보존된 실행 출처"));
  expect(screen.getByText(/요청 Provider·모델: test · requested/)).toBeTruthy();
  expect(screen.getByText("응답 ID: 기록 없음")).toBeTruthy();
});
it("labels a generated candidate's unreported model without substituting its requested model", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { attempt: 1, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "codex_cli", model: "requested-codex-model" } }, provider_reported: { provider_name: "codex_cli", model: "not_reported", response_id: "recorded-thread" } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText("어댑터가 보고한 Provider·모델: codex_cli · 실제 보고 모델 미확인")).toBeTruthy();
  expect(screen.queryByText(/어댑터가 보고한 Provider·모델:.*requested-codex-model|not_reported/)).toBeNull();
  fireEvent.click(screen.getByText("보존된 실행 출처"));
  expect(screen.getByText("요청 Provider·모델: codex_cli · requested-codex-model")).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/generated");
});
async function open() {
  const view = render(<JournalExchangePanel topicId="topic" jobId="job"/>);
  expect(api.fetchJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("저널 입력·결과 파일 교환"));
  await screen.findByText("보존된 저널 후보 · 0개");
  return view;
}

it("describes Codex V2 last-message JSON without claiming a retained process log", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { schema_version: "JOURNAL_EXECUTION_RECEIPT_V2", adapter_manifest: { schema_version: "CODEX_JOURNAL_CLI_V2", raw_output_encoding: "LAST_MESSAGE_JSON_UTF8_V1", raw_envelope_retained: false },
      attempt: 1, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "codex_cli", model: "requested" } }, provider_reported: { provider_name: "codex_cli", model: "not_reported", response_id: "thread" } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText(/Codex CLI V2의 최종 응답 JSON을 개행·문자 그대로 보존/)).toBeTruthy();
  expect(screen.getByText(/전체 CLI 출력 로그를 보존한 파일이 아닙니다/)).toBeTruthy();
  expect(screen.getByText("어댑터가 보고한 Provider·모델: codex_cli · 실제 보고 모델 미확인")).toBeTruthy();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/generated");
});

it("describes Anthropic content JSON without claiming a retained HTTP envelope", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { schema_version: "JOURNAL_EXECUTION_RECEIPT_V2", adapter_manifest: { schema_version: "ANTHROPIC_JOURNAL_WIRE_V1", raw_output_encoding: "CONTENT_TEXT_JSON_UTF8_V1", raw_envelope_retained: false },
      attempt: 1, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "anthropic", model: "requested" } }, provider_reported: { provider_name: "anthropic", model: "reported", response_id: "message" } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText(/Anthropic 응답의 assistant content text JSON을 개행·문자 그대로 보존/)).toBeTruthy();
  expect(screen.getByText(/HTTP 응답 전체를 보존한 파일이 아닙니다/)).toBeTruthy();
  expect(screen.queryByText(/Claude 구조화된 결과/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/generated");
});

it("describes Grok response JSON without claiming a retained ACP transcript", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { schema_version: "JOURNAL_EXECUTION_RECEIPT_V2", adapter_manifest: { schema_version: "GROK_JOURNAL_ACP_V1", raw_journal_encoding: "ACP_AGENT_MESSAGE_TEXT_UTF8_V1", raw_envelope_retained: false },
      attempt: 1, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "grok_cli", model: "requested" } }, provider_reported: { provider_name: "grok_cli", model: "not_reported", response_id: "session" } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText(/Grok ACP 응답 문장에서 추출한 JSON/)).toBeTruthy();
  expect(screen.getByText(/전체 ACP 대화 로그를 보존한 파일이 아닙니다/)).toBeTruthy();
  expect(screen.queryByText(/Claude 구조화된 결과/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/generated");
});

it("describes Antigravity response JSON without claiming a retained NDJSON stream", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { schema_version: "JOURNAL_EXECUTION_RECEIPT_V2", adapter_manifest: { schema_version: "ANTIGRAVITY_JOURNAL_CLI_V1", raw_journal_encoding: "RESULT_RESPONSE_JSON_UTF8_V1", raw_envelope_retained: false },
      attempt: 1, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "antigravity_cli", model: "requested" } }, provider_reported: { provider_name: "antigravity_cli", model: "not_reported", response_id: "session" } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText(/Antigravity 최종 결과 응답의 JSON/)).toBeTruthy();
  expect(screen.getByText(/전체 NDJSON 출력 로그를 보존한 파일이 아닙니다/)).toBeTruthy();
  expect(screen.queryByText(/Claude 구조화된 결과/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/generated");
});

it("describes extracted Claude candidate JSON without claiming original stdout", async () => {
  const generated = { ...candidate("generated"), origin: "RECORDED_ADAPTER_RESULT", preserved_by: "reviewer", preserved_at: "2026-09-27T00:00:00Z",
    execution: { schema_version: "JOURNAL_EXECUTION_RECEIPT_V2", adapter_manifest: { schema_version: "CLAUDE_JOURNAL_CLI_V1", raw_journal_encoding: "EXTRACTED_CANONICAL_JSON_V1", raw_envelope_retained: false },
      attempt: 1, completed_at: "2026-09-27T00:00:00Z", result_digest: "result-sha", contract: { execution_id: "exec-1", actor_id: "requester", specification: { provider_name: "claude_code", model: "requested" } }, provider_reported: { provider_name: "claude_code", model: "not_reported", response_id: "session" } } };
  vi.mocked(api.fetchJson).mockResolvedValue(generated);
  render(<SavedCandidate root="/candidates" candidateId="generated"/>);
  await screen.findByRole("article", { name: "생성 결과 저널 후보" });
  expect(screen.getByText(/CLI 표준 출력 원문을 보존한 파일이 아닙니다/)).toBeTruthy();
  expect(screen.getByText("보존된 후보 JSON과 식별자")).toBeTruthy();
  expect(screen.queryByText(/원문 UTF-8 SHA-256/)).toBeNull();
  expect(api.fetchJson).toHaveBeenCalledExactlyOnceWith("/candidates/generated");
});
async function choose(raw = '{"title":"Proposal"}') {
  fireEvent.change(screen.getByLabelText("저널 결과 JSON 파일"), { target: { files: [file(raw)] } });
  await screen.findByText("선택한 파일: result.json");
}

it("requires explicit import, preserves original UTF-8 text and shows unreviewed coverage with inert source text", async () => {
  let saved = false;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === "POST") { saved = true; return { candidate: candidate(), created: true } as never; }
    if (url.endsWith("/candidate-one")) return candidate() as never;
    return (saved ? { ...empty, items: [summary()], total: 1 } : empty) as never;
  });
  await open(); await choose('\uFEFF{ "title": "한글" }\r\n');
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "미검토 저널 후보 가져오기" }));
  await screen.findByText("보존된 저널 후보 · 1개");
  const article = await screen.findByRole("article", { name: "가져온 저널 후보" });
  await within(article).findByText(/인용한 논문 1 \/ 선택 논문 3/);
  expect(within(article).getByText(/자동 의미 검증·독립적 과학 검증 미수행/)).toBeTruthy();
  expect(within(article).getByText("인용하지 않은 선택 논문 2개")).toBeTruthy();
  expect(within(article).queryByRole("img")).toBeNull();
  const body = vi.mocked(api.fetchJson).mock.calls.find(([, o]) => o?.method === "POST")![1]!.body as string;
  expect(JSON.parse(body)).toEqual({ raw_json: '\uFEFF{ "title": "한글" }\r\n' });
});

it("retains the selected file and exact retry bytes after lost response or rejected binding", async () => {
  let writes = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (_, options) => {
    if (options?.method === "POST") { writes++; throw new api.ApiError(writes === 1 ? "response lost" : "journal_packet_mismatch", writes === 1 ? 503 : 409, ""); }
    return empty as never;
  });
  await open(); await choose();
  fireEvent.click(screen.getByRole("button", { name: "미검토 저널 후보 가져오기" }));
  await screen.findByText(/response lost/);
  fireEvent.click(screen.getByRole("button", { name: "미검토 저널 후보 가져오기" }));
  await screen.findByText(/다른 고정 입력의 결과입니다/);
  const posts = vi.mocked(api.fetchJson).mock.calls.filter(([, o]) => o?.method === "POST");
  expect(posts[0][1]?.body).toBe(posts[1][1]?.body);
  expect(screen.getByText("선택한 파일: result.json")).toBeTruthy();
  expect(screen.queryByRole("article")).toBeNull();
});

it("rejects malformed UTF-8, JSON and oversize before submitting, clearing a previous valid file", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue(empty);
  await open(); await choose();
  for (const bad of [file("not JSON"), { ...file("{}"), size: 512001 }, { ...file("{}"), arrayBuffer: async () => new Uint8Array([0xff]).buffer }]) {
    fireEvent.change(screen.getByLabelText("저널 결과 JSON 파일"), { target: { files: [bad] } });
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "미검토 저널 후보 가져오기" })).toHaveProperty("disabled", true);
    expect(screen.queryByText("선택한 파일: result.json")).toBeNull();
  }
  expect(vi.mocked(api.fetchJson).mock.calls.some(([, o]) => o?.method === "POST")).toBe(false);
});

it("ignores late file decoding and history from the previous job", async () => {
  let resolve!:(value: ArrayBuffer) => void;
  vi.mocked(api.fetchJson).mockResolvedValue(empty);
  const view = await open();
  fireEvent.change(screen.getByLabelText("저널 결과 JSON 파일"), { target: { files: [{ ...file("{}"), arrayBuffer: () => new Promise<ArrayBuffer>(r => { resolve = r; }) }] } });
  view.rerender(<JournalExchangePanel topicId="topic" jobId="new"/>);
  await screen.findByText("보존된 저널 후보 · 0개");
  await act(async () => resolve(new TextEncoder().encode('{}').buffer));
  expect(screen.queryByText("선택한 파일: result.json")).toBeNull();
  expect(screen.getByRole("button", { name: "미검토 저널 후보 가져오기" })).toHaveProperty("disabled", true);
});

it("keeps pending input after a history error and allows import independently of that read failure", async () => {
  vi.mocked(api.fetchJson).mockResolvedValueOnce(empty).mockRejectedValueOnce(new Error("history offline"));
  await open(); await choose();
  fireEvent.click(screen.getByRole("button", { name: "보존 후보 이력 새로고침" }));
  await screen.findByText(/history offline/);
  expect(screen.getByText("선택한 파일: result.json")).toBeTruthy();
  expect(screen.getByRole("button", { name: "미검토 저널 후보 가져오기" })).toHaveProperty("disabled", false);
  expect(screen.getByRole("button", { name: "다음 후보 페이지" })).toHaveProperty("disabled", true);
});

it("pages history and does not let an old candidate response replace the selected candidate or hide a read failure", async () => {
  let resolve!:(value: unknown) => void;
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.endsWith("/one")) return new Promise(r => { resolve = r; }) as never;
    if (url.endsWith("/two")) throw new Error("integrity unavailable");
    return { items: [summary(url.includes("page=2") ? "two" : "one")], total: 11, page: url.includes("page=2") ? 2 : 1, limit: 10 } as never;
  });
  render(<JournalExchangePanel topicId="topic" jobId="job"/>);
  fireEvent.click(screen.getByText("저널 입력·결과 파일 교환"));
  fireEvent.click(await screen.findByRole("button", { name: /^one/ }));
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  fireEvent.click(screen.getByRole("button", { name: "다음 후보 페이지" }));
  fireEvent.click(await screen.findByRole("button", { name: /^two/ }));
  await screen.findByText(/integrity unavailable/);
  await act(async () => resolve(candidate("one")));
  expect(screen.queryByRole("heading", { name: "Proposal" })).toBeNull();
  expect(screen.queryByRole("button", { name: "원본 후보·근거 JSON 내려받기" })).toBeNull();
});
