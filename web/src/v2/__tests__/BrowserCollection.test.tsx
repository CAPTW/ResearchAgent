import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../../lib/api";
import { BrowserCollection } from "../components/BrowserCollection";

vi.mock("../../lib/api", async original => ({ ...await original<typeof api>(), fetchJson: vi.fn(), getApiBase: () => "http://127.0.0.1:8765" }));
const candidate = { arxiv_id: "2610.99999v1", title: "Synthetic collection paper", authors: ["Synthetic Author"], published: "2026-10-01", abstract: "Synthetic", source_url: "https://arxiv.org/abs/2610.99999v1", pdf_url: "https://arxiv.org/pdf/2610.99999v1" };
const makeJob = (topic: string, state = "queued") => ({ job_id: "bc_fixture", topic_id: topic, query: "all:synthetic", created_at: "2026-10-26T12:00:00Z", external_ai_state: "NOT_SENT", connection: { state: "DISCONNECTED", expires_at: null }, items: [{ item_id: "bci_fixture", candidate, state, attempts: [], active_attempt_id: null, pdf: state === "awaiting_confirmation" ? { sha256: "a".repeat(64), bytes: 100 } : null, saved: null }] });
beforeEach(() => { vi.resetAllMocks(); vi.stubGlobal("crypto", { randomUUID: () => "synthetic-request" }); });
afterEach(() => vi.unstubAllGlobals());
const writes = () => vi.mocked(api.fetchJson).mock.calls.filter(([, init]) => init?.method === "POST");
async function open() { fireEvent.click(screen.getByText("브라우저에서 논문 수집")); await screen.findByText("수집 상태 새로고침"); }

it("opens with read-only listing; explicit search and selection create a job without downloading", async () => {
  const job = makeJob("search-topic");
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/search")) return { query: "all:synthetic", search_receipt_id: "search-receipt", candidates: [candidate], retrieved_at: "2026-10-26T12:00:00Z" } as never;
    if (init?.method === "POST") return job as never;
    return { items: [] } as never;
  });
  render(<BrowserCollection topicId="search-topic"/>); await open();
  await screen.findByText("아직 저장된 수집 작업이 없습니다."); expect(writes()).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("arXiv 검색식"), { target: { value: "all:synthetic" } });
  fireEvent.click(screen.getByRole("button", { name: "공식 API로 검색" }));
  await screen.findByText(candidate.title); fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "선택한 논문 수집 작업 저장" }));
  await waitFor(() => expect(writes()).toHaveLength(2));
  const body = JSON.parse(writes()[1][1]!.body as string);
  expect(body.candidates[0].arxiv_id).toBe(candidate.arxiv_id);
  expect(body.query).toBe("all:synthetic");
  expect(body.search_receipt_id).toBe("search-receipt");
  expect(writes().some(([path]) => path.includes("connection"))).toBe(false);
});

it("a definite create rejection survives navigation and can be released for editing", async () => {
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/search")) return { query: "all:synthetic", candidates: [candidate], retrieved_at: "2026-10-26T12:00:00Z" } as never;
    if (init?.method === "POST") throw new api.ApiError("Rejected", 422, "invalid");
    return { items: [] } as never;
  });
  const view = render(<BrowserCollection topicId="rejected-topic"/>); await open();
  await screen.findByText("아직 저장된 수집 작업이 없습니다.");
  fireEvent.change(screen.getByLabelText("arXiv 검색식"), { target: { value: "all:synthetic" } });
  fireEvent.click(screen.getByRole("button", { name: "공식 API로 검색" }));
  await screen.findByText(candidate.title); fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "선택한 논문 수집 작업 저장" }));
  await screen.findByRole("button", { name: "거절된 요청 해제하고 수정" });
  view.unmount(); render(<BrowserCollection topicId="rejected-topic"/>); await open();
  await waitFor(() => expect(screen.getByRole("button", { name: "거절된 요청 해제하고 수정" })).toHaveProperty("disabled", false));
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "거절된 요청 해제하고 수정" }));
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("disabled", false);
});

it("a retry with an unknown outcome removes an earlier rejection's reset authority", async () => {
  let creates = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/search")) return { query: "all:synthetic", candidates: [candidate], retrieved_at: "2026-10-26T12:00:00Z" } as never;
    if (init?.method === "POST") { if (++creates === 1) throw new api.ApiError("Rejected", 422, "invalid"); throw Error("Unknown response"); }
    return { items: [] } as never;
  });
  render(<BrowserCollection topicId="rejected-then-unknown-topic"/>); await open();
  await screen.findByText("아직 저장된 수집 작업이 없습니다.");
  fireEvent.change(screen.getByLabelText("arXiv 검색식"), { target: { value: "all:synthetic" } });
  fireEvent.click(screen.getByRole("button", { name: "공식 API로 검색" }));
  await screen.findByText(candidate.title); fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "선택한 논문 수집 작업 저장" }));
  await screen.findByRole("button", { name: "거절된 요청 해제하고 수정" });
  fireEvent.click(screen.getByRole("button", { name: "같은 수집 작업 저장 확인" }));
  await screen.findByText("Unknown response");
  expect(screen.queryByRole("button", { name: "거절된 요청 해제하고 수정" })).toBeNull();
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("disabled", true);
  expect(writes()[1][1]!.body).toEqual(writes()[2][1]!.body);
});

it("hides a possibly revoked code when replacement issuance loses its response", async () => {
  const job = makeJob("ticket-topic"); let issues = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path) => {
    if (path.endsWith("/connection")) {
      if (++issues > 1) throw Error("Unknown response");
      return { job_id: job.job_id, token: "x".repeat(43), expires_at: "2026-10-26T13:00:00Z" } as never;
    }
    return { items: [job] } as never;
  });
  render(<BrowserCollection topicId="ticket-topic"/>); await open();
  fireEvent.click(await screen.findByRole("button", { name: "브라우저 연결 코드 발급" }));
  await screen.findByLabelText("확장 프로그램에 붙여넣을 연결 코드");
  await waitFor(() => expect(screen.getByRole("button", { name: "브라우저 연결 코드 발급" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "브라우저 연결 코드 발급" }));
  await screen.findByRole("alert");
  expect(screen.queryByLabelText("확장 프로그램에 붙여넣을 연결 코드")).toBeNull();
});

it("requires PDF identity confirmation and refreshes Papers only after a save", async () => {
  const job = makeJob("confirm-topic", "awaiting_confirmation");
  const onSaved = vi.fn().mockResolvedValue(undefined);
  vi.mocked(api.fetchJson).mockImplementation(async (_path, init) => (init?.method === "POST" ? job : { items: [job] }) as never);
  render(<BrowserCollection topicId="confirm-topic" onSaved={onSaved}/>); await open();
  const button = await screen.findByRole("button", { name: "확인한 PDF를 Papers에 저장" });
  expect(button).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(button);
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(JSON.parse(writes()[0][1]!.body as string)).toEqual({ expected_pdf_sha256: "a".repeat(64), identity_confirmed: true });
});

it("authentication-required work remains visible and resume is explicit", async () => {
  const job = makeJob("resume-topic", "auth_required");
  vi.mocked(api.fetchJson).mockResolvedValue({ items: [job] } as never);
  render(<BrowserCollection topicId="resume-topic"/>); await open();
  await screen.findByText(/로그인·MFA·CAPTCHA를 직접 처리/); expect(writes()).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "이 항목 재개 준비" }));
  await waitFor(() => expect(writes()).toHaveLength(1));
  expect(JSON.parse(writes()[0][1]!.body as string)).toEqual({ action: "resume" });
});

it("lost create response keeps request identity for explicit reconciliation", async () => {
  let createCount = 0;
  vi.mocked(api.fetchJson).mockImplementation(async (path, init) => {
    if (path.endsWith("/search")) return { query: "all:synthetic", candidates: [candidate], retrieved_at: "2026-10-26T12:00:00Z" } as never;
    if (init?.method === "POST") { createCount++; throw Error("Unknown response"); }
    return { items: [] } as never;
  });
  render(<BrowserCollection topicId="unknown-topic"/>); await open(); await screen.findByText("아직 저장된 수집 작업이 없습니다.");
  fireEvent.change(screen.getByLabelText("arXiv 검색식"), { target: { value: "all:synthetic" } });
  fireEvent.click(screen.getByRole("button", { name: "공식 API로 검색" }));
  await screen.findByText(candidate.title); fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "선택한 논문 수집 작업 저장" }));
  await screen.findByRole("alert"); fireEvent.click(screen.getByRole("button", { name: "같은 수집 작업 저장 확인" }));
  await waitFor(() => expect(createCount).toBe(2));
  expect(writes()[1][1]!.body).toEqual(writes()[2][1]!.body);
});

it("rejects a connection-code paste before searching or preserving a navigation draft", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ items: [] } as never);
  const view = render(<BrowserCollection topicId="unsafe-paste-topic"/>); await open();
  await screen.findByText("아직 저장된 수집 작업이 없습니다.");
  const syntheticCode = JSON.stringify({ base_url: "http://127.0.0.1:8765", job_id: "bc_fixture", token: "x".repeat(43) });
  fireEvent.paste(screen.getByLabelText("arXiv 검색식"), { clipboardData: { getData: () => syntheticCode } });
  expect((await screen.findByRole("alert")).textContent).toContain("연결 코드나 인증 정보는 검색할 수 없습니다");
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("value", "");
  expect(screen.getByRole("button", { name: "공식 API로 검색" })).toHaveProperty("disabled", true);
  expect(writes()).toHaveLength(0);
  view.unmount(); render(<BrowserCollection topicId="unsafe-paste-topic"/>); await open();
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("value", "");
  expect(writes()).toHaveLength(0);
});

it("hides a legacy unsafe query and prevents issuing a new connection for that job", async () => {
  const job = makeJob("unsafe-history-topic");
  job.query = JSON.stringify({ base_url: "http://127.0.0.1:8765", job_id: "bc_fixture", token: "x".repeat(43) });
  vi.mocked(api.fetchJson).mockResolvedValue({ items: [job] } as never);
  render(<BrowserCollection topicId="unsafe-history-topic"/>); await open();
  expect(await screen.findByRole("button", { name: "브라우저 연결 코드 발급" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("저장된 수집 작업").textContent).not.toContain("x".repeat(43));
  expect(writes()).toHaveLength(0);
});

it("accepts ordinary research about tokens without treating the topic as a credential", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ items: [] } as never);
  render(<BrowserCollection topicId="token-research-topic"/>); await open();
  await screen.findByText("아직 저장된 수집 작업이 없습니다.");
  const query = 'all:"token" AND all:"API key security"';
  fireEvent.change(screen.getByLabelText("arXiv 검색식"), { target: { value: query } });
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("value", query);
  expect(screen.getByRole("button", { name: "공식 API로 검색" })).toHaveProperty("disabled", false);
});

it("does not retain recognized credential shapes from typed or pasted changes", async () => {
  vi.mocked(api.fetchJson).mockResolvedValue({ items: [] } as never);
  const view = render(<BrowserCollection topicId="credential-shapes-topic"/>); await open();
  await screen.findByText("아직 저장된 수집 작업이 없습니다.");
  for (const value of ["client_secret=" + "x".repeat(16), "access-token=" + "x=".repeat(8), "Bearer " + "x".repeat(16), "eyJ" + "a".repeat(8) + "." + "b".repeat(8) + "." + "c".repeat(8)]) {
    fireEvent.change(screen.getByLabelText("arXiv 검색식"), { target: { value } });
    expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("value", "");
    expect(screen.getByRole("button", { name: "공식 API로 검색" })).toHaveProperty("disabled", true);
  }
  view.unmount(); render(<BrowserCollection topicId="credential-shapes-topic"/>); await open();
  expect(screen.getByLabelText("arXiv 검색식")).toHaveProperty("value", "");
  expect(writes()).toHaveLength(0);
});
